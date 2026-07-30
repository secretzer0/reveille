#!/usr/bin/env python3
"""A deaf agent is VISIBLE -- from both presence surfaces, with the reason --
and one ordinary call clears it (design msg 8614, GO msg 8620).

The 21-hour stranding (msg 8573) had this shape: direct mail piling up unread,
no wake attachment, presence green the whole time. Every existing signal asked
"is the agent HERE"; nothing asked "is mail it was sent going unread". The
verdict is COMPUTED at presence-read from live rows (ratified doctrine: derived
state that can lapse must self-heal on next use or report its own absence) --
so this gate asserts the READING, end to end, on a real broker:

  1. Agent joins, goes silent; direct mail arrives; DEAF_AFTER passes.
     -> deaf:true, reason no-waiter, from BOTH the HTTP presence endpoint and
        the MCP presence tool.
  2. The wake daemon attaches. Attach is activity (_seen), so deafness clears
     -- asserted, because "attached" must not read as "still deaf".
  3. Fresh mail arrives, the waiter holds the socket, the agent stays silent
     past DEAF_AFTER -> deaf:true again, reason not-draining: rings arrive and
     nothing acts. The socket being up is not the agent being alive.
  4. The agent makes ONE ordinary call (inbox) -> the verdict clears on both
     surfaces. No flag was stored anywhere at any point.

REVEILLE_DEAF_AFTER is the env knob (seconds, default 900); the gate runs at 2.
"""
import asyncio
import contextlib
import json
import pathlib
import sys
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from reveille import store  # noqa: E402
from scratch import scratch_broker  # noqa: E402  -- owned-lifetime daemon

ROLE = "deaf-dev"
DEAF_AFTER = 2


def post(base, path, secret, payload):
    r = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {secret}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        return resp.status, json.loads(resp.read())


def presence_row(base, secret, name):
    r = urllib.request.Request(base + "/presence",
                               headers={"Authorization": f"Bearer {secret}"})
    with urllib.request.urlopen(r, timeout=10) as resp:
        agents = json.loads(resp.read())["agents"]
    return next((a for a in agents if a["name"] == name), None)


def main():
    # The broker's lifetime is OWNED by the with-block (tests/scratch.py):
    # no bare Popen, no cleanup step, nothing for a tidy-up reflex to reach
    # for -- the mechanism from docs/NOTES-rules-are-not-controls.md, adopted
    # here first. Seeding rides the running daemon's db (sqlite multi-process,
    # same as readmit_gate's mid-run reap).
    with scratch_broker({"REVEILLE_DEAF_AFTER": str(DEAF_AFTER)}) as b:
        base = b.base
        conn = store.connect(b.db)
        u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
        room = store.create_room(conn, u["id"], "gate")
        tok = store.create_token(conn, u["id"], ROLE, agent_name=ROLE)
        store.assign_room(conn, tok["id"], room["id"], u["id"])
        adm = store.create_token(conn, u["id"], "ana", agent_name="ana")
        store.assign_room(conn, adm["id"], room["id"], u["id"])
        conn.close()

        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def call(tool, args=None, secret=tok["secret"], agent=ROLE):
            hdrs = {"Authorization": f"Bearer {secret}", "X-Agent": agent}
            async with streamablehttp_client(f"{base}/mcp", headers=hdrs) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, args or {})
                    return res.structuredContent or json.loads(res.content[0].text)

        def mcp_row(name):
            out = asyncio.run(call("presence", secret=adm["secret"], agent="ana"))
            return next((a for a in out["agents"] if a["name"] == name), None)

        # -- 1. silent agent, unread direct mail, DEAF_AFTER passes ----------
        asyncio.run(call("join", {"url": base}))
        row = presence_row(base, adm["secret"], ROLE)
        assert row and not row.get("deaf"), "deaf before any mail -- verdict is noise"
        post(base, "/send", adm["secret"],
             {"from": "ana", "to": ROLE, "subject": "s1", "body": "unheard"})
        time.sleep(DEAF_AFTER + 1)

        row = presence_row(base, adm["secret"], ROLE)
        assert row and row.get("deaf") is True, (
            "HTTP presence shows no deafness on an agent whose direct mail sat "
            "unread past DEAF_AFTER with no waiter -- the 21h shape, invisible "
            f"again: {row}")
        assert row.get("deaf_reason") == "no-waiter", row
        mrow = mcp_row(ROLE)
        assert mrow and mrow.get("deaf") is True and \
            mrow.get("deaf_reason") == "no-waiter", (
            f"MCP presence disagrees with HTTP: {mrow} -- an agent checking "
            "before a blocking unicast would be told the peer is fine")

        # -- 2. the wake daemon attaches: attach is activity, verdict clears --
        import websockets

        async def attach_and_hold(evt, done):
            # Mirrors reveille-waked: receive rings and KEEP the socket -- the
            # first version returned on the first ring frame, closing the
            # attachment, and the gate itself proved that a dropped socket
            # reads as no-waiter.
            uri = f"ws://127.0.0.1:{b.port}/wake?name={ROLE}&token={tok['secret']}"
            async with websockets.connect(uri) as ws:
                evt.set()
                loop = asyncio.get_running_loop()
                end = loop.time() + done
                while (left := end - loop.time()) > 0:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(ws.recv(), timeout=left)

        async def scenario():
            evt = asyncio.Event()
            holder = asyncio.create_task(attach_and_hold(evt, 3 * DEAF_AFTER + 6))
            await asyncio.wait_for(evt.wait(), timeout=10)
            await asyncio.sleep(0.5)
            row = presence_row(base, adm["secret"], ROLE)
            assert row and not row.get("deaf"), (
                f"still deaf right after the wake attach: {row} -- attach is "
                "activity and must clear the verdict, or a healthy re-arm reads "
                "as an outage")

            # -- 3. socket held, fresh mail, silence again -> not-draining ----
            post(base, "/send", adm["secret"],
                 {"from": "ana", "to": ROLE, "subject": "s2", "body": "ringing"})
            await asyncio.sleep(DEAF_AFTER + 1)
            row = presence_row(base, adm["secret"], ROLE)
            assert row and row.get("deaf") is True and \
                row.get("deaf_reason") == "not-draining", (
                f"waiter attached, mail unread past DEAF_AFTER: {row} -- the "
                "socket being up must not read as the agent being alive")

            # -- 4. ONE ordinary call clears it, both surfaces ---------------
            await call("inbox")
            row = presence_row(base, adm["secret"], ROLE)
            assert row and not row.get("deaf"), f"drain did not clear: {row}"
            out = await call("presence", secret=adm["secret"], agent="ana")
            mrow = next((a for a in out["agents"] if a["name"] == ROLE), None)
            assert mrow and not mrow.get("deaf"), f"MCP still deaf: {mrow}"
            holder.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await holder

        asyncio.run(scenario())

        print("deafness-gate OK: silent agent with unread direct mail went "
              f"deaf:no-waiter after {DEAF_AFTER}s on BOTH presence surfaces; "
              "wake attach cleared it (attach is activity); held socket + fresh "
              "unread mail flipped it to deaf:not-draining; one ordinary inbox "
              "call cleared it everywhere; nothing was stored; the broker's "
              "lifetime was owned by scratch_broker, nothing left to tidy")


if __name__ == "__main__":
    main()
