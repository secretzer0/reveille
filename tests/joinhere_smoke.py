#!/usr/bin/env python3
"""DES-003 W2 gate: `join-here` from a clean shell.

A scratch HOME stands in for the clean user: join-here runs against a real
broker with the token on STDIN, then the gate's two halves are asserted --

1. Zero manual steps to reachability: source the fragment, run the
   credential-free stand-in (join + waked attach, agent-probe's recipe), and
   presence reports live+connected.
2. The token is in EXACTLY ONE file: the 0600 env fragment. Every other byte
   join-here wrote (claude config, hook settings, bashrc, launcher db, spool)
   is scanned and must not contain it; the MCP registration must carry the
   ${REVEILLE_TOKEN} template, not a value.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
ROLE = "smoke-join"


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


async def _join(port, token):
    async with streamablehttp_client(
            f"http://127.0.0.1:{port}/mcp",
            headers={"X-Agent": ROLE,
                     "Authorization": f"Bearer {token}"}) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            await s.call_tool("join", {"url": f"http://127.0.0.1:{port}"})


def presence(port, token):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/presence",
        headers={"Authorization": f"Bearer {token}", "X-Agent": ROLE})
    return json.load(urllib.request.urlopen(req, timeout=3))["agents"]


def main():
    port = free_port()
    db = os.path.join(tempfile.mkdtemp(), "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    owner = store.create_user(conn, "smoke", "smoke-pw-not-a-real-secret")
    room = store.create_room(conn, owner["id"], "smoke")
    tok = store.create_token(conn, owner["id"], ROLE, agent_name=ROLE, create=True)
    store.assign_room(conn, tok["id"], room["id"], owner["id"])
    secret = tok["secret"]
    conn.close()
    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    broker = subprocess.Popen(["reveille-daemon"], env=env,
                              stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)
    home = tempfile.mkdtemp()   # the clean user
    waked = None
    try:
        wait_health(port)
        jenv = dict(os.environ, HOME=home,
                    REVEILLE_LAUNCH_DB=os.path.join(home, "launcher.db"))
        jenv.pop("REVEILLE_SPOOL", None)   # the clean user has no overrides
        r = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "reveille_launch.py"),
             "join-here", ROLE, "--broker", f"http://127.0.0.1:{port}"],
            input=secret + "\n", env=jenv, capture_output=True, text=True,
            cwd=REPO)
        assert r.returncode == 0, f"join-here failed: {r.stderr!r}"
        for step in ("env", "register", "hook", "path", "spool"):
            assert f"[ok] {step}" in r.stdout, f"checklist step {step} missing"

        # -- the token lives in exactly one file, and that file is 0600 -------
        frag = os.path.join(home, ".reveille", f"{ROLE}.env")
        assert stat.S_IMODE(os.stat(frag).st_mode) == 0o600
        assert secret in open(frag).read()
        offenders = []
        for root, _dirs, files in os.walk(home):
            for name in files:
                p = os.path.join(root, name)
                if p == frag:
                    continue
                with contextlib.suppress(OSError):
                    if secret.encode() in open(p, "rb").read():
                        offenders.append(p)
        assert not offenders, f"token LEAKED into: {offenders}"
        # registration carries the template, never the value
        cfg = open(os.path.join(home, ".claude.json")).read()
        assert "${REVEILLE_TOKEN" in cfg and secret not in cfg
        # hook installed in the clean user's settings
        hooks = open(os.path.join(home, ".claude", "settings.json")).read()
        assert "agent-stop-hook" in hooks
        # PATH links exist and resolve
        for tool in ("wake", "wake-watch", "reveille-waked"):
            assert os.path.exists(os.path.join(home, ".local", "bin", tool))

        # -- zero manual steps to reachability: fragment -> join -> waked -----
        frag_env = {}
        for ln in open(frag):
            k, v = ln.replace("export ", "").strip().split("=", 1)
            frag_env[k] = v
        assert frag_env["REVEILLE_AGENT_ROLE"] == ROLE
        asyncio.run(_join(port, frag_env["REVEILLE_TOKEN"]))   # live
        wenv = dict(os.environ, HOME=home,
                    REVEILLE_TOKEN=frag_env["REVEILLE_TOKEN"],
                    PYTHONPATH=str(REPO / "src"))
        wenv.pop("REVEILLE_SPOOL", None)
        waked = subprocess.Popen(
            [os.path.join(home, ".local", "bin", "reveille-waked"),
             "--url", f"ws://127.0.0.1:{port}/wake", "--name", ROLE],
            env=wenv, stderr=subprocess.DEVNULL)                # connected
        deadline = time.time() + 20
        ok = False
        while time.time() < deadline and not ok:
            ok = any(a["name"] == ROLE and a["live"] and a["connected"]
                     for a in presence(port, secret))
            time.sleep(0.5)
        assert ok, "never reached live+connected from the bootstrap alone"

        print("joinhere-smoke OK: checklist walked, token in exactly one file "
              "(0600), config carries the env template, live+connected from "
              "the fragment + PATH links alone")
    finally:
        if waked and waked.poll() is None:
            waked.terminate()
        broker.terminate()


if __name__ == "__main__":
    main()
