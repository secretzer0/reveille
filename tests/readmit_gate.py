#!/usr/bin/env python3
"""An agent reaped mid-session comes BACK on its next call -- visible in presence
and addressable by unicast -- without being told to do anything (operator report
2026-07-30, msg 8576).

What the operator saw: reveille-senior-ui-ux's messages arriving in the web feed
while the AGENTS rail did not list it. It had been deaf for 21 hours, so
reap_stale had correctly dropped its member row; nothing but an explicit join()
ever put one back, and it was mid-session, so it never called join again. From
the inside every call still succeeded -- it could send, read, commit -- while
being absent from presence and UNADDRESSABLE (a unicast to it is refused with
"is it joined?"). Nothing anywhere told it.

Proven here over the real MCP surface, in the order it happens for real:
  1. Agent joins, is visible, is addressable.
  2. Its membership is reaped (the outage).
  3. It makes ONE ordinary call -- not join, not a flag.
  4. It is visible again, addressable again, and the mail sent while it was gone
     is still UNREAD (a re-join would have eaten it).
"""
import asyncio
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from reveille import store  # noqa: E402

ROLE = "reaped-dev"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(port, timeout=25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(OSError):
            if urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1).read() == b"ok":
                return True
        time.sleep(0.2)
    return False


def post(base, path, secret, payload):
    r = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {secret}",
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r, timeout=10) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def presence_names(base, secret):
    r = urllib.request.Request(base + "/presence",
                               headers={"Authorization": f"Bearer {secret}"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return [a["name"] for a in json.loads(resp.read())["agents"]]


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    port = free_port()
    base = f"http://127.0.0.1:{port}"
    db = str(tmp / "broker.db")

    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "gate")
    tok = store.create_token(conn, u["id"], ROLE, agent_name=ROLE, create=True)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    adm = store.create_token(conn, u["id"], "ana", agent_name="ana", create=True)
    store.assign_room(conn, adm["id"], room["id"], u["id"])
    conn.close()

    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    env["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + env["PATH"]
    proc = subprocess.Popen(["reveille-daemon"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert wait_health(port)

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def call(tool, args=None):
            hdrs = {"Authorization": f"Bearer {tok['secret']}", "X-Agent": ROLE}
            async with streamablehttp_client(f"{base}/mcp", headers=hdrs) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, args or {})
                    return res.structuredContent or json.loads(res.content[0].text)

        # -- 1. joined: visible and addressable ------------------------------
        asyncio.run(call("join", {"url": base}))
        assert ROLE in presence_names(base, adm["secret"])
        code, _ = post(base, "/send", adm["secret"],
                       {"from": "ana", "to": ROLE, "subject": "before",
                        "body": "reachable"})
        assert code == 200, code

        # -- 2. the outage: heartbeat goes stale, the row is reaped ----------
        c = store.connect(db)
        c.execute("UPDATE members SET seen_ns=? WHERE name=?",
                  (time.time_ns() - 10 * 3600 * 10**9, ROLE))
        c.commit()
        assert store.reap_stale(c) == [ROLE]
        c.commit()
        c.close()
        assert ROLE not in presence_names(base, adm["secret"]), \
            "the reap must really have happened, or this gate proves nothing"
        code, body = post(base, "/send", adm["secret"],
                          {"from": "ana", "to": ROLE, "subject": "during",
                           "body": "should be refused"})
        assert code != 200 and "joined" in json.dumps(body), (code, body)

        # -- 3. ONE ordinary call. Not join. Not a flag. ---------------------
        out = asyncio.run(call("inbox"))
        subjects = [m["subject"] for m in out["messages"]]

        # -- 4. back, and the mail from before the outage is still unread ----
        assert ROLE in presence_names(base, adm["secret"]), \
            ("still invisible after an authenticated call -- the agent can work "
             "and cannot be seen or addressed, which is the defect")
        code, _ = post(base, "/send", adm["secret"],
                       {"from": "ana", "to": ROLE, "subject": "after",
                        "body": "reachable again"})
        assert code == 200, "still unaddressable after readmit"
        assert "before" in subjects, (
            "readmit ate the mail: the message sent BEFORE the outage must still "
            "be unread, which is why this is not implemented as a re-join")

        # -- 5. and LEAVING still means leaving --------------------------------
        # The trap in the first version of this fix: leave() and the reaper were
        # both a DELETE, so re-admission undid every DIRECTIVE:LEAVE within one
        # tool call. Proven here at the level the agent experiences it.
        asyncio.run(call("leave"))
        assert ROLE not in presence_names(base, adm["secret"]), "leave() did nothing"
        code, body = post(base, "/send", adm["secret"],
                          {"from": "ana", "to": ROLE, "subject": "after leaving",
                           "body": "should be refused"})
        assert code != 200 and "joined" in json.dumps(body), (code, body)
        asyncio.run(call("inbox"))          # the ordinary call that used to undo it
        assert ROLE not in presence_names(base, adm["secret"]), \
            ("an ordinary call re-admitted an agent that deliberately left -- "
             "DIRECTIVE:LEAVE is ratified doctrine and this voids it silently")
        asyncio.run(call("join", {"url": base}))   # join is the way back, and only join
        assert ROLE in presence_names(base, adm["secret"])

        print("readmit-gate OK: an agent reaped mid-session was invisible and "
              "unaddressable, then ONE ordinary MCP call put it back in presence "
              "and made a unicast deliverable again -- with its pre-outage mail "
              "still unread; and after a deliberate leave() that same ordinary "
              "call left it out, until it joined again")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
