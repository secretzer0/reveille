#!/usr/bin/env python3
"""DES-002 T2 gate: provision one agent container through reveille-launch, end to end,
against a REAL broker on a scratch db -- assert the launcher sees it live+connected,
that launcher.db never held the token, then destroy it.

agent-probe stands in for `claude reveille` (join + hold the waiter) so the gate needs
no Anthropic login. Zero broker changes: this touches only src/agentbus via store.seed;
the broker code is unmodified, so smoke_ws stays green in the same `make build`.
"""
import contextlib
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
from agentbus import store  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCHER = str(REPO / "scripts" / "reveille_launch.py")
ROLE = "smoke-agent"


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_health(port, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with contextlib.suppress(Exception):
            if urllib.request.urlopen(f"http://127.0.0.1:{port}/health",
                                      timeout=1).read() == b"ok":
                return
        time.sleep(0.5)
    raise SystemExit("broker never came up")


def seed(db):
    """Mint a BOUND token for ROLE with one room -- exactly what an operator does in
    /ui before pasting the token into `reveille-launch new`."""
    conn = store.connect(db)
    store.migrate(conn, db)
    owner = store.create_user(conn, "smoke", "smoke-pw-not-a-real-secret")
    room = store.create_room(conn, owner["id"], "smoke")
    tok = store.create_token(conn, owner["id"], ROLE, agent_name=ROLE)
    store.assign_room(conn, tok["id"], room["id"], owner["id"])
    conn.close()
    return tok["secret"]


def launch(launch_db, *args, **kw):
    env = dict(os.environ, REVEILLE_LAUNCH_DB=launch_db)
    return subprocess.run([sys.executable, LAUNCHER, *args], env=env,
                          text=True, cwd=REPO, **kw)


def main():
    port = free_port()
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "broker.db")
    launch_db = os.path.join(tmp, "launcher.db")
    secret = seed(db)
    broker = f"http://127.0.0.1:{port}"
    env = dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    proc = subprocess.Popen(["agentbus-daemon"], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_health(port)

        # Provision. Token rides stdin, never argv. /nonexistent repo makes the
        # entrypoint's clone fail fast and continue -- the health gate is join+arm.
        r = launch(launch_db, "new", ROLE, "/nonexistent", "--broker", broker,
                   "--network", "host", "--boot-cmd", "agent-probe",
                   "--timeout", "150", input=secret + "\n")
        assert r.returncode == 0, f"provision reported unhealthy (exit {r.returncode})"

        # The T2 invariant: the token never landed in launcher.db.
        assert secret.encode() not in pathlib.Path(launch_db).read_bytes(), \
            "TOKEN LEAKED into launcher.db"

        ls = launch(launch_db, "ls", capture_output=True)
        assert ROLE in ls.stdout and "running" in ls.stdout, f"ls: {ls.stdout!r}"

        print("launch-smoke OK: provisioned, live+connected, launcher.db token-free")
    finally:
        launch(launch_db, "destroy", ROLE, "--purge", capture_output=True)
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
