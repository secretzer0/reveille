#!/usr/bin/env python3
"""Cross-machine broker daemon: HTTP-MCP data plane + WebSocket wake plane.

One process on an always-on host (your LAN box). It serves:
  - MCP over streamable-HTTP at /mcp  -- agents call send/inbox/thread/... remotely.
  - a WebSocket wake endpoint at /wake -- the wake plane, pushed not polled.
behind one SQLite store (store.py, transport-agnostic and reused verbatim).

Why a daemon now: a Claude session on another machine (e.g. a Mac doing iOS work)
can't share a local file/SQLite. So the data lives here and every machine -- local
or remote -- talks to this one broker.

Identity: each agent's MCP registration sends a static `X-Agent: <name>` header;
tools resolve "me" from it. Auth: an optional shared `AGENTBUS_TOKEN` (Bearer on
HTTP, ?token= on the WS). Unset = open mode, fine for a fully trusted LAN.

Wake: scripts/agent runs a wake sidecar -- a tiny WS client that connects with the
agent's name and holds the socket (0 tokens). The daemon pushes a frame the instant a
message for that agent is sent; the sidecar pokes the agent's tmux pane (send-keys),
waking the session to pull its mail over MCP. One held connection, one poke per message.
"""
import asyncio
import contextlib
import fcntl
import logging
import os
import pathlib
import pty
import re
import struct
import termios

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from agentbus import __version__, store

# scripts/agent in this repo -- the web terminal spawns it to launch a session in tmux.
AGENT_BIN = str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent")

TOKEN = os.environ.get("AGENTBUS_TOKEN") or None  # None = open (trusted LAN)

# The authoritative how-to, served BY the broker (usage tool + GET /usage) so any agent
# on any machine fetches it over the wire -- never points at a file on someone's disk.
USAGE = """AGENTBUS usage. Source: usage() tool or GET /usage. Tool signatures are in your
MCP tool schemas; this is only what they don't cover.

ENV (set by the launching pane; never hardcode or prompt):
  $AGENT_ROLE      your bus name (the X-Agent header). Unset -> you are "unset-agent".
  $AGENTBUS_TOKEN  the bus secret.

USE:
1. Startup: join(url="http://<broker-host>:8765").
2. Reachability: launch via `scripts/agent <role>` in tmux. A sidecar then holds your wake
   socket at 0 tokens and pokes your pane when a real message arrives -- do not run wake
   yourself. On a poke ("agentbus ring...") or any turn: inbox() -> act -> ack(ids).
   No tmux -> no poke; just inbox() each turn (messages queue durably).
3. Messaging: unicast by default; broadcast (to="*") only if most peers care; reply_to to
   thread. DIRECTIVE:LEAVE addressed to you -> leave().

--- CLAUDE.md block (replace any old agentbus section) ---
## Agent bus
Identity/token from env, never hardcode: $AGENT_ROLE = my bus name, $AGENTBUS_TOKEN = secret.
Startup: join(url="http://<broker-host>:8765").
Reachability: launched via `scripts/agent <role>` in tmux -> a sidecar pokes my pane on real
messages; I idle at 0 tokens and do not run wake myself. On a poke or any turn: inbox(),
act, ack(). No tmux -> inbox() each turn.
Messaging: unicast default; broadcast only if most peers care; reply_to to thread.
DIRECTIVE:LEAVE to me -> leave().
Full reference: usage() or GET <broker>/usage.
"""

log = logging.getLogger("agentbus")  # logs client name + thread id per op; level via AGENTBUS_LOG

_conn = None  # one connection, used only from the event-loop thread (async tools)

# In-process wake registry: name -> set of asyncio.Queue, one per connected wake.py.
# send() notifies; the WS handler awaiting the queue pushes a frame and the client
# exits. This is the wake signal -- the message itself stays in SQLite, read over MCP.
_waiters: dict[str, set] = {}

mcp = FastMCP("agentbus", stateless_http=True, json_response=True)


def _notify(names):
    for n in names:
        for q in list(_waiters.get(n, ())):
            q.put_nowait(None)


def _me(ctx: Context) -> str:
    req = ctx.request_context.request
    name = req.headers.get("x-agent") if req else None
    if not name:
        raise ValueError("missing X-Agent header (set it in your MCP registration)")
    return name


def _seen(name):
    with contextlib.suppress(store.BusError):
        store.touch(_conn, name)  # heartbeat if joined; no-op otherwise


# ---- MCP tools (async -> run on the loop thread, so one sqlite conn is safe) ----

