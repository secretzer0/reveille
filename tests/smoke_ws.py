#!/usr/bin/env python3
"""End-to-end cross-machine smoke: real daemon subprocess, two agents over
HTTP-MCP, and a WebSocket wake pushed between them. Run: uv run python tests/smoke_ws.py

Proves the daemon the way remote machines use it: data plane over HTTP-MCP with a
per-agent X-Agent header for identity and a per-agent bearer for authority, wake plane
over a pushed WS frame.

There is no open mode to test: an agent presents a token or it gets a 401. So the DB is
seeded with a user, a room and one token per agent BEFORE the daemon starts, which is
also the only honest way to exercise the real path -- rooms are resolved from the token
server-side, never from anything the agent says about itself.
"""
import asyncio
import contextlib
import http.cookiejar
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import websockets
from mcp import ClientSession
import httpx2
from mcp.client.streamable_http import streamable_http_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scratch import child_report  # noqa: E402
from reveille import __version__, store  # noqa: E402


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
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).read() == b"ok":
                return
        time.sleep(0.2)
    raise RuntimeError("daemon did not come up")


def data(result):
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def session(port, name, token):
    url = f"http://127.0.0.1:{port}/mcp"
    return streamable_http_client(
        url,
        http_client=httpx2.AsyncClient(
            headers={"X-Agent": name,
                     "Authorization": f"Bearer {token}"},
            timeout=httpx2.Timeout(30, read=300)))


