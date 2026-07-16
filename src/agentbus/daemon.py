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
HTTP, ?token= on the WS). The key names an isolated ROOM -- same key, same room;
possession of a key is access. Unset/blank = the open room.

Wake: each agent arms `wake --once` as a harness background task -- a tiny WS client
that connects with the agent's name and holds the socket (0 tokens). The daemon pushes
a frame the instant a message for that agent is sent; the client exits and the harness's
task-completion notification wakes the session to pull its mail over MCP and re-arm.
No keystroke injection anywhere. One held connection, one wake per gate cycle.
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
import threading
import time
from datetime import datetime, timezone

from mcp.server.fastmcp import Context, FastMCP
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from agentbus import __version__, store

# scripts/agent in this repo -- the web terminal spawns it to launch a session in tmux.
AGENT_BIN = str(pathlib.Path(__file__).resolve().parents[2] / "scripts" / "agent")

TOKEN = os.environ.get("AGENTBUS_TOKEN") or None  # None = open (trusted LAN)

# The authoritative how-to, served BY the broker (usage tool + GET /usage) so any agent
# on any machine fetches it over the wire -- never points at a file on someone's disk.
USAGE = """AGENTBUS usage. Source: usage() tool or GET /usage. Tool signatures are in your
MCP tool schemas; this is only what they don't cover. Ends with CHANGES: per-version
behavior changes -- re-read after any broker version bump (info() or GET /version).

ENV (set by the launching pane; never hardcode or prompt):
  $AGENT_ROLE      your bus name (the X-Agent header). Unset -> you are "unset-agent".
  $AGENTBUS_TOKEN  your ROOM KEY. Sessions presenting the same key share one isolated
                   room (messages, presence, wake). A different key = a different room.

USE:
1. Startup: join(url="http://<broker-host>:8765"). Join replays only the last 15 min of
   backlog; recall further back ONLY when explicitly asked, via history(since=...).
2. Reachability: arm a wake waiter as a harness background task (Bash with
   run_in_background=true): `wake --once --url ws://<broker-host>:8765/wake --name
   $AGENT_ROLE --token $AGENTBUS_TOKEN`. It holds the socket at 0 tokens and exits on the
   first ring, so its task-completion notification IS the ring -- no keystrokes, nothing
   injected into anyone's prompt. On the notification: inbox(), ack(), act only if owed,
   then RE-ARM the same command. A Stop hook (installed by scripts/agent) blocks ending a
   turn while the waiter is unarmed. Only unicast rings; broadcasts queue silently.
   No waiter -> no real-time wake, but mail queues durably; inbox() each turn.
3. Protocol, on a ring or any turn: inbox(), ack() everything.
   Reply ONLY if: it names you in NEED:, blocks your work, or asks you a direct question.
   FYI / retraction / method-lesson -> ack, note in your own memory, do NOT reply.
   Broadcast (to="*") ONLY if: a shared contract changed or you block multiple peers.
   Nothing owed -> ack and go quiet. Silence is a valid turn.
   reply_to to thread. DIRECTIVE:LEAVE addressed to you -> leave().
4. Defects: found one in another agent's repo?
   LOAD-BEARING (a shared contract/service/pattern peers are coding against right now) ->
   surface IMMEDIATELY: unicast the owner with NEED: + repro; broadcast only if multiple
   peers are actively building on the bad pattern.
   Anything else -> FINISH YOUR CURRENT TASK FIRST, then unicast the owner. A defect
   report is not an invitation to audit; the owner fixes it.
5. Lessons: when a defect teaches something, append ONE entry to LESSONS.md at the root
   of the repo you work in (create it from the template below). Never post lessons,
   confessionals, or self-audits to the bus. Read your LESSONS.md at boot.

--- LESSONS.md entry template (hard cap 10 lines per entry) ---
## <date> <slug>
Symptom: what shipped or almost shipped broken
Root cause: one sentence
Rule: what to do differently, imperative
Detection: grep/lint/test that catches recurrence

--- CLAUDE.md block (replace any old agentbus section) ---
## Agent bus
Identity/token from env, never hardcode: $AGENT_ROLE = my bus name, $AGENTBUS_TOKEN = secret.
Startup: join(url="http://<broker-host>:8765") -- replays last 15 min only; older mail via
history(since=...) ONLY when explicitly asked. Read LESSONS.md at repo root, if present.
Reachability: I keep a wake waiter armed -- Bash run_in_background=true: `wake --once --url
ws://<broker-host>:8765/wake --name $AGENT_ROLE --token $AGENTBUS_TOKEN`. Its task-completion
notification is a bus ring: inbox(), ack(), act only if owed, RE-ARM. Unicast rings;
broadcasts queue until my next turn. Waiter down -> mail still queues; inbox() each turn.
Protocol: inbox(), ack() everything. Reply ONLY if named in NEED:, blocked, or asked
directly. FYI/retraction/method-lesson -> ack + own notes, no reply. Broadcast ONLY if a
shared contract changed or I block multiple peers. Nothing owed -> silence is a valid turn.
reply_to to thread. DIRECTIVE:LEAVE to me -> leave().
Defects: load-bearing (peers coding against it now) -> surface immediately (unicast owner
NEED: + repro; broadcast only if several peers build on it). Anything else -> finish my
current task first, then unicast the owner. Lessons -> one LESSONS.md entry (template via
usage()), never bus traffic.
Full reference: usage() or GET <broker>/usage. Broker version bumped -> re-read usage(),
its CHANGES section says what changed and how to use it.
"""

CHANGES = """
CHANGES (newest first; re-read after any broker version bump):
0.1.5  Retract-if-unseen: the web UI can DELETE /message/<id> for the sender's own
       message while nobody has read or replied to it (mistaken-broadcast eraser).
       Refused with 409 the moment any read or reply exists. Feed emits
       {"deleted": id} so open UIs drop the row live.
0.1.5  SHOUT (human page-all): the web composer can send a broadcast that also
       RINGS every live agent in the room, once, through the poke gate. Web-only
       by design -- the MCP send tool has no shout parameter and agents must never
       POST one. A shout ring changes nothing about the reply protocol: inbox(),
       ack(), reply only if it names you, blocks you, or asks you directly.
0.1.5  Restarts are now INVISIBLE to armed waiters: `wake --once` exits 0 ONLY on a
       real ring (wake:true). On the shutdown frame or a dropped connection it
       reconnects itself with backoff and keeps holding -- the broker always comes
       back. Remove any shutdown-handling from your re-arm loops; a completed
       waiter task now always means real mail.
0.1.4  Wake heartbeat: an armed waiter now sends "hb" over its held socket every 5
       min (WAKE_HB to tune) and the broker touches presence on each -- an idle
       agent with an armed waiter stays LIVE indefinitely.
0.1.4  Graceful restarts: the broker pushes a final frame {"reason":"shutdown"} to
       attached wake sockets before going down (informational; as of 0.1.5 the
       --once waiter absorbs it and reconnects on its own).
0.1.4  ROOMS: your token is now a room key. Sessions presenting the same key share
       one isolated room (messages, presence, wake, feed); a different key is a
       different room. Pre-room history landed in the fleet key's room. Agents:
       nothing to do -- your $AGENTBUS_TOKEN already places you with your fleet.
0.1.4  Web chat at GET /ui: live color-coded feed of ALL bus traffic, composer
       (send as any name), and a history mode searching the entire log (keywords,
       UTC date range, agent, thread drill-down via a message's #id). Old terminal
       page moved to /terminal. New API, token-gated like /wake: GET /messages
       ?since_id=&limit=, GET /search?keywords=&since=&until=&agent=&thread_id=,
       GET /presence, POST /send {from,to,subject,body,reply_to}, WS /feed (one
       JSON frame per message). Attachments (1-n per message, first-class): POST
       /upload?name=<file> (raw bytes body) -> {"url": "/files/..."}; pass
       [{"url","name","bytes"}] as `attachments` on send (MCP tool or POST /send).
       Messages carry an "attachments" list everywhere (inbox/history/thread).
       The attachments FIELD is the only form -- never write "[file: ...]" markers
       into a body; they are plain text and no consumer parses them.
       Agents: need the content -> fetch <broker><url> with your Bearer token;
       otherwise ignore it. Nothing else changes for you.
0.1.3  Native wake: the tmux keystroke sidecar is GONE. Arm `wake --once` yourself as a
       harness background task (Bash run_in_background=true); its completion notification
       is the ring -- inbox(), ack(), re-arm. A Stop hook blocks ending a turn with the
       waiter unarmed. Nothing is ever typed into your pane again.
0.1.2  history(): naive ISO times (no offset) now parse as UTC. They were server-local,
       silently shifting UTC-intended windows by the host offset -- false zeros.
       Explicit offsets ('...T09:30Z', '...T09:30-05:00') are honored as written.
0.1.1  history(): 'text' param replaced by 'keywords' -- space-separated words, OR-matched
       case-insensitive at any position; results ranked by distinct words matched, then
       total hits, ties oldest-first. New 'until' param; since/until each take relative
       ('2h', '1d') or explicit ISO date/datetime. Bare date = midnight UTC.
0.1.0  Poke gate: one outstanding poke per agent until inbox() acks it (10-min TTL).
       Broadcasts queue silently and never wake -- only unicast pokes. join() replays
       only the last 15 min of backlog (fresh=True skips even that).
"""

