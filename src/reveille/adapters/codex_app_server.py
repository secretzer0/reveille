"""Codex's doorbell: the app-server control socket.

CLAUDE'S DOORBELL READS A DIRECTORY OF SESSION DESCRIPTORS and writes to each
session's own unix socket. Codex publishes no such descriptors. What it has
instead is ONE socket -- the app-server control socket, `$CODEX_HOME/
app-server-control/app-server-control.sock` -- and a JSON-RPC protocol over a
WebSocket on it, through which every live session is visible and addressable.

MEASURED 2026-09-23 against codex-cli 0.156.1, with one idle TUI session open:

    thread/loaded/list -> {"data": ["01a0cedd-..."]}      the live sessions
    thread/read        -> status {"type": "idle"},        what it is doing
                          canAcceptDirectInput true,      whether it can be rung
                          cwd "/home/.../reveille"        WHOSE session it is

Those three fields are exactly the question `doorbell.inboxes_for` asks of a
Claude descriptor -- live, interactive, and in THIS directory -- so the two
runtimes answer the same question from different sources rather than meaning
different things by a ring.

A stored thread is not a session: `thread/list` pages the ROLLOUT LOGS, which
include every conversation that ever ran in a directory and report
`status.type = "notLoaded"`. Ringing one of those would resume somebody's
finished conversation, so the loaded list is the only list this module trusts.

THE DAEMON IS NOT OURS TO START. `codex app-server daemon start` installs and
launches a managed daemon; doing that from a delivery path would mean a ring
spawns a background service on somebody's machine. When the socket is absent
the ring stays in the spool and the reason says so -- which is the same answer
Claude's doorbell gives for "no live session in that directory".
"""
import asyncio
import concurrent.futures
import json
import os
from pathlib import Path

from . import AdapterError

CONNECT_TIMEOUT_S = 5
CALL_TIMEOUT_S = 10
CLIENT = {"name": "reveille", "title": "Reveille", "version": "1"}

# A session that is mid-turn already has the floor; `turn/start` would queue
# behind it and `turn/steer` would shove a ring into somebody's reasoning. Both
# are worse than waiting, because the spool entry is not lost either way.
RINGABLE = ("idle",)


def control_socket():
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    return home / "app-server-control" / "app-server-control.sock"


async def _exchange(calls, collect_ids):
    """initialize, then send `calls`; return {id: result} for `collect_ids`."""
    from websockets.asyncio.client import unix_connect

    sock = control_socket()
    if not sock.exists():
        raise AdapterError(f"no Codex app-server socket at {sock} -- "
                           f"`codex app-server daemon start` runs the daemon")
    async with unix_connect(str(sock), "ws://localhost/",
                            open_timeout=CONNECT_TIMEOUT_S) as ws:
        await ws.send(json.dumps({"method": "initialize", "id": 0,
                                  "params": {"clientInfo": CLIENT}}))
        await asyncio.wait_for(ws.recv(), CALL_TIMEOUT_S)
        await ws.send(json.dumps({"method": "initialized", "params": {}}))
        for call in calls:
            await ws.send(json.dumps(call))
        out, wanted = {}, set(collect_ids)
        while wanted:
            msg = json.loads(await asyncio.wait_for(ws.recv(), CALL_TIMEOUT_S))
            mid = msg.get("id")
            if mid in wanted:            # a notification has no id, so it is skipped
                wanted.discard(mid)
                if "error" in msg:
                    raise AdapterError(f"Codex app-server: "
                                       f"{msg['error'].get('message', msg['error'])}")
                out[mid] = msg.get("result")
        return out


def _run(coro):
    """Run one exchange from sync code, whatever the caller is doing.

    waked is asyncio and the Stop hook is not, so this cannot assume either an
    idle loop or no loop: a thread with its own loop is correct in both, and
    the timeout is the caller's guarantee that a wedged daemon cannot hold a
    delivery path open.
    """
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result(timeout=CALL_TIMEOUT_S * 2)


def _loaded_threads():
    result = _run(_exchange([{"method": "thread/loaded/list", "id": 1, "params": {}}], [1]))
    return list((result.get(1) or {}).get("data") or [])


def sessions(project):
    """Every LIVE session whose cwd is `project`, as a list of thread ids.

    All of them, not the newest -- the same reasoning as Claude's: a duplicate
    ring costs one turn and a skipped one costs every ring.

    NO DAEMON IS AN EMPTY LIST, NOT AN ERROR. Claude's side answers the same
    question by listing a directory that is simply empty when nothing runs, and
    a reachability gate that raised here would turn "Codex is not running" into
    a traceback on the Stop hook's path. The DELIVERY path still names the
    socket, because there the difference between "nothing running at all" and
    "no session in this directory" is the whole reason a ring went nowhere.
    """
    if not control_socket().exists():
        return []
    ids = _loaded_threads()
    if not ids:
        return []
    calls = [{"method": "thread/read", "id": i, "params": {"threadId": tid}}
             for i, tid in enumerate(ids, start=10)]
    read = _run(_exchange(calls, [c["id"] for c in calls]))
    want = str(Path(project).resolve())
    out = []
    for i, tid in enumerate(ids, start=10):
        thread = (read.get(i) or {}).get("thread") or {}
        if str(Path(thread.get("cwd") or "/nonexistent").resolve()) != want:
            continue
        if not thread.get("canAcceptDirectInput"):
            continue
        if (thread.get("status") or {}).get("type") not in RINGABLE:
            continue
        out.append(tid)
    return out


def ring(project, text):
    """Start a turn in every ringable session in `project`. (rung, reason)."""
    sock = control_socket()
    if not sock.exists():
        return 0, (f"no Codex app-server socket at {sock} -- "
                   f"`codex app-server daemon start` runs the daemon")
    try:
        found = sessions(project)
    except (AdapterError, OSError, ValueError, concurrent.futures.TimeoutError) as e:
        return 0, str(e)
    if not found:
        return 0, "no live Codex session in that directory"
    calls = [{"method": "turn/start", "id": i,
              "params": {"threadId": tid, "input": [{"type": "text", "text": text}]}}
             for i, tid in enumerate(found, start=20)]
    try:
        _run(_exchange(calls, [c["id"] for c in calls]))
    except (AdapterError, OSError, ValueError, concurrent.futures.TimeoutError) as e:
        return 0, f"turn/start refused: {e}"
    return len(found), ""