async def run(port, secrets):
    base = f"http://127.0.0.1:{port}"
    async with session(port, "alice", secrets["alice"]) as (ra, wa), \
               ClientSession(ra, wa) as alice, \
               session(port, "bob", secrets["bob"]) as (rb, wb), \
               ClientSession(rb, wb) as bob:
        await alice.initialize()
        await bob.initialize()

        tools = {t.name for t in (await alice.list_tools()).tools}
        assert {"join", "send", "inbox", "presence", "trace"} <= tools, tools
        print("HTTP-MCP handshake + tools:", sorted(tools))

        j = data(await alice.call_tool("join", {"name": "alice", "url": base}))
        await bob.call_tool("join", {"name": "bob", "url": base})
        assert j["wake_url"] == f"ws://127.0.0.1:{port}/wake", j
        # the broker never announces a restart on the bus -- boot is where you ask
        assert j["version"] == __version__, j
        # the room came from the token, not from anything alice claimed
        assert [r["name"] for r in j["rooms"]] == ["smoke"], j
        print("join returns wake_url + token's rooms:", j["wake_url"],
              [r["name"] for r in j["rooms"]])

        # arm alice's WS wake, THEN bob sends -> the daemon pushes the ring
        wake_uri = f"{j['wake_url']}?name=alice&token={secrets['alice']}"
        async with websockets.connect(wake_uri) as ws:
            # THE ATTACH FRAME IS UNCONDITIONAL since 0.2.253 -- every connect
            # is answered with {"wake": ..., "reason": "backlog"|"hello", ...}
            # before any mail exists, which is what lets a body converge on a
            # deploy the moment its socket comes back. So the first frame here
            # is the greeting, not the wake, and reading one frame and calling
            # it the wake is how this line sat red from that release until
            # something finally ran the file.
            hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert hello["reason"] in ("hello", "backlog"), hello
            assert hello["unread"] == 0 and not hello["wake"], \
                f"nothing has been sent yet, so the greeting must not ring: {hello}"

            sent = data(await bob.call_tool("send", {"to": "alice", "body": "yo", "subject": "hi"}))
            assert sent["delivered_to"] == ["alice"], sent
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["wake"] and frame["unread"] == 1, frame
            print("WS wake pushed over the wire:", frame)

        box = data(await alice.call_tool("inbox", {}))["messages"]
        assert len(box) == 1 and box[0]["from"] == "bob", box
        await alice.call_tool("ack", {"message_ids": [box[0]["id"]]})
        assert data(await alice.call_tool("inbox", {}))["messages"] == []

        # The wake BINARY on the live-push path, armed exactly as an agent arms it:
        # --once exits 0 only on a real ring, so this is the whole reachability contract.
        w = await asyncio.create_subprocess_exec(
            "wake", "--url", j["wake_url"], "--name", "alice",
            "--token", secrets["alice"], "--once",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await asyncio.sleep(0.5)
        await bob.call_tool("send", {"to": "alice", "body": "ping"})
        out, werr = await asyncio.wait_for(w.communicate(), timeout=8)
        # NAME THE CAUSE, NOT THE CODE. This read `(w.returncode, out)` and
        # threw stderr away, so a CI red said `AssertionError: (2, b'')` and
        # nothing else -- while wake.py had printed exactly why on the stream
        # being discarded. Exit 2 is `superseded`: a NEWER wake attachment for
        # this agent took the slot, and the broker supersedes the OLDER holder
        # on every new attach (daemon.py, `_SUPERSEDE`). So a 2 here means
        # something attached as alice AFTER this binary did, and the next red
        # will say so in its own words instead of in a tuple.
        assert w.returncode == 0 and b'"wake"' in out, child_report(
            "wake", w.returncode, out, werr,
            hint="2 = superseded: a NEWER wake attachment for this agent took the slot")
        print("wake.py binary woke on push:", out.decode().strip())
        ping = data(await alice.call_tool("inbox", {}))["messages"]
        await alice.call_tool("ack", {"message_ids": [m["id"] for m in ping]})

        pres = {a["name"]: a for a in data(await alice.call_tool("presence", {}))["agents"]}
        assert set(pres) == {"alice", "bob"} and pres["alice"]["url"] == base, pres
        print("presence (cross-machine, with urls):",
              {k: v["url"] for k, v in pres.items()})

        # ---- who wakes whom (msg 8496) -----------------------------------
        # Two waiters armed at once. An AGENT broadcast must reach both inboxes
        # and ring NEITHER socket; the same broadcast sent by a HUMAN over the
        # web plane must ring BOTH, carrying the facts that let each decide
        # whether it is owed anything.
        aw = f"{j['wake_url']}?name=alice&token={secrets['alice']}"
        bw = f"{j['wake_url']}?name=bob&token={secrets['bob']}"
        async with websockets.connect(aw) as wsa, websockets.connect(bw) as wsb:
            await asyncio.sleep(0.3)
            await alice.call_tool("send", {"to": "*", "body": "agent broadcast",
                                           "subject": "fyi"})
            # A FRAME IS NOT A RING. Since 0.2.253 every attach is answered with
            # an unconditional greeting (`reason: hello`, `wake: false`), so
            # "the socket said anything at all" stopped meaning "the socket was
            # rung" -- and this check, which treats any frame as the storm,
            # reported the greeting as an N^2 storm from that release onward.
            # What the rule is about is `wake`, so that is what is read.
            for who, ws in (("alice", wsa), ("bob", wsb)):
                while True:
                    try:
                        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=2))
                    except asyncio.TimeoutError:
                        break          # silence is the pass: nothing rang
                    if frame.get("wake"):
                        raise SystemExit(
                            f"agent broadcast RANG {who} -- that is the N^2 storm "
                            f"this rule exists to prevent: {frame}")
            got = data(await bob.call_tool("inbox", {}))["messages"]
            assert any(m["body"] == "agent broadcast" for m in got), got
            print("agent broadcast: delivered to inbox, rang nobody")

            # Same shape, human plane: the web composer's broadcast.
            #
            # AS A REAL SESSION, not a bearer token wearing from:"operator".
            # This used to post with ALICE'S AGENT TOKEN, which worked back
            # when the plane was inferred from the `from` string. Since unbound
            # tokens went read-only (11252) a human IS a session principal --
            # an unbound token on /send answers 401, measured -- so the old
            # shape quietly became an AGENT's parentless broadcast, which
            # correctly rings nobody, and this half asserted the opposite of
            # what it was exercising. Logging in is what makes it the human
            # plane, and the anti-storm rule above keeps its only coverage.
            jar = http.cookiejar.CookieJar()
            web = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
            web.open(urllib.request.Request(
                f"{base}/login",
                data=json.dumps({"name": "smoke",
                                 "password": "smoke-pw-not-a-real-secret"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=10)
            web.open(urllib.request.Request(
                f"{base}/send", method="POST",
                data=json.dumps({"to": "*", "subject": "page",
                                 "body": "human broadcast"}).encode(),
                headers={"Content-Type": "application/json"}), timeout=5)
            for who, ws in (("alice", wsa), ("bob", wsb)):
                frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
                assert frame["wake"], (who, frame)
                # the ring carries the facts, so a woken agent can apply the
                # reply test without a round trip
                # THE BROKER NAMES THE REAL SENDER, not what the client typed.
                # This asked for "operator" because the old shape PASSED that
                # string in the body and the broker echoed it. Since 14056 the
                # sender is resolved server-side from the authenticated
                # principal, so it is the logged-in user -- a strictly stronger
                # property than the one this line used to check, and the reason
                # the body no longer carries a `from` at all.
                assert frame["from"] == "smoke", (who, frame)
                assert frame["from_moniker"] == "smoke", (who, frame)
                assert frame["subject"] == "page", (who, frame)
                assert frame["direct"] == 0, (who, frame)   # nothing addressed to me
                assert frame["unread"] >= 1, (who, frame)
            print("human broadcast: rang BOTH waiters, frames carry "
                  "from/subject/direct=0")

    print("\nWS + HTTP-MCP smoke OK")


async def check_auth(port, token):
    # Each surface rejects in its OWN idiom, and asserting the wrong one proves nothing:
    # this used to POST /mcp with no Accept header and call the resulting 406 a 401.
    #
    # REST API -> a real HTTP 401. This is the path scripts/set-token validates against,
    # so a token that fails here is one no agent could have used.
    for headers, label in (({}, "no token"), ({"Authorization": "Bearer WRONG"}, "bad token")):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/messages?limit=1",
                                     headers=headers)
        try:
            urllib.request.urlopen(req, timeout=3)
            raise AssertionError(f"/messages accepted a request with {label}")
        except urllib.error.HTTPError as e:
            assert e.code == 401, f"{label}: expected 401, got {e.code}"

    # MCP tool layer -> a JSON-RPC transport answers 200 and carries the failure in the
    # result, so the assertion is on isError, not on the status code.
    async with session(port, "alice", "WRONG") as (r, w), ClientSession(r, w) as bad:
        await bad.initialize()
        res = await bad.call_tool("inbox", {})
        assert res.is_error and "bad token" in res.content[0].text.lower(), res

    # Binding (0.2.7): alice's token IS alice. Presenting it as bob must fail in each
    # surface's own idiom -- MCP isError, WS name_mismatch frame -- and both reasons
    # must be DISTINGUISHABLE from a dead credential.
    async with session(port, "bob", token) as (r2, w2), ClientSession(r2, w2) as forged:
        await forged.initialize()
        res = await forged.call_tool("inbox", {})
        assert res.is_error and "bound" in res.content[0].text.lower(), res
    async with websockets.connect(f"ws://127.0.0.1:{port}/wake?name=bob&token={token}") as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert frame.get("error") == "name_mismatch", frame

    # WS rejections must be DISTINGUISHABLE (bug.md): accepted, then a reason frame.
    async with websockets.connect(f"ws://127.0.0.1:{port}/wake?name=x&token=WRONG") as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert frame.get("error") == "bad_token", frame
    async with websockets.connect(f"ws://127.0.0.1:{port}/wake?token={token}") as ws:  # good token, no name
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert frame.get("error") == "missing_name", frame
    print("auth: REST 401 + MCP isError('bad token'/'bound') + WS reasons "
          "(bad_token, missing_name, name_mismatch)")


def seed(db):
    """One user, one room, one token per agent -- written before the daemon opens the DB.

    Mirrors what an operator does in the web UI: mint a token, put a room on it. The
    secret exists only here and in the agent's env; the DB keeps a hash, so a test that
    wanted to read it back afterwards could not.
    """
    conn = store.connect(db)
    store.migrate(conn, db)
    owner = store.create_user(conn, "smoke", "smoke-pw-not-a-real-secret")
    room = store.create_room(conn, owner["id"], "smoke")
    secrets = {}
    for name in ("alice", "bob"):
        # BOUND tokens (0.2.7): the whole smoke then runs the per-agent path --
        # every MCP call and wake connect below is also a binding check.
        tok = store.create_token(conn, owner["id"], name, agent_name=name, create=True)
        store.assign_room(conn, tok["id"], room["id"], owner["id"])
        secrets[name] = tok["secret"]
    conn.close()
    return secrets




def spawn_daemon():
    port = free_port()
    db = os.path.join(tempfile.mkdtemp(), "broker.db")
    secrets = seed(db)
    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    proc = subprocess.Popen(["reveille-daemon"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return port, proc, secrets


def main():
    # One daemon serves both halves: there is no open mode to spawn separately any more.
    port, proc, secrets = spawn_daemon()
    try:
        wait_health(port)
        asyncio.run(run(port, secrets))
        asyncio.run(check_auth(port, secrets["alice"]))
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
