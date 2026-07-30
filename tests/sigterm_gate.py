#!/usr/bin/env python3
"""SIGTERM stops the broker. It used to wedge it (observed live, 2026-07-30).

The mechanism: uvicorn's graceful shutdown waits for every open connection, with
no time limit by default -- and this daemon's main clients are DESIGNED never to
hang up. waked holds the wake socket for its whole life; the shutdown courtesy
frame tells it "do not reply, just re-arm". So SIGTERM pushed the notices,
closed the listeners, and then waited forever on sockets that would never
close: process alive in state Ss, docker reporting Up, restart policy never
firing, docker start a no-op. The only recovery was SIGKILL by hand.

The holder is NOT the wake socket -- the server closes those itself after the
courtesy frame (the handler breaks), which is why a wake-only version of this
gate passed on the unfixed daemon and proved nothing. The holder is /feed: a
browser tab's socket, parked on q.get() forever, that _push_shutdown never
touches -- the operator had one open, as any operator always will.

So the gate reproduces the incident's exact population: a wake attachment (gets
the courtesy frame) AND a /feed tab that never hangs up. SIGTERM, then assert:
  1. The courtesy shutdown frame still arrives (the feature stays).
  2. The process EXITS within the graceful timeout plus slack -- with the feed
     socket still held. On the pre-fix daemon this is the wedge, verified: the
     gate times out there.
  3. The exit code says SIGTERM (or clean), not a crash.
"""
import contextlib
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
from reveille import store  # noqa: E402

ROLE = "wedge-gate"
USER, PASS = "ana", "hunter2hunter2"
EXIT_DEADLINE = 12   # 0.5s courtesy delay + 5s graceful timeout + slack


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


def main():
    import asyncio

    import websockets

    tmp = tempfile.mkdtemp()
    port = free_port()
    db = os.path.join(tmp, "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, USER, PASS)
    room = store.create_room(conn, u["id"], "gate")
    tok = store.create_token(conn, u["id"], ROLE, agent_name=ROLE)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    conn.close()

    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    env["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + env["PATH"]
    proc = subprocess.Popen(["reveille-daemon"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    got_shutdown = threading.Event()
    attached = threading.Event()

    def hold_the_socket():
        """A waked stand-in: attach to /wake and NEVER hang up -- the exact
        client behaviour that made graceful shutdown wait forever."""
        async def run():
            uri = (f"ws://127.0.0.1:{port}/wake?name={ROLE}"
                   f"&token={tok['secret']}")
            async with websockets.connect(uri) as ws:
                attached.set()
                with contextlib.suppress(Exception):
                    async for raw in ws:
                        if json.loads(raw).get("reason") == "shutdown":
                            got_shutdown.set()
                        # deliberately: no close, no break -- hold the socket
        with contextlib.suppress(Exception):
            asyncio.run(run())

    feed_open = threading.Event()

    def hold_the_feed():
        """The operator's browser tab: a /feed socket parked forever. THIS is
        what graceful shutdown waited on -- nothing ever tells /feed to close."""
        async def run():
            import http.cookiejar
            import urllib.request as u_
            jar = http.cookiejar.CookieJar()
            web = u_.build_opener(u_.HTTPCookieProcessor(jar))
            web.open(u_.Request(
                f"http://127.0.0.1:{port}/login",
                data=json.dumps({"name": USER, "password": PASS}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            cookie = "; ".join(f"{c.name}={c.value}" for c in jar)
            async with websockets.connect(
                    f"ws://127.0.0.1:{port}/feed",
                    additional_headers={"Cookie": cookie}) as ws:
                feed_open.set()
                with contextlib.suppress(Exception):
                    async for _ in ws:
                        pass              # deliberately: never close, never break
        with contextlib.suppress(Exception):
            asyncio.run(run())

    t = threading.Thread(target=hold_the_socket, daemon=True)
    tf = threading.Thread(target=hold_the_feed, daemon=True)
    try:
        assert wait_health(port), "broker never came up"
        t.start()
        tf.start()
        assert attached.wait(timeout=10), "the wake client never attached"
        assert feed_open.wait(timeout=10), "the feed tab never attached"
        time.sleep(0.5)   # let both settle into their tables

        proc.send_signal(15)
        start = time.time()
        try:
            rc = proc.wait(timeout=EXIT_DEADLINE)
        except subprocess.TimeoutExpired:
            raise SystemExit(
                f"WEDGED: {EXIT_DEADLINE}s after SIGTERM the broker is still "
                "alive with its listeners closed -- docker would report Up, the "
                "restart policy would never fire, and every health check that "
                "trusts docker status would call it healthy. This is the "
                "defect.") from None
        elapsed = time.time() - start
        assert got_shutdown.wait(timeout=1), \
            "the courtesy shutdown frame was lost -- exiting is not license to stop saying goodbye"
        # -15 is the CORRECT code, not a crash: after graceful cleanup uvicorn
        # re-raises the captured SIGTERM with the default handler restored, so
        # the process reports dying of exactly the signal it was sent -- which
        # is what its parent (docker, systemd, a shell) is entitled to observe.
        assert rc in (0, -15), f"exit code {rc}: neither clean nor died-of-SIGTERM"

        print(f"sigterm-gate OK: with a /feed tab held open by a client that "
              f"never hangs up (the incident's holder) and a wake attachment "
              f"ringing, SIGTERM exited in {elapsed:.1f}s (deadline "
              f"{EXIT_DEADLINE}s) and the courtesy frame arrived first")
    finally:
        if proc.poll() is None:
            proc.kill()


if __name__ == "__main__":
    main()
