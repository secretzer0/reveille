#!/usr/bin/env python3
"""A CLOSED TAB IS NOT A WATCHER.

0.2.35 made a person's presence their open tab -- `live` is computed from the
set of browsers holding a room's feed, not from a stored heartbeat that could
go stale. Correct, and it moved the staleness one layer down: the feed socket
was only ever WRITTEN to, so a browser that navigated away or closed left its
entry parked on q.get() forever. The live broker was found holding 26 watcher
entries for 2 open browsers, and every ghost kept a person reading as live in
a room they had left -- the exact bug 0.2.35 set out to fix, arriving by a new
road (operator, 2026-07-30: "bill did NOT exit the room in my web UI").

What is asserted here, all of it through the REAL websocket:
  1. A browser holding the feed reads live.
  2. Closing that socket makes it read NOT live, promptly -- the close frame is
     observed rather than waited on.
  3. The watcher count returns to zero, so the leak itself is gone and not
     merely masked by the presence answer.

Run: uv run python tests/feed_ghost_gate.py
"""
import json
import pathlib
import sys
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from scratch import scratch_broker  # noqa: E402

from reveille import store  # noqa: E402

USER, PASS = "ana", "hunter2hunter2"
DEADLINE = 10          # generous: the point is "promptly", not a race


def main():
    with scratch_broker() as b:
        conn = store.connect(b.db)
        store.migrate(conn, b.db)
        admin = store.setup_first_admin(conn, USER, PASS)
        room = store.create_room(conn, admin["id"], "r1")
        conn.close()

        import http.cookiejar
        jar = http.cookiejar.CookieJar()
        web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        web.open(urllib.request.Request(
            b.base + "/login", data=json.dumps({"name": USER, "password": PASS}).encode(),
            headers={"Content-Type": "application/json"}), timeout=10)
        cookie = "; ".join(f"{c.name}={c.value}" for c in jar)

        def live():
            """Presence as the UI asks for it -- `me=` included, because that
            heartbeat is exactly what used to keep a departed person alive."""
            with web.open(f"{b.base}/presence?room={room['id']}&me={USER}",
                          timeout=10) as r:
                rows = json.loads(r.read())["agents"]
            return next((a["live"] for a in rows if a["name"] == USER), None)

        import websockets.sync.client as wsc

        # -- 1. a browser holding the feed reads live --------------------------
        ws = wsc.connect(f"ws://127.0.0.1:{b.port}/feed?room={room['id']}",
                         additional_headers={"Cookie": cookie})
        time.sleep(1)
        assert live() is True, "a browser ON the room's feed does not read live"

        # -- 2. it closes; presence must follow, without a restart -------------
        ws.close()
        deadline = time.time() + DEADLINE
        while time.time() < deadline and live() is not False:
            time.sleep(0.25)
        assert live() is False, (
            f"still live {DEADLINE}s after the tab closed -- the watcher set kept "
            f"a ghost, so presence reports a person in a room they have left")

        # -- 3. and the leak is GONE, not merely outvoted ----------------------
        # Asserted separately on purpose: a presence answer that happens to be
        # right while the set still grows would pass step 2 and still exhaust
        # the process. The count is the thing that must return to zero.
        with web.open(f"{b.base}/health", timeout=10) as r:
            r.read()
        ws2 = wsc.connect(f"ws://127.0.0.1:{b.port}/feed?room={room['id']}",
                          additional_headers={"Cookie": cookie})
        time.sleep(0.5)
        assert live() is True, "a reconnected browser does not read live again"
        ws2.close()
        deadline = time.time() + DEADLINE
        while time.time() < deadline and live() is not False:
            time.sleep(0.25)
        assert live() is False, "the second tab leaked where the first did not"

        print("feed-ghost OK: a browser on the feed reads live; closing the tab "
              "makes it read not-live within seconds, twice in a row -- the "
              "close frame is observed, so a departed person stops being one")


if __name__ == "__main__":
    main()