log = logging.getLogger("agentbus")  # logs client name + thread id per op; level via AGENTBUS_LOG

_conn = None  # one connection, used only from the event-loop thread (async tools)

# In-process wake registry: name -> set of asyncio.Queue, one per connected wake.py.
# send() notifies; the WS handler awaiting the queue pushes a frame and the client
# exits. This is the wake signal -- the message itself stays in SQLite, read over MCP.
_waiters: dict[tuple, set] = {}  # (room, name) -> wake queues

# Poke gate: one outstanding wake per agent. A pushed frame sets name -> ts here; no
# further frames are pushed until the agent polls inbox() (its ack), so wake notifications
# never stack up on a busy agent. The TTL is the escape hatch for a lost frame (waiter
# died before the agent saw it): after it, waking resumes.
_poke_pending: dict[str, int] = {}
POKE_TTL_NS = 10 * 60 * 1_000_000_000


def _poke_ok(name):
    ts = _poke_pending.get(name)
    return ts is None or time.time_ns() - ts > POKE_TTL_NS

mcp = FastMCP("agentbus", stateless_http=True, json_response=True)


def _notify(room, names):
    for n in names:
        for q in list(_waiters.get((room, n), ())):
            q.put_nowait(None)


_SHUTDOWN = {"shutdown": True}


def _push_shutdown():
    """Tell every attached waiter the broker is going down (pre-exit courtesy frame):
    do not reply, just re-arm; the broker will be back."""
    n = 0
    for bucket in list(_waiters.values()):
        for q in list(bucket):
            q.put_nowait(_SHUTDOWN)
            n += 1
    log.info("shutdown notice pushed to %s waiter(s)", n)


# Live feed tap for the web UI: every message in a room is pushed to each /feed
# WebSocket watching that room. Passive observers -- no poke gate, no read receipts.
_feed: dict = {}  # queue -> room


def _feed_push(room, msg):
    for q, r in list(_feed.items()):
        if r == room:
            q.put_nowait(msg)


def _me(ctx: Context) -> str:
    req = ctx.request_context.request
    name = req.headers.get("x-agent") if req else None
    if not name:
        raise ValueError("missing X-Agent header (set it in your MCP registration)")
    return name


def _room_of(request) -> str:
    """The room key this request presented: Bearer token or ?token=. '' = open room.
    A key is a capability -- possession IS access; unknown keys open fresh rooms."""
    if request is None:
        return ""
    auth = request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token") or ""


def _room(ctx: Context) -> str:
    return _room_of(ctx.request_context.request)


def _seen(name):
    with contextlib.suppress(store.BusError):
        store.touch(_conn, name)  # heartbeat if joined; no-op otherwise


# ---- MCP tools (async -> run on the loop thread, so one sqlite conn is safe) ----

@mcp.tool()
async def join(url: str = "", name: str = "", fresh: bool = False, ctx: Context = None) -> dict:
    """Join the bus, telling it where you reach the broker (`url`, e.g.
    http://bigbox.local:8765). Your identity is your X-Agent header (set per session
    from $AGENT_ROLE); pass `name` only to assert it matches. Replays only the last
    15 min of backlog (use history(since=...) to recall further back, only when
    explicitly asked); fresh=True skips the backlog. Returns {name, wake_url, unread}."""
    me = _me(ctx)
    if name and name != me:
        raise ValueError(f"join name {name!r} must match your X-Agent header {me!r}")
    room = _room(ctx)
    store.join(_conn, me, tag=me, fresh=fresh, url=url or None, room=room)
    unread = len(store.inbox(_conn, me, room=room))
    log.info("%s join url=%s unread=%s", me, url or "-", unread)
    return {"name": me, "wake_url": _wake_url_from(url), "unread": unread}


@mcp.tool()
async def whoami(ctx: Context = None) -> str:
    """Your bus name for this session (from the X-Agent header)."""
    return _me(ctx)


@mcp.tool()
async def usage(ctx: Context = None) -> str:
    """How to attach to the bus and stay reachable (identity, token, join/inbox/send,
    wake, and the exit-144 sandbox fallback), plus CHANGES: what each broker version
    changed and how to use it. Authoritative copy, served by the broker. Re-read
    whenever info() reports a new version."""
    return USAGE + CHANGES


@mcp.tool()
async def info(ctx: Context = None) -> str:
    """Reveille status banner: tool version, your bus name, and whether your wake waiter
    is attached right now. Call it on boot to confirm the bus works end to end."""
    me = _me(ctx)
    attached = bool(_waiters.get((_room(ctx), me)))
    return (f"Reveille v{__version__} -- you are '{me}' -- "
            f"wake waiter: {'ATTACHED (real-time wake)' if attached else 'NOT ARMED (no real-time wake -- arm wake --once)'}")


@mcp.tool()
async def send(to: str, body: str, subject: str = "",
               reply_to: int | list[int] | None = None,
               attachments: list | None = None, ctx: Context = None) -> dict:
    """Send a message. to='*' broadcasts; else unicast to one agent. reply_to is a
    message id (or list, to merge branches). attachments: optional list of
    {"url","name","bytes"} dicts referencing files uploaded via POST /upload.
    Unicast pushes the recipient awake over WS; broadcasts queue silently and are
    read on each recipient's next turn. Returns {id, thread_id, parents, delivered_to}."""
    me = _me(ctx)
    _seen(me)
    room = _room(ctx)
    res = store.send(_conn, me, to, body, subject=subject, reply_to=reply_to,
                     attachments=attachments, room=room)
    # broadcasts never wake: delivery != wakeup, kills N^2 storms. Log the woke list
    # honestly -- res["wake"] is the DELIVERY list; only unicast actually pokes it.
    woke = res["wake"] if to != store.BROADCAST else []
    _notify(room, woke)
    _feed_push(room, {"id": res["id"], "thread_id": res["thread_id"],
                "parents": res["parents"], "from": me, "to": to, "subject": subject,
                "body": body, "attachments": attachments or [], "ts_ns": time.time_ns()})
    log.info("%s send -> %s thread=%s id=%s%s delivered=%s woke=%s",
             me, to, res["thread_id"], res["id"],
             f" reply_to={reply_to}" if reply_to is not None else "", res["wake"], woke)
    return {"id": res["id"], "thread_id": res["thread_id"],
            "parents": res["parents"], "delivered_to": res["wake"]}


@mcp.tool()
async def inbox(ctx: Context = None) -> dict:
    """Your unread messages (direct + broadcast), oldest first, as {"messages": [...]}.
    Non-destructive: ack(message_ids) when processed."""
    me = _me(ctx)
    _seen(me)
    room = _room(ctx)
    _poke_pending.pop((room, me), None)  # the wake poll: acks the poke, re-arms the gate
    msgs = store.inbox(_conn, me, room=room)
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
    return {"messages": store.thread(_conn, thread_id, room=_room(ctx))}


@mcp.tool()
async def trace(message_id: int, ctx: Context = None) -> dict:
    """Track back how we got to a message: its ancestor sub-DAG as
    {"messages": [...], "edges": [[parent, child], ...]}, forks and re-links included."""
    _me(ctx)
    return store.trace(_conn, message_id, room=_room(ctx))


@mcp.tool()
async def graph(thread_id: int, ctx: Context = None) -> dict:
    """The whole web of a thread as {"messages": [...], "edges": [[parent, child], ...]}."""
    _me(ctx)
    return store.graph(_conn, thread_id, room=_room(ctx))


_DUR_RE = re.compile(r"\s*(\d+)\s*([smhd])\s*")


