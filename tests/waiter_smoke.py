#!/usr/bin/env python3
"""DES-003 W1 gate, scriptable form: a real broker, a real reveille-waked, a
real wake-watch, and the real drain discipline between rings (inbox -> ack ->
delete spool entries -> re-arm; the ack is also what clears the broker's poke
gate, exactly as a live agent's turn does).

Proves the split end to end:
1. daemon attaches; a sent message becomes a spool file; wake-watch fires.
2. SUPERSEDE: a second daemon for the same agent (different spool base -- the
   flock is per-host, this simulates another host) takes the slot; the FIRST
   exits code 2 on the superseded frame; the next ring lands in the new
   holder's spool.
3. KILL -9 the holder; a respawned daemon reclaims the slot while the old TCP
   lingers half-open -- the supersede rule makes the reclaim unconditional.
4. BROKER RESTART absorbed: the daemon reconnects by itself, the agent
   performs ZERO re-arms, the next message rings.

Presence is deliberately not the probe: presence lists JOINED members, and a
pure socket-holder never join()s -- ring delivery through the spool is the
attachment proof. The Stop-hook respawn half runs live on this host after
merge (it needs a real Claude session around it); everything the hook wraps
is proven here.
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
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import spool, store  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
AGENT = "smoke-waked"
SENDER = "smoke-sender"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def wait_health(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                      timeout=1).read() == b"ok":
                return
        time.sleep(0.2)
    raise SystemExit("broker never came up")


async def _mcp(port, name, token, calls):
    url = f"http://127.0.0.1:{port}/mcp"
    async with streamablehttp_client(
            url, headers={"X-Agent": name,
                          "Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            out = []
            for tool, args in calls:
                res = await s.call_tool(tool, args)
                if res.isError:
                    raise SystemExit(f"{tool} failed: {res.content[0].text}")
                out.append(res.structuredContent
                           if res.structuredContent is not None
                           else json.loads(res.content[0].text))
            return out


def send_to_agent(port, tok, body):
    asyncio.run(_mcp(port, SENDER, tok, [("send", {"to": AGENT, "body": body})]))


def drain_discipline(port, tok, base):
    """What a live agent does on a ring: inbox() -> ack() -> delete the spool
    entries it processed. The ack clears the broker's poke gate for the next
    ring; the deletes are the spool analog of ack (I4)."""
    (got,) = asyncio.run(_mcp(port, AGENT, tok, [("inbox", {})]))
    ids = [m["id"] for m in got.get("messages", [])]
    if ids:
        asyncio.run(_mcp(port, AGENT, tok, [("ack", {"message_ids": ids})]))
    for p in spool.entries(AGENT, base=base):
        os.unlink(p)


def start_waked(port, tok, base):
    env = dict(os.environ, REVEILLE_TOKEN=tok, REVEILLE_SPOOL=base,
               PYTHONPATH=str(REPO / "src"))
    return subprocess.Popen(
        [sys.executable, "-m", "reveille.waked",
         "--url", f"ws://127.0.0.1:{port}/wake", "--name", AGENT],
        env=env, stderr=subprocess.PIPE, text=True)


def run_watch(base, timeout=30):
    env = dict(os.environ, REVEILLE_SPOOL=base, PYTHONPATH=str(REPO / "src"))
    return subprocess.run([sys.executable, "-m", "reveille.watch", AGENT],
                          env=env, capture_output=True, text=True,
                          timeout=timeout)


def main():
    port = free_port()
    db = os.path.join(tempfile.mkdtemp(), "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    owner = store.create_user(conn, "smoke", "smoke-pw-not-a-real-secret")
    room = store.create_room(conn, owner["id"], "smoke")
    toks = {}
    for name in (AGENT, SENDER):
        t = store.create_token(conn, owner["id"], name, agent_name=name)
        store.assign_room(conn, t["id"], room["id"], owner["id"])
        toks[name] = t["secret"]
    conn.close()
    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    broker = subprocess.Popen(["reveille-daemon"], env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    base1, base2 = tempfile.mkdtemp(), tempfile.mkdtemp()
    d1 = d2 = d3 = None
    try:
        wait_health(port)
        # Both identities join once -- production shape: an agent joins on
        # boot, the daemon only ever holds its wake socket afterwards.
        for name in (AGENT, SENDER):
            asyncio.run(_mcp(port, name, toks[name],
                             [("join", {"url": f"http://127.0.0.1:{port}"})]))

        # 1. attach; message -> spool -> watcher fires; drain like a real turn
        d1 = start_waked(port, toks[AGENT], base1)
        send_to_agent(port, toks[SENDER], "ring one")
        r = run_watch(base1)
        assert r.returncode == 0 and json.loads(r.stdout)["wake"] is True, r.stderr
        drain_discipline(port, toks[AGENT], base1)

        # 2. supersede: the new holder takes the slot, the old one exits 2
        d2 = start_waked(port, toks[AGENT], base2)
        rc = d1.wait(timeout=15)
        assert rc == 2, f"superseded daemon exited {rc}, want 2"
        assert "superseded" in d1.stderr.read()
        send_to_agent(port, toks[SENDER], "ring two")
        r = run_watch(base2)
        assert json.loads(r.stdout)["wake"] is True  # rings land with the NEW holder
        drain_discipline(port, toks[AGENT], base2)

        # 3. kill -9; a respawn reclaims the slot unconditionally
        d2.kill()
        d2.wait(timeout=10)
        d3 = start_waked(port, toks[AGENT], base1)
        send_to_agent(port, toks[SENDER], "ring three")
        r = run_watch(base1)
        assert json.loads(r.stdout)["wake"] is True
        drain_discipline(port, toks[AGENT], base1)

        # 4. broker restart absorbed: same daemon, zero re-arms, next ring lands
        broker.terminate()
        broker.wait(timeout=15)
        broker = subprocess.Popen(["reveille-daemon"], env=env,
                                  stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
        wait_health(port)
        time.sleep(2)   # give the daemon's backoff a beat to reconnect
        send_to_agent(port, toks[SENDER], "ring four")
        r = run_watch(base1, timeout=60)
        assert json.loads(r.stdout)["wake"] is True
        assert d3.poll() is None, "daemon died across the broker restart"

        print("waiter-smoke OK: attach+ring+watch+drain, supersede exits the "
              "old holder (code 2) and rings follow the new one, kill -9 "
              "reclaim via supersede, broker restart absorbed with zero agent "
              "re-arms")
    finally:
        for p in (d1, d2, d3):
            if p and p.poll() is None:
                p.terminate()
        broker.terminate()


if __name__ == "__main__":
    main()