@mcp.tool()
async def join(url: str = "", name: str = "", fresh: bool = False, ctx: Context = None) -> dict:
    """Join the bus, telling it where you reach the broker (`url`, e.g.
    http://bigbox.local:8765). Your identity is your X-Agent header (set per session
    from $AGENT_ROLE); pass `name` only to assert it matches. fresh=True skips the
    backlog. Returns {name, wake_url, unread}."""
    me = _me(ctx)
    if name and name != me:
        raise ValueError(f"join name {name!r} must match your X-Agent header {me!r}")
    store.join(_conn, me, tag=me, fresh=fresh, url=url or None)
    unread = len(store.inbox(_conn, me))
    log.info("%s join url=%s unread=%s", me, url or "-", unread)
    return {"name": me, "wake_url": _wake_url_from(url), "unread": unread}


@mcp.tool()
async def whoami(ctx: Context = None) -> str:
    """Your bus name for this session (from the X-Agent header)."""
    return _me(ctx)


@mcp.tool()
async def usage(ctx: Context = None) -> str:
    """How to attach to the bus and stay reachable (identity, token, join/inbox/send,
    wake, and the exit-144 sandbox fallback). Authoritative copy, served by the broker."""
    return USAGE


@mcp.tool()
async def info(ctx: Context = None) -> str:
    """Reveille status banner: tool version, your bus name, and whether your wake sidecar
    is attached right now. Call it on boot to confirm the bus works end to end."""
    me = _me(ctx)
    attached = bool(_waiters.get(me))
    return (f"Reveille v{__version__} -- you are '{me}' -- "
            f"sidecar: {'ATTACHED (real-time wake)' if attached else 'not attached (no real-time wake)'}")


@mcp.tool()
async def send(to: str, body: str, subject: str = "",
               reply_to: int | list[int] | None = None, ctx: Context = None) -> dict:
    """Send a message. to='*' broadcasts; else unicast to one agent. reply_to is a
    message id (or list, to merge branches). Pushes the recipient(s) awake over WS.
    Returns {id, thread_id, parents, delivered_to}."""
    me = _me(ctx)
    _seen(me)
    res = store.send(_conn, me, to, body, subject=subject, reply_to=reply_to)
    _notify(res["wake"])
    log.info("%s send -> %s thread=%s id=%s%s -> woke %s",
             me, to, res["thread_id"], res["id"],
             f" reply_to={reply_to}" if reply_to is not None else "", res["wake"])
    return {"id": res["id"], "thread_id": res["thread_id"],
            "parents": res["parents"], "delivered_to": res["wake"]}


@mcp.tool()
async def inbox(ctx: Context = None) -> dict:
    """Your unread messages (direct + broadcast), oldest first, as {"messages": [...]}.
    Non-destructive: ack(message_ids) when processed."""
    me = _me(ctx)
    _seen(me)
    msgs = store.inbox(_conn, me)
    log.info("%s inbox -> %s unread", me, len(msgs))
    return {"messages": msgs}


@mcp.tool()
async def ack(message_ids: list[int], ctx: Context = None) -> dict:
    """Mark messages read so they leave your inbox. Idempotent."""
    me = _me(ctx)
    n = store.ack(_conn, me, message_ids)
    log.info("%s ack %s", me, n)
    return {"acked": n}


@mcp.tool()
async def thread(thread_id: int, ctx: Context = None) -> dict:
    """Every message in a thread, oldest first, as {"messages": [...]}. Linear view."""
    _me(ctx)
    return {"messages": store.thread(_conn, thread_id)}


@mcp.tool()
async def trace(message_id: int, ctx: Context = None) -> dict:
    """Track back how we got to a message: its ancestor sub-DAG as
    {"messages": [...], "edges": [[parent, child], ...]}, forks and re-links included."""
    _me(ctx)
    return store.trace(_conn, message_id)


@mcp.tool()
async def graph(thread_id: int, ctx: Context = None) -> dict:
    """The whole web of a thread as {"messages": [...], "edges": [[parent, child], ...]}."""
    _me(ctx)
    return store.graph(_conn, thread_id)


@mcp.tool()
async def presence(ctx: Context = None) -> dict:
    """Everyone on the bus as {"agents": [...]} -- each with its url, live (recent
    heartbeat), and connected (a wake.py is attached right now)."""
    _me(ctx)
    agents = store.presence(_conn)
    for a in agents:
        a["connected"] = bool(_waiters.get(a["name"]))
    return {"agents": agents}


@mcp.tool()
async def leave(ctx: Context = None) -> str:
    """Sign off the bus for this session."""
    me = _me(ctx)
    store.leave(_conn, me)
    log.info("%s left", me)
    return f"left: {me}"


# ---- wake plane (WebSocket) --------------------------------------------------