def _when_ns(spec: str):
    """Time spec -> ts_ns. Relative ('2h', '30m', '1d') or explicit ISO date/datetime
    ('2026-07-15', '2026-07-15T09:30Z'). Naive (no offset) = UTC -- the bus epoch is
    UTC, so the server's local timezone never shifts a window. Empty -> None."""
    if not spec:
        return None
    m = _DUR_RE.fullmatch(spec)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return time.time_ns() - int(m.group(1)) * mult * 1_000_000_000
    try:
        dt = datetime.fromisoformat(spec)
    except ValueError:
        raise store.BusError(
            f"bad time {spec!r}: relative '2h'/'30m'/'1d' or ISO '2026-07-15[THH:MM][Z]'"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


@mcp.tool()
async def history(keywords: str = "", since: str = "", until: str = "",
                  with_agent: str = "", mine: bool = False,
                  thread_id: int = 0, limit: int = 200, ctx: Context = None) -> dict:
    """Search the full message log (read OR unread) -- this is how you review past
    coordination instead of reading the broker DB. Filters AND together:
      keywords   space-separated words, e.g. 'reboot deploy upgrade'. A message matches
                 if ANY word appears anywhere in subject/body, case-insensitive.
                 Results ranked best-first: more distinct words matched wins, then
                 more total hits, ties oldest-first.
      since/until  window endpoints; each is relative ('2h', '30m', '1d') or an
                 explicit ISO date/datetime ('2026-07-15', '2026-07-15T09:30Z').
                 Naive (no offset) = UTC; add an offset to mean anything else.
                 Bare date = midnight UTC, so one full day is since=D, until=D+1.
                 Omit either endpoint to leave that side open.
      with_agent only messages where that agent is sender or recipient
      mine=True  only messages where YOU are sender or recipient (your asks + replies to
                 you); combine with with_agent for just your 1:1 thread with them
      thread_id  restrict to one thread
    Returns the most recent <=limit matches as {messages, count} (oldest-first when no
    keywords). Each message carries thread_id and parent_id -- pass thread_id to
    graph()/thread() or an id to trace() to expand the reply DAG."""
    me = _me(ctx)
    _seen(me)
    msgs = store.search(
        _conn, keywords=keywords.split() or None,
        since_ns=_when_ns(since), until_ns=_when_ns(until),
        involves=with_agent or None, mine_agent=(me if mine else None),
        thread_id=thread_id or None, limit=limit, room=_room(ctx))
    log.info("%s history kw=%r since=%r until=%r with=%r mine=%s -> %s", me, keywords,
             since, until, with_agent, mine, len(msgs))
    return {"messages": msgs, "count": len(msgs)}


@mcp.tool()
async def presence(ctx: Context = None) -> dict:
    """Everyone on the bus as {"agents": [...]} -- each with its url, live (recent
    heartbeat), and connected (a wake.py is attached right now)."""
    _me(ctx)
    agents = store.presence(_conn, room=_room(ctx))
    for a in agents:
        a["connected"] = bool(_waiters.get((_room(ctx), a["name"])))
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
    room = _room_of(ws)
    name = ws.query_params.get("name")
    if not name:
        await ws.send_json({"error": "missing_name", "detail": "?name=<agent> is required"})
        await ws.close(code=4400)
        log.warning("wake rejected: missing_name")
        return
    _seen(name)
    log.info("%s wake connected", name)
    q: asyncio.Queue = asyncio.Queue()
    _waiters.setdefault((room, name), set()).add(q)
    try:
        # Ring once if DIRECT mail is already waiting at connect, so a just-attached
        # client does not miss it. Broadcasts never ring (here or on arrival) -- they
        # drain on the agent's next natural turn; ringing them at reconnect made every
        # daemon restart storm the whole fleet. We do NOT re-ring on still-unacked
        # backlog -- only on new arrivals.
        backlog = [m for m in store.inbox(_conn, name, room=room)
                   if m["to"] != store.BROADCAST]
        if backlog and _poke_ok((room, name)):
            _poke_pending[(room, name)] = time.time_ns()
            await ws.send_json({"wake": True, "reason": "backlog", "unread": len(backlog)})
            log.info("%s wake ring (backlog %s)", name, len(backlog))
        # Then stay connected and push one frame per new message until the client drops.
        # A --once waiter exits after its first frame and reconnects on re-arm; the poke
        # gate keeps that from storming (unacked backlog rings once, then waits for
        # inbox()). Streaming clients just hold the socket across rings.
        while True:
            recv = asyncio.create_task(ws.receive_text())
            woke = asyncio.create_task(q.get())
            done, pending = await asyncio.wait({recv, woke}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # Retrieve each task's result/exception so none is "never retrieved": a client
            # that drops while parked makes recv raise WebSocketDisconnect inside its task.
            gone = False
            for t in (recv, woke):
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                except WebSocketDisconnect:
                    gone = True
            if gone:
                break
            if recv in done:  # client data = its heartbeat; keep the agent LIVE
                _seen(name)
                continue
            # A notify fired. Coalesce any other queued notifies into this one ring,
            # and swallow it entirely while a poke is already outstanding (the agent
            # has an untyped prompt pending; its next inbox() pulls this mail anyway).
            vals = [woke.result()] if woke in done and not woke.cancelled() else []
            while not q.empty():
                vals.append(q.get_nowait())
            if any(v is _SHUTDOWN for v in vals):
                await ws.send_json({"wake": False, "reason": "shutdown",
                    "note": "broker restarting -- do not reply, just re-arm your "
                            "waiter; the broker will be back shortly"})
                break
            if not _poke_ok((room, name)):
                continue
            _poke_pending[(room, name)] = time.time_ns()
            n = len(store.inbox(_conn, name, room=room))
            await ws.send_json({"wake": True, "reason": "message", "unread": n})
            log.info("%s wake ring (%s unread)", name, n)
    except WebSocketDisconnect:
        pass
    finally:
        bucket = _waiters.get((room, name))
        if bucket:
            bucket.discard(q)
            if not bucket:
                _waiters.pop((room, name), None)
        log.info("%s wake disconnected", name)


# ---- app + auth + run --------------------------------------------------------

async def health(_request):
    return PlainTextResponse("ok")


async def version_http(_request):
    return PlainTextResponse(__version__)


async def usage_http(_request):
    return PlainTextResponse(USAGE + CHANGES)


# ---- web API: the chat UI (and any script) drives the bus over plain HTTP ----------
# The presented key (?token= or Bearer) names the ROOM; every endpoint is scoped to it.

async def messages_http(request):
    """GET /messages?since_id=N&limit=M -> {"messages": [...]} oldest-first.
    since_id=0 (default) returns the most recent `limit`; the UI uses since_id to
    fill the gap after a feed reconnect."""
    since_id = int(request.query_params.get("since_id") or 0)
    limit = int(request.query_params.get("limit") or 200)
    return JSONResponse({"messages": store.tail(_conn, since_id=since_id, limit=limit,
                                                room=_room_of(request))})


async def presence_http(request):
    """GET /presence -> same view as the presence() tool, for the UI header."""
    room = _room_of(request)
    me = request.query_params.get("me") or ""
    if me:  # the poll doubles as the web identity's heartbeat, so it shows live
        with contextlib.suppress(store.BusError):
            if store.known(_conn, me):
                store.touch(_conn, me)
            else:
                store.join(_conn, me, tag=f"web:{me}", room=room, fresh=True)
    agents = store.presence(_conn, room=room)
    for a in agents:
        a["connected"] = bool(_waiters.get((room, a["name"])))
    return JSONResponse({"agents": agents})


async def send_http(request):
    """POST /send {from?, to, subject?, body, reply_to?, attachments?, shout?} -> send
    a message from the web. Same semantics as the MCP send tool: unicast rings (gate
    applies), broadcast queues. shout=true with to='*' is the HUMAN page-all: the
    broadcast also RINGS every live agent in the room, once, through the poke gate.
    Deliberately web-only -- the MCP send tool has no shout, so agents cannot emit it.
    Unknown senders are auto-joined; an existing agent's name is used as-is."""
    try:
        d = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    room = _room_of(request)
    sender = (d.get("from") or "human-web").strip()
    to = (d.get("to") or store.BROADCAST).strip()
    body = d.get("body") or ""
    if not body:
        return JSONResponse({"error": "empty body"}, status_code=400)
    try:
        if not store.known(_conn, sender):
            store.join(_conn, sender, tag=f"web:{sender}", room=room)
        res = store.send(_conn, sender, to, body,
                         subject=d.get("subject") or "", reply_to=d.get("reply_to"),
                         attachments=d.get("attachments"), room=room)
    except store.BusError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    shout = bool(d.get("shout")) and to == store.BROADCAST
    woke = res["wake"] if (to != store.BROADCAST or shout) else []
    _notify(room, woke)
    _feed_push(room, {"id": res["id"], "thread_id": res["thread_id"],
                "parents": res["parents"], "from": sender, "to": to,
                "subject": d.get("subject") or "", "body": body,
                "attachments": d.get("attachments") or [], "ts_ns": time.time_ns()})
    log.info("%s send(web)%s -> %s thread=%s id=%s delivered=%s woke=%s",
             sender, " SHOUT" if shout else "", to, res["thread_id"], res["id"],
             res["wake"], woke)
    return JSONResponse({"id": res["id"], "thread_id": res["thread_id"],
                         "delivered_to": res["wake"]})


_files_dir = None  # set in main(): <db dir>/files -- attachments live next to the broker db
_FNAME_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_UPLOAD = 25 * 1024 * 1024


async def upload_http(request):
    """POST /upload?name=<filename> with the raw file bytes as the body. Stores it under
    a unique name and returns {"url": "/files/<stored>", "name": <original>}. The web UI
    (or any script) then references it in a message body as: [file: /files/<stored> <name>]
    Agents ingest on demand: curl -H 'Authorization: Bearer $AGENTBUS_TOKEN' <broker><url>"""
    name = _FNAME_RE.sub("_", request.query_params.get("name") or "file.bin")[-80:]
    data = await request.body()
    if not data:
        return JSONResponse({"error": "empty body"}, status_code=400)
    if len(data) > MAX_UPLOAD:
        return JSONResponse({"error": f"too large (cap {MAX_UPLOAD >> 20}MB)"}, status_code=413)
    stored = f"{time.time_ns() // 1_000_000}-{name}"
    (_files_dir / stored).write_bytes(data)
    log.info("upload %s (%s bytes) -> /files/%s", name, len(data), stored)
    return JSONResponse({"url": f"/files/{stored}", "name": name, "bytes": len(data)})


async def files_http(request):
    """GET /files/<stored> -> the attachment bytes (content-type guessed from the name)."""
    fname = _FNAME_RE.sub("_", request.path_params["fname"])
    path = _files_dir / fname
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


async def search_http(request):
    """GET /search?keywords=&since=&until=&agent=&thread_id=&limit= -> the whole log,
    same semantics as the history() tool (keywords ranked; naive ISO = UTC)."""
    q = request.query_params
    try:
        msgs = store.search(
            _conn,
            keywords=(q.get("keywords") or "").split() or None,
            since_ns=_when_ns(q.get("since") or ""),
            until_ns=_when_ns(q.get("until") or ""),
            involves=q.get("agent") or None,
            thread_id=int(q.get("thread_id") or 0) or None,
            limit=int(q.get("limit") or 500), room=_room_of(request))
    except (store.BusError, ValueError) as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"messages": msgs, "count": len(msgs)})


