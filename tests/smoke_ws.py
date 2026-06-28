#!/usr/bin/env python3
"""End-to-end cross-machine smoke: real daemon subprocess, two agents over
HTTP-MCP, and a WebSocket wake pushed between them. Run: uv run python tests/smoke_ws.py

Proves the daemon the way remote machines use it: data plane over HTTP-MCP with a
per-agent X-Agent header for identity, wake plane over a pushed WS frame.
"""
import asyncio
import contextlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request

import websockets
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


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
    if result.structuredContent is not None:
        return result.structuredContent
    return json.loads(result.content[0].text)


def session(port, name):
    url = f"http://127.0.0.1:{port}/mcp"
    return streamablehttp_client(url, headers={"X-Agent": name})


async def run(port):
    base = f"http://127.0.0.1:{port}"
    async with session(port, "alice") as (ra, wa, _a), ClientSession(ra, wa) as alice, \
               session(port, "bob") as (rb, wb, _b), ClientSession(rb, wb) as bob:
        await alice.initialize()
        await bob.initialize()

        tools = {t.name for t in (await alice.list_tools()).tools}
        assert {"join", "send", "inbox", "presence", "trace"} <= tools, tools
        print("HTTP-MCP handshake + tools:", sorted(tools))

        j = data(await alice.call_tool("join", {"name": "alice", "url": base}))
        await bob.call_tool("join", {"name": "bob", "url": base})
        assert j["wake_url"] == f"ws://127.0.0.1:{port}/wake", j
        print("join returns wake_url:", j["wake_url"])

        # arm alice's WS wake, THEN bob sends -> the daemon pushes the ring
        wake_uri = f"{j['wake_url']}?name=alice"
        async with websockets.connect(wake_uri) as ws:
            await asyncio.sleep(0.3)  # let the daemon register the waiter
            sent = data(await bob.call_tool("send", {"to": "alice", "body": "yo", "subject": "hi"}))
            assert sent["delivered_to"] == ["alice"], sent
            frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=5))
            assert frame["wake"] and frame["unread"] == 1, frame
            print("WS wake pushed over the wire:", frame)

        box = data(await alice.call_tool("inbox", {}))["messages"]
        assert len(box) == 1 and box[0]["from"] == "bob", box
        await alice.call_tool("ack", {"message_ids": [box[0]["id"]]})
        assert data(await alice.call_tool("inbox", {}))["messages"] == []

        # the wake.py BINARY on the live-push path: arm it, bob sends, it exits 0
        w = await asyncio.create_subprocess_exec(
            "wake", "--url", j["wake_url"], "--name", "alice", "--timeout", "5",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        await asyncio.sleep(0.5)
        await bob.call_tool("send", {"to": "alice", "body": "ping"})
        out, _ = await asyncio.wait_for(w.communicate(), timeout=8)
        assert w.returncode == 0 and b'"wake"' in out, (w.returncode, out)
        print("wake.py binary woke on push:", out.decode().strip())
        ping = data(await alice.call_tool("inbox", {}))["messages"]
        await alice.call_tool("ack", {"message_ids": [m["id"] for m in ping]})

        pres = {a["name"]: a for a in data(await alice.call_tool("presence", {}))["agents"]}
        assert set(pres) == {"alice", "bob"} and pres["alice"]["url"] == base, pres
        print("presence (cross-machine, with urls):",
              {k: v["url"] for k, v in pres.items()})

    print("\nWS + HTTP-MCP smoke OK")


async def check_auth(port, token):
    # HTTP /mcp without the bearer -> 401
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", method="POST", data=b"{}",
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=3)
        raise AssertionError("HTTP /mcp accepted a request with no token")
    except urllib.error.HTTPError as e:
        assert e.code == 401, f"expected 401, got {e.code}"
    # WS rejections must be DISTINGUISHABLE (bug.md): accepted, then a reason frame.
    async with websockets.connect(f"ws://127.0.0.1:{port}/wake?name=x&token=WRONG") as ws:
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert frame.get("error") == "bad_token", frame
    async with websockets.connect(f"ws://127.0.0.1:{port}/wake?token={token}") as ws:  # good token, no name
        frame = json.loads(await asyncio.wait_for(ws.recv(), timeout=3))
        assert frame.get("error") == "missing_name", frame
    print("auth: HTTP 401 + distinguishable WS reasons (bad_token, missing_name)")


def spawn_daemon(token=None):
    port = free_port()
    env = dict(os.environ, AGENTBUS_DB=os.path.join(tempfile.mkdtemp(), "broker.db"),
               AGENTBUS_PORT=str(port), AGENTBUS_HOST="127.0.0.1")
    if token:
        env["AGENTBUS_TOKEN"] = token
    else:
        env.pop("AGENTBUS_TOKEN", None)
    proc = subprocess.Popen(["agentbus-daemon"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return port, proc


def main():
    # open-mode daemon: full happy path
    port, proc = spawn_daemon()
    try:
        wait_health(port)
        asyncio.run(run(port))
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    # token-mode daemon: auth must reject bad creds
    aport, aproc = spawn_daemon(token="s3cret")
    try:
        wait_health(aport)
        asyncio.run(check_auth(aport, "s3cret"))
    finally:
        aproc.terminate()
        with contextlib.suppress(Exception):
            aproc.wait(timeout=5)


if __name__ == "__main__":
    main()
