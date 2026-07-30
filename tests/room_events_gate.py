#!/usr/bin/env python3
"""THE ROOM PUSHES ITS OWN EVENTS, and a browser learns without asking.

Operator, 2026-07-30: "I want the web interface to reflect in real time the
join/leave of a user/agent... These ARE room level events", and then: "There
will end up being MANY other room-level events". So this gates the CHANNEL, not
just presence: every frame carries an `event` type, and presence is the first
event to ride it.

Asserted here, all of it off the REAL websocket with nothing polled:
  1. Every frame carries `event`. A frame without one is the ad-hoc-field
     dispatch this replaced, and it must not come back.
  2. A watcher ARRIVING pushes presence to the watchers already there --
     unprompted, with no fetch.
  3. A watcher LEAVING pushes it again, and the departed person reads NOT live.
     This is the operator's exact complaint, on the wire.
  4. An AGENT calling leave() pushes it too -- an agent is a room event as much
     as a person is.
  5. The frame carries the room's WHOLE presence list (state, not a diff), so a
     browser that missed one is corrected by the next.

Run: uv run python tests/room_events_gate.py
"""
import asyncio
import json
import pathlib
import sys
import threading

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from scratch import scratch_broker  # noqa: E402

from reveille import store  # noqa: E402

USER, PASS = "ana", "hunter2hunter2"
ROLE = "gate-agent"
WAIT = 10


def main():
    with scratch_broker() as b:
        conn = store.connect(b.db)
        store.migrate(conn, b.db)
        u = store.setup_first_admin(conn, USER, PASS)
        room = store.create_room(conn, u["id"], "r1", public=True)
        store.create_user(conn, "bob", PASS)
        tok = store.create_token(conn, u["id"], ROLE, agent_name=ROLE)
        store.assign_room(conn, tok["id"], room["id"], u["id"])
        conn.close()

        import http.cookiejar
        import urllib.request

        def browser(name):
            """A signed-in web session, and the ONE presence poll the page makes at
            boot -- which is what turns a viewer into a member row. Without it a
            watcher is in the socket set but in nobody's presence list, and the
            push would have nothing to say about them."""
            jar = http.cookiejar.CookieJar()
            o = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            o.open(urllib.request.Request(
                b.base + "/login",
                data=json.dumps({"name": name, "password": PASS}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            ck = "; ".join(f"{c.name}={c.value}" for c in jar)
            o.open(f"{b.base}/presence?room={room['id']}&me={name}", timeout=10).read()
            return o, ck

        web, cookie = browser(USER)
        _bobweb, bobcookie = browser("bob")

        import websockets.sync.client as wsc
        url = f"ws://127.0.0.1:{b.port}/feed?room={room['id']}"

        # -- the OBSERVER: one browser, already in the room, never polling -----
        obs = wsc.connect(url, additional_headers={"Cookie": cookie})
        seen = []
        stop = threading.Event()

        def pump():
            while not stop.is_set():
                try:
                    seen.append(json.loads(obs.recv(timeout=1)))
                except Exception:
                    pass

        t = threading.Thread(target=pump, daemon=True)
        t.start()

        def live_of(frame, name):
            return next((a["live"] for a in frame["agents"] if a["name"] == name), None)

        def wait_presence(pred, why):
            end = __import__("time").time() + WAIT
            while __import__("time").time() < end:
                for f in list(seen):
                    if f.get("event") == "presence" and pred(f):
                        return f
                __import__("time").sleep(0.2)
            raise AssertionError(
                f"{why} -- frames seen: {[f.get('event') for f in seen]}")

        # -- 2. someone ELSE arrives: pushed, unprompted ----------------------
        seen.clear()
        second = wsc.connect(url, additional_headers={"Cookie": bobcookie})
        f = wait_presence(lambda f: live_of(f, "bob") is True,
                          "no presence frame showing bob live when he opened the "
                          "room -- ana learns only by polling")
        assert f["room"] == room["id"], f
        # -- 5. state, not a diff --------------------------------------------
        assert isinstance(f["agents"], list) and f["agents"], f
        assert all("live" in a and "name" in a for a in f["agents"]), f

        # -- 3. someone LEAVES: pushed again, and reads NOT live ---------------
        seen.clear()
        second.close()
        f = wait_presence(lambda f: live_of(f, "bob") is False,
                          "closing a tab pushed no presence frame showing the person "
                          "gone -- this is the operator's exact complaint")

        # -- 4. an AGENT leaving is a room event too ---------------------------
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        async def call(tool, args=None):
            hdrs = {"Authorization": f"Bearer {tok['secret']}", "X-Agent": ROLE}
            async with streamablehttp_client(f"{b.base}/mcp", headers=hdrs) as (r_, w_, _):
                async with ClientSession(r_, w_) as s:
                    await s.initialize()
                    res = await s.call_tool(tool, args or {})
                    return res.structuredContent or json.loads(res.content[0].text)

        seen.clear()
        asyncio.run(call("join", {"url": b.base}))
        wait_presence(lambda f: any(a["name"] == ROLE for a in f["agents"]),
                      "an agent joining pushed no presence frame")
        seen.clear()
        asyncio.run(call("leave", {"room": room["id"]}))
        wait_presence(lambda f: not any(a["name"] == ROLE for a in f["agents"]),
                      "an agent leaving pushed no presence frame")

        # -- 1. the channel's own invariant ------------------------------------
        stop.set()
        t.join(timeout=3)
        assert seen, "no frames at all"
        bad = [f for f in seen if "event" not in f]
        assert not bad, f"frames without an event type: {bad[:2]}"
        obs.close()

        print("room-events OK: presence is PUSHED to everyone watching the room "
              "when a browser opens or closes its feed and when an agent joins or "
              "leaves, carrying the whole list rather than a diff -- and every "
              "frame on the channel names its own event type")


if __name__ == "__main__":
    main()