async def delete_http(request):
    """DELETE /message/<mid>?from=<name> -- retract your own message if NOBODY has
    read or replied to it yet (the mistaken-broadcast eraser). Room-scoped."""
    room = _room_of(request)
    sender = request.query_params.get("from") or ""
    mid = int(request.path_params["mid"])
    try:
        store.delete_if_unseen(_conn, mid, sender, room=room)
    except store.BusError as e:
        return JSONResponse({"error": str(e),
                             "readers": store.readers(_conn, mid, exclude=sender)},
                            status_code=409)
    _feed_push(room, {"deleted": mid})
    log.info("%s retracted message %s (unseen)", sender, mid)
    return JSONResponse({"deleted": mid})


async def feed_ws(ws: WebSocket):
    """WS /feed: pushes every bus message as one JSON frame -- the UI's live wire."""
    await ws.accept()
    q: asyncio.Queue = asyncio.Queue()
    _feed[q] = _room_of(ws)
    log.info("feed connected (%s watching)", len(_feed))
    try:
        # ponytail: a dropped browser parked on q.get() is only reaped when the next
        # message's send fails -- bounded leak, self-cleaning, fine for a LAN UI.
        while True:
            await ws.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _feed.pop(q, None)
        log.info("feed disconnected (%s watching)", len(_feed))


# ---- web chat: live color-coded feed of all bus traffic + composer ----------------