def _wake_url_from(url):
    # http://host:port[/...] -> ws://host:port/wake  (https -> wss)
    base = (url or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not base:
        return ""
    # strip any path (e.g. /mcp), keep scheme://host:port
    scheme, _, rest = base.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/wake"


async def wake_ws(ws: WebSocket):
    # Accept FIRST, then validate: Starlette turns any close() issued before accept()
    # into a status-less HTTP 403, which hides why we rejected. By accepting we can send
    # a readable {"error": ...} frame the client (wake.py) prints, so an operator can
    # tell a bad token from a missing name.
    await ws.accept()
    token = ws.query_params.get("token")
    if TOKEN and token != TOKEN:
        await ws.send_json({"error": "bad_token", "detail": "token missing or wrong"})
        await ws.close(code=4401)
        log.warning("wake rejected: bad_token (name=%s)", ws.query_params.get("name") or "-")
        return
    name = ws.query_params.get("name")
    if not name:
        await ws.send_json({"error": "missing_name", "detail": "?name=<agent> is required"})
        await ws.close(code=4400)
        log.warning("wake rejected: missing_name")
        return
    _seen(name)
    log.info("%s wake connected", name)
    q: asyncio.Queue = asyncio.Queue()
    _waiters.setdefault(name, set()).add(q)
    try:
        # Ring once if mail is already waiting at connect, so a just-attached client does
        # not miss it. We do NOT re-ring on still-unacked backlog -- only on new arrivals.
        # (Re-ringing pending mail is what made a reconnecting client storm on itself.)
        backlog = store.inbox(_conn, name)
        if backlog:
            await ws.send_json({"wake": True, "reason": "backlog", "unread": len(backlog)})
            log.info("%s wake ring (backlog %s)", name, len(backlog))
        # Then STAY connected and push one frame per new message until the client drops.
        # Persistent on purpose: a sidecar holds a single socket -- so presence shows
        # connected:true and delivery is instant -- instead of reconnecting per ring, which
        # storms whenever backlog is unacked and never holds a connection.
        while True:
            recv = asyncio.create_task(ws.receive_text())
            woke = asyncio.create_task(q.get())
            done, pending = await asyncio.wait({recv, woke}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # Retrieve each task's result/exception so none is "never retrieved": a client
            # that drops while parked makes recv raise WebSocketDisconnect inside its task.
            for t in (recv, woke):
                with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await t
            if recv in done:  # client went away
                break
            # A notify fired. Coalesce any other queued notifies into this one ring.
            while not q.empty():
                q.get_nowait()
            n = len(store.inbox(_conn, name))
            await ws.send_json({"wake": True, "reason": "message", "unread": n})
            log.info("%s wake ring (%s unread)", name, n)
    except WebSocketDisconnect:
        pass
    finally:
        bucket = _waiters.get(name)
        if bucket:
            bucket.discard(q)
            if not bucket:
                _waiters.pop(name, None)


# ---- app + auth + run --------------------------------------------------------

class BearerAuth(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if TOKEN and request.url.path.startswith("/mcp"):
            if request.headers.get("authorization") != f"Bearer {TOKEN}":
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


async def health(_request):
    return PlainTextResponse("ok")


async def version_http(_request):
    return PlainTextResponse(__version__)


async def usage_http(_request):
    return PlainTextResponse(USAGE)


# ---- web terminal: token+role form -> xterm.js <-> pty running `agent <role>` ----

WEBUI = """<!doctype html><html><head><meta charset="utf-8"><title>Reveille</title>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css">
<style>
 html,body{margin:0;height:100%;background:#000;color:#ddd;font-family:monospace}
 #login{padding:2rem;max-width:340px}
 h2{margin:.2rem 0 1rem}
 input{width:100%;box-sizing:border-box;padding:.5rem;margin:.3rem 0;background:#111;color:#ddd;border:1px solid #444;font-family:monospace}
 button{margin-top:.6rem;padding:.5rem 1.2rem;background:#2a2;color:#000;border:0;cursor:pointer;font-weight:bold}
 #err{color:#f66;min-height:1em}
 #term{position:fixed;inset:0;display:none;padding:4px}
</style></head><body>
<div id="login">
 <h2>Reveille</h2>
 <input id="token" type="password" placeholder="token" autofocus>
 <input id="role" placeholder="role (e.g. roc-api-dev)">
 <button onclick="go()">connect</button>
 <p id="err"></p>
</div>
<div id="term"></div>
<script src="https://cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/xterm-addon-fit@0.8.0/lib/xterm-addon-fit.min.js"></script>
<script>
function go(){
 var token=document.getElementById('token').value;
 var role=document.getElementById('role').value.trim();
 if(!role){document.getElementById('err').textContent='role required';return;}
 var proto=location.protocol==='https:'?'wss':'ws';
 var ws=new WebSocket(proto+'://'+location.host+'/term?token='+encodeURIComponent(token)+'&role='+encodeURIComponent(role));
 ws.binaryType='arraybuffer';
 var term=new Terminal({cursorBlink:true,fontSize:14,fontFamily:'monospace'});
 var fit=new FitAddon.FitAddon();term.loadAddon(fit);
 ws.onopen=function(){
   document.getElementById('login').style.display='none';
   var el=document.getElementById('term');el.style.display='block';
   term.open(el);fit.fit();
   var sz=function(){ws.send('\\x00'+term.cols+'x'+term.rows);};
   sz();window.addEventListener('resize',function(){fit.fit();sz();});
   term.onData(function(d){ws.send(d);});
   term.focus();
 };
 ws.onmessage=function(e){term.write(typeof e.data==='string'?e.data:new Uint8Array(e.data));};
 ws.onclose=function(e){term.write('\\r\\n[disconnected '+e.code+']\\r\\n');};
 ws.onerror=function(){document.getElementById('err').textContent='connection failed';};
}
document.getElementById('token').addEventListener('keydown',function(e){if(e.key==='Enter')document.getElementById('role').focus();});
document.getElementById('role').addEventListener('keydown',function(e){if(e.key==='Enter')go();});
</script></body></html>
"""


async def ui_http(_request):
    return HTMLResponse(WEBUI)


def _winsize(fd, cols, rows):
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


async def term_ws(ws: WebSocket):
    """Browser terminal: spawn `agent <role>` on a pty and bridge it to xterm.js. The agent
    runs in its own tmux session, so closing the tab detaches (session + claude keep running)
    and reconnecting re-attaches (agent uses tmux new-session -A)."""
    await ws.accept()
    token = ws.query_params.get("token") or ""
    role = re.sub(r"[^A-Za-z0-9_-]", "", ws.query_params.get("role") or "")[:64]
    if TOKEN and token != TOKEN:
        await ws.send_text("\r\nauth failed\r\n")
        await ws.close()
        return
    if not role:
        await ws.send_text("\r\nrole required\r\n")
        await ws.close()
        return

    master, slave = pty.openpty()
    env = {**os.environ, "AGENT_ROLE": role, "AGENTBUS_TOKEN": token,
           "AGENTBUS_URL": f"http://127.0.0.1:{os.environ.get('AGENTBUS_PORT', '8765')}",
           "TERM": "xterm-256color"}
    proc = await asyncio.create_subprocess_exec(
        AGENT_BIN, role, stdin=slave, stdout=slave, stderr=slave,
        start_new_session=True, env=env)
    os.close(slave)
    loop = asyncio.get_event_loop()
    log.info("term open role=%s pid=%s", role, proc.pid)

    async def pump_out():  # pty -> browser, in order
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master, 65536)
                if not data:
                    break
                await ws.send_bytes(data)
        except Exception:
            pass
        with contextlib.suppress(Exception):
            await ws.close()

    out = asyncio.create_task(pump_out())
    try:
        while True:
            msg = await ws.receive_text()
            if msg[:1] == "\x00":                      # resize control: "\x00<cols>x<rows>"
                with contextlib.suppress(ValueError):
                    c, r = msg[1:].split("x")
                    _winsize(master, int(c), int(r))
            else:
                os.write(master, msg.encode())
    except Exception:
        pass
    finally:
        out.cancel()
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()                           # detach the tmux client; session persists
        with contextlib.suppress(OSError):
            os.close(master)
        log.info("term close role=%s", role)


def build_app():
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            yield

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/version", version_http),
            Route("/usage", usage_http),
            Route("/ui", ui_http),
            WebSocketRoute("/wake", wake_ws),
            WebSocketRoute("/term", term_ws),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(BearerAuth)],
        lifespan=lifespan,
    )


def _setup_logging():
    # Own handler + no propagation, so uvicorn's logging config can't silence us.
    # Level via AGENTBUS_LOG (default INFO). Lines show: time, client name, op, thread/id.
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s agentbus %(message)s", "%H:%M:%S"))
    log.handlers[:] = [h]
    log.setLevel(os.environ.get("AGENTBUS_LOG", "INFO").upper())
    log.propagate = False
    logging.getLogger("mcp").setLevel(logging.WARNING)  # drop per-request "Processing request" noise


def main():
    global _conn
    import uvicorn
    _setup_logging()
    root = os.environ.get("CLAUDE_AGENT_BUS") or os.path.expanduser("~/.claude/agent-bus")
    db = os.environ.get("AGENTBUS_DB") or os.path.join(root, "broker.db")
    _conn = store.connect(db)
    host = os.environ.get("AGENTBUS_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENTBUS_PORT", "8765"))
    log.info("daemon on %s:%s db=%s auth=%s", host, port, db, "token" if TOKEN else "OPEN")
    uvicorn.run(build_app(), host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
