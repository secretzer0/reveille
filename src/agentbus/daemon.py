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

Wake: a session, while asleep between turns, runs wake.py -- a tiny WS client that
connects with its name, blocks on the socket (0 tokens), and exits when this daemon
pushes a frame. The daemon pushes the instant a message for that agent is sent. The
session wakes, pulls its mail over MCP, acts, re-arms wake.py. The exit-to-wake shape
of a file doorbell, but pushed over the network instead of an inotify event.
"""
import asyncio
import contextlib
import logging
import os

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from agentbus import store

TOKEN = os.environ.get("AGENTBUS_TOKEN") or None  # None = open (trusted LAN)

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
    backlog. Returns {name, wake_url, unread} -- arm wake.py on wake_url with --name name."""
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
        # Already have mail? ring immediately so a just-armed client doesn't miss it.
        backlog = store.inbox(_conn, name)
        if backlog:
            await ws.send_json({"wake": True, "reason": "backlog", "unread": len(backlog)})
            log.info("%s wake ring (backlog %s)", name, len(backlog))
            return
        # Wait for either a notify (mail arrived) or the client going away.
        recv = asyncio.create_task(ws.receive_text())
        woke = asyncio.create_task(q.get())
        done, pending = await asyncio.wait({recv, woke}, return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        if woke in done and not recv.done():
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


def build_app():
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            yield

    return Starlette(
        routes=[
            Route("/health", health),
            WebSocketRoute("/wake", wake_ws),
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