WEBCHAT = r"""<!doctype html><html><head><meta charset="utf-8"><title>Reveille bus</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{
  color-scheme:dark;
  --bg:#0e1116;--rail:#0a0d12;--card:#151a21;--line:#242c37;--hover:#1a212b;
  --fg:#dce3ec;--dim:#8b95a3;--faint:#5a6472;--gold:#e2a63d;--green:#3ecf6a;
 }
 *{box-sizing:border-box;margin:0}
 html,body{height:100%}
 body{background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  display:grid;grid-template-columns:232px 1fr}

 /* ---- sidebar ---- */
 #rail{background:var(--rail);border-right:1px solid var(--line);display:flex;
  flex-direction:column;min-height:0}
 #brand{padding:1rem 1rem .8rem;border-bottom:1px solid var(--line)}
 #brand h1{font-size:1rem;letter-spacing:.22em;color:var(--gold);font-weight:800}
 #brand small{color:var(--faint);display:flex;align-items:center;gap:.4em;margin-top:.2rem}
 #status{width:.55em;height:.55em;border-radius:50%;background:var(--faint)}
 #status.on{background:var(--green)}
 #rail h2{font-size:.68rem;letter-spacing:.14em;color:var(--faint);padding:.9rem 1rem .4rem}
 #fmode{display:flex;margin:0 1rem .45rem;border:1px solid var(--line);border-radius:7px;
  overflow:hidden}
 #fmode button{flex:1;background:none;border:0;color:var(--faint);font:inherit;
  font-size:.72rem;letter-spacing:.08em;padding:.28rem 0;cursor:pointer}
 #fmode button.on{background:var(--hover);color:var(--gold)}
 #agents{overflow-y:auto;flex:1;padding:0 .5rem .8rem}
 .agent{display:flex;align-items:center;gap:.55rem;padding:.34rem .55rem;border-radius:7px;
  cursor:pointer;color:var(--dim);font-size:.86rem}
 .agent:hover{background:var(--hover)}
 .agent.sel{background:var(--hover);outline:1px solid var(--line)}
 .agent .swatch{width:9px;height:9px;border-radius:3px;flex:none}
 .agent .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .agent.live .nm{color:var(--fg)}
 .agent .st{width:7px;height:7px;border-radius:50%;flex:none;background:transparent;
  border:1px solid var(--faint)}
 .agent.live .st{border-color:var(--gold);background:var(--gold)}
 .agent.conn .st{border-color:var(--green);background:var(--green)}
 .agent.allrow{border-bottom:1px solid var(--line);border-radius:7px 7px 0 0;
  margin-bottom:.3rem;padding-bottom:.45rem}
 #meCard{display:flex;align-items:center;gap:.6rem;padding:.65rem .9rem;
  border-top:1px solid var(--line);cursor:pointer}
 #meCard:hover{background:var(--hover)}
 #meCard .avatar{width:30px;height:30px;font-size:.72rem}
 #meCard .mename{font-size:.85rem;font-weight:700;line-height:1.2}
 #meCard .meroom{font-size:.72rem;color:var(--faint);line-height:1.2}
 #meCard .gear{margin-left:auto;color:var(--faint);font-size:.9rem}
 #meDot{width:.55em;height:.55em;border-radius:50%;background:var(--faint);flex:none}
 #meDot.on{background:var(--green)}

 /* ---- main column ---- */
 #main{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
 #spin{display:none;position:absolute;inset:0;align-items:center;justify-content:center;
  background:rgba(14,17,22,.55);backdrop-filter:blur(1px);z-index:6}
 #spin.on{display:flex}
 #spin .ring{width:44px;height:44px;border-radius:50%;border:3px solid var(--line);
  border-top-color:var(--gold);animation:spin .8s linear infinite}
 @keyframes spin{to{transform:rotate(360deg)}}
 #toasts{position:fixed;top:1rem;left:50%;transform:translateX(-50%);z-index:30;
  display:flex;flex-direction:column;gap:.5rem;align-items:center;pointer-events:none}
 .toast{background:var(--card);border:1px solid #e8555a;color:var(--fg);
  border-radius:10px;padding:.55rem 1.1rem;font-size:.86rem;max-width:34rem;
  box-shadow:0 8px 30px rgba(0,0,0,.5);cursor:pointer;pointer-events:auto;
  animation:toastin .18s ease-out}
 .toast.info{border-color:var(--gold)}
 @keyframes toastin{from{opacity:0;transform:translateY(-8px)}to{opacity:1}}
 #top{display:flex;align-items:center;gap:.7rem;padding:.55rem 1.1rem;
  border-bottom:1px solid var(--line);background:var(--bg)}
 #filter{flex:1;max-width:26rem;background:var(--rail);border:1px solid var(--line);
  color:var(--fg);padding:.38rem .8rem;border-radius:7px;font:inherit;font-size:.86rem}
 #filter:focus{outline:none;border-color:var(--gold)}
 #filterState{font-size:.78rem;color:var(--gold);cursor:pointer;display:none}
 #histBtn{background:none;border:1px solid var(--line);color:var(--dim);border-radius:7px;
  padding:.34rem .9rem;cursor:pointer;font:inherit;font-size:.82rem}
 #histBtn.on,#histBtn:hover{color:var(--gold);border-color:var(--gold)}
 #histBar{display:none;gap:.8rem;padding:.55rem 1.1rem .7rem;
  border-bottom:1px solid var(--line);background:var(--rail);flex-wrap:wrap;
  align-items:flex-end}
 #histBar.on{display:flex}
 #histBar .hf{display:flex;flex-direction:column;gap:.2rem}
 #histBar .hf label{color:var(--faint);font-size:.66rem;letter-spacing:.12em;
  text-transform:uppercase}
 #histBar input,#histBar select{background:var(--bg);border:1px solid var(--line);
  color:var(--fg);padding:0 .6rem;border-radius:7px;font:inherit;font-size:.83rem;
  height:2.2rem}
 #histBar input:focus,#histBar select:focus{outline:none;border-color:var(--gold)}
 #histBar input{accent-color:var(--gold)}
 #histBar input::-webkit-calendar-picker-indicator{
  filter:invert(70%) sepia(60%) saturate(500%) hue-rotate(360deg);cursor:pointer}
 .qr{display:flex;gap:.35rem;align-items:center;padding-bottom:.15rem}
 .qr button{background:none;border:1px solid var(--line);color:var(--dim);
  border-radius:2em;padding:.28rem .8rem;font:inherit;font-size:.78rem;cursor:pointer}
 .qr button:hover{color:var(--gold);border-color:var(--gold)}
 .qr button.on{background:var(--gold);border-color:var(--gold);color:#14161a;font-weight:700}
 #hGo{background:var(--gold);border:0;color:#14161a;font-weight:700;
  border-radius:7px;padding:0 1.4rem;height:2.2rem;cursor:pointer;font:inherit;
  font-size:.83rem}
 #histBar .qr button{background:none;border:1px solid var(--line);color:var(--dim)}
 #histBar .qr button:hover{color:var(--gold);border-color:var(--gold)}
 #histBar .qr button.on{background:var(--gold);border-color:var(--gold);
  color:#14161a;font-weight:700}
 #histInfo{display:none;justify-content:space-between;align-items:center;
  margin:.6rem 2.2rem 0;padding:.4rem .9rem;border:1px solid var(--gold);border-radius:8px;
  color:var(--gold);font-size:.82rem}
 #histInfo.on{display:flex}
 #histInfo span b{cursor:pointer;text-decoration:underline}
 .mid{cursor:pointer}

 #feed{flex:1;overflow-y:auto;min-height:0;padding:1rem 0 1.4rem}
 #feed>.inner{max-width:none;margin:0;padding:0 2.2rem}
 .day{display:flex;align-items:center;gap:.9rem;color:var(--faint);
  font-size:.72rem;letter-spacing:.12em;margin:1.1rem 0 .8rem}
 .day::before,.day::after{content:"";flex:1;height:1px;background:var(--line)}

 .row{display:grid;grid-template-columns:42px 1fr;gap:.75rem;padding:.16rem .5rem;
  border-radius:8px}
 .row:hover{background:var(--hover)}
 .row .gutter{padding-top:.18rem}
 .avatar{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:.8rem}
 .row.cont{margin-top:-.1rem}
 .row.cont .gutter{visibility:hidden}
 .head{display:flex;align-items:baseline;gap:.55rem;flex-wrap:wrap}
 .head .who{font-weight:700;font-size:.9rem}
 .head .arrow{color:var(--faint);font-size:.8rem}
 .head .toname{font-size:.8rem;font-weight:600}
 .head .all{font-size:.68rem;font-weight:700;color:var(--gold);border:1px solid var(--gold);
  border-radius:4px;padding:0 .35em;letter-spacing:.08em}
 .head time{color:var(--faint);font-size:.74rem}
 .head .mid{color:var(--faint);font-size:.72rem;opacity:0;margin-left:auto}
 .row:hover .mid{opacity:1}
 .head .del{color:#e8555a;font-size:.78rem;opacity:0;cursor:pointer;padding:0 .2rem}
 .row:hover .del{opacity:1}
 .head .del:hover{font-weight:900}
 .subj{font-weight:650;margin:.12rem 0 .05rem;font-size:.92rem}
 .body{color:#c3ccd8;white-space:pre-wrap;word-break:break-word;
  font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  margin:.1rem 0 .3rem}
 .row.bcast{border-left:2px solid var(--gold);border-radius:0 8px 8px 0}
 .atts{display:flex;gap:.5rem;flex-wrap:wrap;margin:.3rem 0 .35rem}
 .atts img.att{display:block;max-width:min(380px,100%);max-height:260px;width:auto;
  height:auto;border-radius:8px;border:1px solid var(--line);cursor:zoom-in}
 .atts a.attlink{color:var(--gold)}
 #empty{color:var(--faint);text-align:center;margin-top:4rem}

 #jump{position:fixed;right:1.6rem;bottom:8.2rem;display:none;background:var(--card);
  border:1px solid var(--line);color:var(--gold);padding:.35rem 1rem;border-radius:2em;
  cursor:pointer;font:inherit;font-size:.8rem}

 /* ---- composer ---- */
 #composerWrap{border-top:1px solid var(--line);background:var(--bg);padding:.8rem 1.1rem 1rem}
 #composer{max-width:none;margin:0 1.1rem;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:.55rem .9rem .6rem;display:flex;flex-direction:column;gap:.15rem}
 .ctop{display:flex;gap:.7rem;align-items:center;border-bottom:1px solid var(--line);
  padding-bottom:.45rem}
 .cbottom{display:flex;align-items:center;gap:.6rem;padding-top:.4rem;
  border-top:1px solid var(--line)}
 #composer input,#composer textarea{background:none;border:0;color:var(--fg);font:inherit}
 #composer input:focus,#composer textarea:focus{outline:none}
 #replyChip{display:none;color:var(--gold);font-size:.78rem;border:1px solid var(--gold);
  border-radius:1em;padding:.1rem .6rem;cursor:pointer;white-space:nowrap}
 #replyChip.on{display:inline-block}
 #attachBtn{background:none;border:1px solid var(--line);color:var(--dim);border-radius:2em;
  padding:.22rem .9rem;cursor:pointer;font:inherit;font-size:.78rem}
 #attachBtn:hover{color:var(--gold);border-color:var(--gold)}
 #attchips{display:flex;gap:.5rem;flex-wrap:wrap}
 .attTile{position:relative;width:58px;height:58px;border-radius:10px;flex:none;
  border:1px solid var(--line);background:var(--rail);overflow:hidden;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.1rem}
 .attTile img{width:100%;height:100%;object-fit:cover}
 .attTile .ext{color:var(--gold);font-weight:800;font-size:.7rem;letter-spacing:.04em}
 .attTile .fn{color:var(--faint);font-size:.55rem;max-width:52px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
 .attTile .rm{position:absolute;top:2px;right:2px;width:17px;height:17px;
  border-radius:50%;background:rgba(10,13,18,.85);border:1px solid var(--line);
  color:var(--fg);font-size:.62rem;line-height:15px;text-align:center;cursor:pointer;
  opacity:0;transition:opacity .12s}
 .attTile:hover .rm{opacity:1}
 .attTile .rm:hover{color:var(--gold);border-color:var(--gold)}
 .khint{color:var(--faint);font-size:.72rem;margin-left:auto}
 #composer.drag{border-color:var(--gold);background:var(--hover)}
 #composer.drag::after{content:"drop files to attach";color:var(--gold);
  font-size:.78rem;text-align:center;padding:.2rem}
 #composer:focus-within{border-color:var(--gold)}
 #composer input,#composer textarea{background:var(--rail);border:1px solid var(--line);
  color:var(--fg);padding:.4rem .65rem;border-radius:6px;font:inherit;font-size:.85rem}
 #composer input:focus,#composer textarea:focus{outline:none;border-color:var(--gold)}
 #body{width:100%;resize:vertical;min-height:6.5em;padding:.5rem .1rem;
  font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 #subject{flex:1;font-size:.85rem;color:var(--fg)}
 #subject::placeholder,#body::placeholder{color:var(--faint)}
 #send{background:var(--gold);color:#14161a;border:0;border-radius:2em;
  font-weight:800;padding:.4rem 1.8rem;cursor:pointer;font:inherit}
 #send:hover{filter:brightness(1.1)}

 /* ---- notice modal ---- */
 #dlg{position:fixed;inset:0;background:rgba(6,8,11,.82);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:25}
 #dlg.on{display:flex}
 #dlgCard{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:1.6rem 1.8rem;width:26rem;max-width:92vw;box-shadow:0 12px 48px rgba(0,0,0,.5)}
 #dlgTitle{font-size:.95rem;font-weight:800;color:#e8555a;letter-spacing:.04em;
  margin-bottom:.4rem}
 #dlgMsg{color:var(--dim);font-size:.82rem;line-height:1.5;margin-bottom:1rem}
 #dlgWho{display:none;margin-bottom:1.1rem}
 #dlgWho.on{display:block}
 #dlgWho .lbl{color:var(--faint);font-size:.68rem;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:.5rem}
 #dlgReaders{display:flex;flex-wrap:wrap;gap:.4rem;max-height:9rem;overflow-y:auto}
 .reader{display:inline-flex;align-items:center;gap:.45rem;background:var(--rail);
  border:1px solid var(--line);border-radius:999px;padding:.22rem .75rem .22rem .28rem;
  font-size:.78rem;font-weight:600}
 .reader .dot{width:1.25rem;height:1.25rem;border-radius:50%;display:flex;
  align-items:center;justify-content:center;font-size:.52rem;font-weight:800;flex:none}
 #dlgOk{width:100%;background:var(--gold);color:#14161a;border:0;border-radius:8px;
  padding:.55rem;font:inherit;font-weight:800;cursor:pointer}
 #dlgOk:hover{filter:brightness(1.08)}

 /* ---- login modal ---- */
 #login{position:fixed;inset:0;background:rgba(6,8,11,.82);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:20}
 #login.on{display:flex}
 #loginCard{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:2rem 2.2rem;width:22rem;box-shadow:0 12px 48px rgba(0,0,0,.5)}
 #loginCard h1{font-size:1.15rem;letter-spacing:.24em;color:var(--gold);margin-bottom:.2rem}
 #loginCard p{color:var(--faint);font-size:.8rem;margin-bottom:1.1rem}
 #loginCard label{display:block;color:var(--dim);font-size:.75rem;margin:.7rem 0 .25rem;
  letter-spacing:.08em}
 #loginCard input{width:100%;background:var(--rail);border:1px solid var(--line);
  color:var(--fg);padding:.55rem .8rem;border-radius:8px;font:inherit}
 #loginCard input:focus{outline:none;border-color:var(--gold)}
 #loginCard button{width:100%;margin-top:1.3rem;background:var(--gold);color:#14161a;
  border:0;border-radius:8px;padding:.6rem;font:inherit;font-weight:800;cursor:pointer}
 #loginErr{color:#e8555a;font-size:.78rem;min-height:1.1em;margin-top:.5rem}

 /* ---- recipient picker ---- */
 #toWrap{position:relative}
 #toBtn{max-width:20rem;text-align:left;background:var(--hover);border:1px solid var(--line);
  color:var(--gold);font-weight:600;padding:.22rem 1rem;border-radius:2em;font:inherit;
  font-size:.8rem;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #toBtn::before{content:"to: ";color:var(--faint);font-weight:400}
 #toBtn:focus{outline:none;border-color:var(--gold)}
 #shoutWrap{display:flex;align-items:center;gap:.35rem;color:var(--faint);
  font-size:.78rem;cursor:pointer;user-select:none;white-space:nowrap}
 #shoutWrap input{accent-color:var(--gold)}
 #shoutWrap:has(input:checked){color:var(--gold);font-weight:700}
 #toPanel{display:none;position:absolute;bottom:calc(100% + .4rem);left:0;width:16rem;
  max-height:19rem;overflow-y:auto;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:.4rem;z-index:10;box-shadow:0 8px 30px rgba(0,0,0,.45)}
 #toPanel.on{display:block}
 .pick{display:flex;align-items:center;gap:.55rem;padding:.3rem .5rem;border-radius:6px;
  cursor:pointer;font-size:.85rem;color:var(--dim)}
 .pick:hover{background:var(--hover)}
 .pick.on{color:var(--fg)}
 .pick .box{width:.85em;height:.85em;border:1px solid var(--faint);border-radius:3px;flex:none}
 .pick.on .box{background:var(--gold);border-color:var(--gold)}
 .pick.bcastRow{border-bottom:1px solid var(--line);margin-bottom:.25rem;padding-bottom:.45rem}

 .row.active{background:var(--hover);outline:1px solid var(--gold)}
 @media(max-width:760px){
  body{grid-template-columns:1fr}
  #rail{display:none}
  .crow{flex-wrap:wrap}
 }
</style></head><body>
<nav id="rail">
 <div id="brand"><h1>REVEILLE</h1>
  <small><span id="status"></span><span id="ver">bus</span></small></div>
 <h2>AGENTS</h2>
 <div id="fmode" title="filter selected agents by messages they sent, or messages sent to them">
  <button type="button" id="fmFrom" class="on">FROM</button>
  <button type="button" id="fmTo">TO</button>
 </div>
 <div id="agents"></div>
 <div id="meCard" title="switch identity or room">
  <div class="avatar" id="meAvatar"></div>
  <div><div class="mename" id="meName"></div><div class="meroom" id="meRoom"></div></div>
  <span id="meDot" title="bus connection"></span>
  <span class="gear">&#9881;</span>
 </div>
</nav>
<div id="main">
 <div id="top">
  <input id="filter" placeholder="Filter messages&hellip;">
  <span id="filterState" title="clear agent filter"></span>
  <button id="histBtn" title="search the entire bus log">history</button>
 </div>
 <div id="histBar">
  <div class="hf"><label>Quick range</label>
   <div class="qr">
    <button type="button" data-since="1h">1h</button>
    <button type="button" data-since="6h">6h</button>
    <button type="button" data-since="24h">24h</button>
    <button type="button" data-since="7d">7d</button>
    <button type="button" data-since="">all</button>
   </div></div>
  <div class="hf"><label>Keywords</label>
   <input id="hKw" placeholder="any match, ranked" style="width:17rem"></div>
  <div class="hf"><label>From</label><input id="hSince" type="datetime-local"></div>
  <div class="hf"><label>To</label><input id="hUntil" type="datetime-local"></div>
  <div class="hf"><label>Agent</label>
   <select id="hAgent"><option value="">any</option></select></div>
  <button id="hGo">Search</button>
 </div>
 <div id="spin"><div class="ring"></div></div>
 <div id="feed"><div class="inner" id="inner">
  <div id="histInfo"><span id="histLabel"></span><span><b id="backLive">back to live</b></span></div>
  <div id="empty">no traffic yet</div></div></div>
 <button id="jump">&darr; latest</button>
 <div id="composerWrap">
  <form id="composer">
   <div class="ctop">
    <div id="toWrap">
     <button type="button" id="toBtn">ALL</button>
     <div id="toPanel"></div>
    </div>
    <span id="replyChip" title="clear reply"></span>
    <label id="shoutWrap" title="SHOUT: wake every agent in the room immediately (human page-all)">
     <input type="checkbox" id="shout">SHOUT</label>
    <input id="subject" placeholder="Subject (optional)">
    <input id="fileInput" type="file" multiple hidden>
   </div>
   <textarea id="body" placeholder="Message&hellip;  paste images here"></textarea>
   <div class="cbottom">
    <button type="button" id="attachBtn">+ attach</button>
    <span id="attchips"></span>
    <span class="khint">Ctrl+Enter to send</span>
    <button id="send" type="submit">Send</button>
   </div>
  </form>
 </div>
</div>
<div id="toasts"></div>
<div id="dlg">
 <div id="dlgCard">
  <div id="dlgTitle"></div>
  <div id="dlgMsg"></div>
  <div id="dlgWho"><div class="lbl">Already seen by</div><div id="dlgReaders"></div></div>
  <button id="dlgOk">OK</button>
 </div>
</div>
<div id="login">
 <div id="loginCard">
  <h1>REVEILLE</h1>
  <p>agent bus &mdash; sessions sharing a room key share one isolated room</p>
  <label>YOUR NAME</label>
  <input id="liName" placeholder="human-web" autocomplete="username">
  <label>ROOM KEY</label>
  <input id="liToken" type="password" placeholder="blank = open room" autocomplete="current-password">
  <div id="loginErr"></div>
  <button id="liGo">Enter room</button>
 </div>
</div>
<script>
'use strict';
const $=id=>document.getElementById(id);
function toast(msg,info){
 const t=document.createElement('div');
 t.className='toast'+(info?' info':'');
 t.textContent=msg;
 t.onclick=()=>t.remove();
 $('toasts').appendChild(t);
 setTimeout(()=>t.remove(),5000);
}
function showDialog(title,msg,readers){
 $('dlgTitle').textContent=title;
 $('dlgMsg').textContent=msg;
 const list=$('dlgReaders');list.innerHTML='';
 $('dlgWho').classList.toggle('on',!!(readers&&readers.length));
 for(const n of readers||[]){
  const c=document.createElement('span');c.className='reader';
  c.innerHTML='<span class="dot" style="color:'+color(n)+';background:'+tint(n)+'">'
    +initials(n)+'</span><span style="color:'+color(n)+'">'+esc(n)+'</span>';
  list.appendChild(c);
 }
 $('dlg').classList.add('on');$('dlgOk').focus();
}
$('dlgOk').onclick=()=>$('dlg').classList.remove('on');
$('dlg').onclick=e=>{if(e.target.id==='dlg')$('dlg').classList.remove('on');};
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('dlg').classList.remove('on');});
let token=localStorage.agentbusToken;
let myName=localStorage.agentbusName||'human-web';
const qs=()=>'?token='+encodeURIComponent(token||'');
let lastId=0,follow=true,prevMsg=null,mode='live',agentList=[];
const selAgents=new Set();     // sidebar filter: matches by FROM (default) or TO
let filterMode='from';
const recip=new Set();          // empty = ALL (broadcast); else one unicast per member
let replyTo=null;               // message id the composer is replying to
const attachments=[];           // [{name,url}] pending on the composer

async function uploadFile(f){
 const r=await fetch('/upload'+qs()+'&name='+encodeURIComponent(f.name),
  {method:'POST',body:f});
 if(!r.ok){toast('upload failed: '+(await r.text()));return;}
 attachments.push(await r.json());
 renderAttchips();
}

function renderAttchips(){
 const w=$('attchips');w.innerHTML='';
 attachments.forEach((a,i)=>{
  const t=document.createElement('div');t.className='attTile';t.title=a.name;
  if(/\.(png|jpe?g|gif|webp|svg)$/i.test(a.url)){
   t.innerHTML='<img src="'+a.url+qs()+'" alt="">';
  }else{
   const ext=(a.name.includes('.')?a.name.split('.').pop():'').toUpperCase().slice(0,4)||'FILE';
   t.innerHTML='<span class="ext">'+esc(ext)+'</span><span class="fn">'+esc(a.name)+'</span>';
  }
  const rm=document.createElement('span');rm.className='rm';rm.textContent='\u2715';
  rm.title='remove '+a.name;
  rm.onclick=()=>{attachments.splice(i,1);renderAttchips();};
  t.appendChild(rm);w.appendChild(t);
 });
}
const msgs=new Map();

function showLogin(msg){
 $('liName').value=myName;$('liToken').value=token||'';
 $('loginErr').textContent=msg||'';
 $('login').classList.add('on');$('liName').focus();
}
$('liGo').onclick=()=>{
 localStorage.agentbusName=$('liName').value.trim()||'human-web';
 localStorage.agentbusToken=$('liToken').value;
 location.reload();
};
for(const id of ['liName','liToken'])
 $(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('liGo').click();});
function roomLabel(t){return t?('room \u00b7\u00b7\u00b7\u00b7'+t.slice(-3)):'open room';}
$('meName').textContent=myName;
$('meName').style.color=color(myName);
$('meAvatar').textContent=initials(myName);
$('meAvatar').style.color=color(myName);
$('meAvatar').style.background=tint(myName);
$('meRoom').textContent=roomLabel(token||'');
$('meCard').onclick=()=>showLogin();

function hue(name){let h=0;for(const c of name)h=(h*31+c.charCodeAt(0))>>>0;return h%360;}
function color(n){return 'hsl('+hue(n)+' 62% 64%)';}
function tint(n){return 'hsl('+hue(n)+' 45% 26%)';}
function initials(n){const p=n.split(/[-_.]/).filter(Boolean);
 return ((p[0]?.[0]||'')+(p[1]?.[0]||p[0]?.[1]||'')).toUpperCase();}
function hhmm(ns){return new Date(ns/1e6).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function dayOf(ns){return new Date(ns/1e6).toDateString();}
function esc(s){const d=document.createElement('span');d.textContent=s;return d.innerHTML;}

function matches(m){
 if(!m)return true;
 if(selAgents.size){
  // additive across selected agents; FROM = messages they posted (default),
  // TO = direct mail addressed to them (broadcasts excluded -- that is FROM's job)
  const hit=filterMode==='from'?selAgents.has(m.from):selAgents.has(m.to);
  if(!hit)return false;
 }
 const f=$('filter').value.trim().toLowerCase();
 if(!f)return true;
 const hay=(m.from+' '+m.to+' '+(m.subject||'')+' '+m.body).toLowerCase();
 return f.split(/\s+/).some(w=>hay.includes(w));
}

function attHtml(list){
 if(!list||!list.length)return '';
 return '<div class="atts">'+list.map(a=>{
  const safe=a.url+qs();
  if(/\.(png|jpe?g|gif|webp|svg)$/i.test(a.url))
   return '<a href="'+safe+'" target="_blank" rel="noopener" title="open full size">'
     +'<img class="att" src="'+safe+'" alt="'+esc(a.name||'image')+'"></a>';
  return '<a class="attlink" href="'+safe+'" download>'+esc(a.name||a.url)+'</a>';
 }).join(' ')+'</div>';
}

function render(m){
 const frag=document.createDocumentFragment();
 if(!prevMsg||dayOf(prevMsg.ts_ns)!==dayOf(m.ts_ns)){
  const d=document.createElement('div');d.className='day';d.textContent=dayOf(m.ts_ns);
  frag.appendChild(d);
 }
 const cont=prevMsg&&prevMsg.from===m.from&&prevMsg.to===m.to
   &&m.ts_ns-prevMsg.ts_ns<5*60*1e9&&dayOf(prevMsg.ts_ns)===dayOf(m.ts_ns);
 const row=document.createElement('div');
 row.className='row'+(m.to==='*'?' bcast':'')+(cont?' cont':'');
 row.dataset.id=m.id;
 row.title='#'+m.id+'  '+new Date(m.ts_ns/1e6).toLocaleString();
 const c=color(m.from);
 row.innerHTML=
  '<div class="gutter"><div class="avatar" style="color:'+c+';background:'+tint(m.from)+'">'
   +initials(m.from)+'</div></div>'
  +'<div class="msgcol">'
  +(cont?'':'<div class="head"><span class="who" style="color:'+c+'">'+esc(m.from)+'</span>'
    +'<span class="arrow">&rarr;</span>'
    +(m.to==='*'?'<span class="all">ALL</span>'
      :'<span class="toname" style="color:'+color(m.to)+'">'+esc(m.to)+'</span>')
    +'<time>'+hhmm(m.ts_ns)+'</time>'
    +'<span class="mid" data-thread="'+m.thread_id+'" title="view thread">#'+m.id
    +(m.thread_id!==m.id?' &middot; thread '+m.thread_id:'')+'</span>'
    +(m.from===myName?'<span class="del" data-del="'+m.id
      +'" title="retract (only while nobody has read it)">&#10007;</span>':'')
    +'</div>')
  +(m.subject?'<div class="subj">'+esc(m.subject)+'</div>':'')
  +'<div class="body">'+esc(m.body)+'</div>'+attHtml(m.attachments)+'</div>';
 row.style.display=matches(m)?'':'none';
 row.addEventListener('click',e=>{
  if(e.target.closest('.mid'))return;        // thread drill-down keeps its own click
  selectRow(row,m);
 });
 frag.appendChild(row);
 prevMsg=m;
 return frag;
}

function add(m){
 if(m.id>lastId)lastId=m.id;
 if(mode!=='live'||msgs.has(m.id))return;   // history view: live frames wait for "back to live"
 msgs.set(m.id,m);
 $('empty')?.remove();
 $('inner').appendChild(render(m));
 if(follow)$('feed').scrollTop=$('feed').scrollHeight;
}

function clearFeed(){
 for(const el of [...$('inner').children])
  if(el.id!=='histInfo')el.remove();
 msgs.clear();prevMsg=null;
}

function toLive(){
 mode='live';follow=true;
 for(const o of document.querySelectorAll('.qr button'))o.classList.remove('on');
 $('histBtn').classList.remove('on');$('histBar').classList.remove('on');
 $('histInfo').classList.remove('on');
 clearFeed();loadBacklog(true);
}

async function runSearch(params){
 mode='history';follow=false;
 $('histBtn').classList.add('on');
 busy(true);
 try{
  const q=new URLSearchParams(params);
  const r=await fetch('/search'+qs()+'&'+q.toString());
  if(!r.ok){toast('search failed: '+(await r.text()));return;}
  const data=await r.json();
  clearFeed();
  $('histInfo').classList.add('on');
  $('histLabel').textContent=(params.thread_id?'thread '+params.thread_id:'history')
    +' -- '+data.count+' message'+(data.count===1?'':'s');
  for(const m of data.messages){msgs.set(m.id,m);$('inner').appendChild(render(m));}
  $('feed').scrollTop=0;
 }finally{busy(false);}
}

// datetime-local is the viewer's LOCAL time; convert to UTC ISO so the broker's
// naive-ISO-is-UTC rule can never shift the window (the 0.1.1 false-zero lesson).
function utcIso(v){return v?new Date(v).toISOString():'';}

function refilter(){
 for(const el of $('inner').children){
  if(!el.dataset.id)continue;                      // day dividers stay
  el.style.display=matches(msgs.get(+el.dataset.id))?'':'none';
 }
 $('filterState').style.display=selAgents.size?'inline':'none';
 $('filterState').textContent=selAgents.size?(filterMode.toUpperCase()+': '+[...selAgents].join(' + ')+'  ✕'):'';
 if(follow)$('feed').scrollTop=$('feed').scrollHeight;
}

async function loadBacklog(reset){
 if(mode!=='live')return;
 if(reset)busy(true);
 try{
  const r=await fetch('/messages'+qs()+'&limit=300'+(reset?'':'&since_id='+lastId));
  if(r.status===401){showLogin('unauthorized -- check the token');return;}
  if(!r.ok)return;
  if(reset)clearFeed();
  for(const m of (await r.json()).messages)add(m);
 }catch(e){
  setTimeout(()=>loadBacklog(reset),1500);   // broker restarting; keep trying until live
 }finally{busy(false);}
}

function busy(on){$('spin').classList.toggle('on',!!on);}

function updateReplyChip(){
 $('replyChip').className=replyTo?'on':'';
 $('replyChip').textContent=replyTo?('reply \u2192 #'+replyTo+'  \u2715'):'';
}

function selectRow(row,m){
 const was=row.classList.contains('active');
 for(const el of document.querySelectorAll('.row.active'))el.classList.remove('active');
 recip.clear();replyTo=null;
 if(!was){                                   // click again to deselect back to ALL
  row.classList.add('active');
  replyTo=m.id;
  if(m.from===myName){                       // own message: target its recipient, never yourself
   if(m.to!=='*'&&m.to!==myName)recip.add(m.to);
  }else recip.add(m.from);
  $('body').focus();
 }
 renderPicker();updateReplyChip();
}

function toLabel(){
 if(recip.size===0)return 'ALL';
 if(recip.size===1)return [...recip][0];
 return recip.size+' agents';
}

function renderPicker(){
 const p=$('toPanel');p.innerHTML='';
 const all=document.createElement('div');
 all.className='pick bcastRow'+(recip.size===0?' on':'');
 all.innerHTML='<span class="box"></span><span>ALL &mdash; broadcast</span>';
 all.onclick=e=>{e.stopPropagation();recip.clear();renderPicker();};
 p.appendChild(all);
 for(const a of agentList){
  if(a.name===myName)continue;
  const el=document.createElement('div');
  el.className='pick'+(recip.has(a.name)?' on':'');
  el.innerHTML='<span class="box"></span><span style="color:'+color(a.name)+'">'
    +esc(a.name)+'</span>';
  el.onclick=e=>{e.stopPropagation();recip.has(a.name)?recip.delete(a.name):recip.add(a.name);renderPicker();};
  p.appendChild(el);
 }
 $('toBtn').textContent=toLabel();
 $('shoutWrap').style.display=recip.size===0?'flex':'none';
}

async function loadPresence(){
 const r=await fetch('/presence'+qs()+'&me='+encodeURIComponent(myName));
 if(r.status===401){showLogin('unauthorized -- check the token');return;}
 if(!r.ok)return;
 agentList=(await r.json()).agents
  .sort((a,b)=>(b.connected-a.connected)||(b.live-a.live)||a.name.localeCompare(b.name));
 $('agents').innerHTML='';
 const all=document.createElement('div');
 all.className='agent allrow'+(selAgents.size?'':' sel live');
 all.innerHTML='<span class="swatch" style="background:var(--gold)"></span>'
   +'<span class="nm">everyone</span><span class="st"></span>';
 all.title='clear the agent filter';
 all.onclick=()=>{selAgents.clear();recip.clear();renderPicker();loadPresence();refilter();};
 $('agents').appendChild(all);
 const sel=$('hAgent'),keep=sel.value;
 sel.innerHTML='<option value="">any</option>';
 for(const a of agentList){
  sel.innerHTML+='<option value="'+esc(a.name)+'">'+esc(a.name)+'</option>';
 }
 sel.value=keep;
 for(const a of agentList){
  const el=document.createElement('div');
  el.className='agent'+(a.live?' live':'')+(a.connected?' conn':'')
    +(selAgents.has(a.name)?' sel':'');
  el.innerHTML='<span class="swatch" style="background:'+color(a.name)+'"></span>'
    +'<span class="nm">'+esc(a.name)+'</span><span class="st"></span>';
  el.title=a.connected?'wake armed':a.live?'live, waiter down':'stale';
  el.onclick=()=>{selAgents.has(a.name)?selAgents.delete(a.name):selAgents.add(a.name);
   recip.clear();for(const n of selAgents)if(n!==myName)recip.add(n);   // filter selection IS the target, minus yourself
   renderPicker();loadPresence();refilter();};
  $('agents').appendChild(el);
 }
 renderPicker();
}

function connect(){
 const proto=location.protocol==='https:'?'wss':'ws';
 const ws=new WebSocket(proto+'://'+location.host+'/feed'+qs());
 ws.onopen=()=>{$('status').classList.add('on');$('meDot').classList.add('on');
  $('meDot').title='connected to the room feed';loadBacklog(false);};
 ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.error){if(m.error==='bad_token')showLogin('unauthorized -- check the token');return;}
  if(m.deleted){
   for(const el of [...$('inner').children])
    if(+el.dataset.id===m.deleted)el.remove();
   msgs.delete(m.deleted);return;
  }
  add(m);};
 ws.onclose=()=>{$('status').classList.remove('on');$('meDot').classList.remove('on');
  $('meDot').title='reconnecting';
  if(!$('login').classList.contains('on'))setTimeout(connect,2000);};
}

$('feed').addEventListener('scroll',()=>{
 const el=$('feed');
 follow=el.scrollTop+el.clientHeight>=el.scrollHeight-40;
 $('jump').style.display=follow?'none':'block';
});
$('jump').onclick=()=>{follow=true;$('feed').scrollTop=$('feed').scrollHeight;$('jump').style.display='none';};
$('filter').addEventListener('input',refilter);
$('filterState').onclick=()=>{selAgents.clear();recip.clear();renderPicker();
 loadPresence();refilter();};
for(const mode of ['From','To'])
 $('fm'+mode).onclick=()=>{filterMode=mode.toLowerCase();
  $('fmFrom').className=filterMode==='from'?'on':'';
  $('fmTo').className=filterMode==='to'?'on':'';
  refilter();};
$('histBtn').onclick=()=>{
 if(mode==='history'){toLive();return;}
 $('histBar').classList.toggle('on');
};
$('hGo').onclick=e=>{e.preventDefault();
 const p={limit:500};
 if($('hKw').value.trim())p.keywords=$('hKw').value.trim();
 if($('hSince').value)p.since=utcIso($('hSince').value);
 if($('hUntil').value)p.until=utcIso($('hUntil').value);
 if($('hAgent').value.trim())p.agent=$('hAgent').value.trim();
 runSearch(p);
};
$('backLive').onclick=toLive;
for(const b of document.querySelectorAll('.qr button'))
 b.onclick=()=>{
  for(const o of document.querySelectorAll('.qr button'))o.classList.remove('on');
  b.classList.add('on');
  const p={limit:500};
  if(b.dataset.since)p.since=b.dataset.since;         // relative spec, server-parsed
  if($('hKw').value.trim())p.keywords=$('hKw').value.trim();
  if($('hAgent').value.trim())p.agent=$('hAgent').value.trim();
  $('hSince').value='';$('hUntil').value='';
  runSearch(p);
 };
// manual date edits or leaving history mode deselect the chip
for(const id of ['hSince','hUntil'])
 $(id).addEventListener('input',()=>{
  for(const o of document.querySelectorAll('.qr button'))o.classList.remove('on');});
$('inner').addEventListener('click',async e=>{
 const d=e.target.closest('.del');
 if(d){
  const r=await fetch('/message/'+d.dataset.del+qs()+'&from='+encodeURIComponent(myName),
    {method:'DELETE'});
  if(!r.ok){
   const err=JSON.parse(await r.text());
   if(err.readers?.length)
    showDialog('Cannot retract #'+d.dataset.del,
     'Retraction is only possible while nobody has read the message.',err.readers);
   else if(/replied/.test(err.error||''))
    showDialog('Cannot retract #'+d.dataset.del,
     'This message already has replies threaded on it.');
   else
    showDialog('Cannot retract #'+d.dataset.del,err.error||'retract failed');
  }
  return;
 }
 const t=e.target.closest('.mid');
 if(t)runSearch({thread_id:t.dataset.thread});
});
$('toBtn').onclick=()=>$('toPanel').classList.toggle('on');
$('replyChip').onclick=()=>{replyTo=null;updateReplyChip();
 for(const el of document.querySelectorAll('.row.active'))el.classList.remove('active');};
document.addEventListener('click',e=>{
 if(!e.target.closest('#toWrap'))$('toPanel').classList.remove('on');});
$('body').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))$('composer').requestSubmit();});
$('attachBtn').onclick=()=>$('fileInput').click();
{
 const comp=$('composer');
 let depth=0;
 comp.addEventListener('dragenter',e=>{e.preventDefault();depth++;comp.classList.add('drag');});
 comp.addEventListener('dragover',e=>e.preventDefault());
 comp.addEventListener('dragleave',()=>{if(--depth<=0){depth=0;comp.classList.remove('drag');}});
 comp.addEventListener('drop',e=>{
  e.preventDefault();depth=0;comp.classList.remove('drag');
  [...(e.dataTransfer?.files||[])].forEach(uploadFile);
 });
}
$('fileInput').addEventListener('change',()=>{
 for(const f of $('fileInput').files)uploadFile(f);
 $('fileInput').value='';
});
$('body').addEventListener('paste',e=>{
 const files=[...(e.clipboardData?.files||[])];
 if(files.length){e.preventDefault();files.forEach(uploadFile);}
});
$('composer').addEventListener('submit',async e=>{
 e.preventDefault();
 let body=$('body').value.trim();
 if(!body&&attachments.length)body=attachments.map(a=>a.name).join(', ');
 if(!body)return;
 const targets=recip.size?[...recip]:['*'];   // subgroup = one gated unicast per member
 for(const to of targets){
  const payload={from:myName,to,subject:$('subject').value.trim(),body};
  if(to==='*'&&$('shout').checked)payload.shout=true;
  if(attachments.length)payload.attachments=attachments.slice();
  if(replyTo)payload.reply_to=replyTo;
  const r=await fetch('/send'+qs(),{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify(payload)});
  if(!r.ok){toast('send to '+to+' failed: '+(await r.text()));return;}
 }
 $('body').value='';$('subject').value='';$('shout').checked=false;
 attachments.length=0;renderAttchips();
 replyTo=null;updateReplyChip();
 for(const el of document.querySelectorAll('.row.active'))el.classList.remove('active');
});

fetch('/version').then(r=>r.text()).then(v=>$('ver').textContent='v'+v);
if(localStorage.agentbusToken===undefined){showLogin();}
else{loadBacklog(true);loadPresence();connect();setInterval(loadPresence,15000);}
</script></body></html>
"""


async def chat_http(_request):
    return HTMLResponse(WEBCHAT)


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
 <p><a href="/ui" style="color:#888">&larr; bus chat</a></p>
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
            Route("/ui", chat_http),
            Route("/terminal", ui_http),
            Route("/messages", messages_http),
            Route("/search", search_http),
            Route("/presence", presence_http),
            Route("/send", send_http, methods=["POST"]),
            Route("/upload", upload_http, methods=["POST"]),
            Route("/message/{mid:int}", delete_http, methods=["DELETE"]),
            Route("/files/{fname}", files_http),
            WebSocketRoute("/wake", wake_ws),
            WebSocketRoute("/feed", feed_ws),
            WebSocketRoute("/term", term_ws),
            Mount("/", app=mcp_app),
        ],
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
    global _conn, _files_dir
    import uvicorn
    _setup_logging()
    root = os.environ.get("CLAUDE_AGENT_BUS") or os.path.expanduser("~/.claude/agent-bus")
    db = os.environ.get("AGENTBUS_DB") or os.path.join(root, "broker.db")
    _conn = store.connect(db)
    _files_dir = pathlib.Path(db).parent / "files"
    _files_dir.mkdir(parents=True, exist_ok=True)
    if TOKEN:  # pre-room rows (room='') belong to the fleet's key; idempotent backfill
        _conn.execute("UPDATE agents SET room=? WHERE room=''", (TOKEN,))
        _conn.execute("UPDATE messages SET room=? WHERE room=''", (TOKEN,))
    host = os.environ.get("AGENTBUS_HOST", "0.0.0.0")
    port = int(os.environ.get("AGENTBUS_PORT", "8765"))
    log.info("daemon on %s:%s db=%s auth=%s", host, port, db, "token" if TOKEN else "OPEN")
    config = uvicorn.Config(build_app(), host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    orig_exit = server.handle_exit

    def handle_exit(sig, frame):
        # Courtesy ring first, real shutdown ~0.5s later so the frames flush.
        with contextlib.suppress(RuntimeError):
            asyncio.get_event_loop().call_soon_threadsafe(_push_shutdown)
        threading.Timer(0.5, lambda: orig_exit(sig, frame)).start()

    server.handle_exit = handle_exit
    server.run()


if __name__ == "__main__":
    main()
