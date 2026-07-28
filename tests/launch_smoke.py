#!/usr/bin/env python3
"""DES-002 T2 gate: provision agent containers through reveille-launch, end to end,
on a user-defined docker network exactly as production runs (4.2) -- broker reached by
container DNS, each agent on its own namespace.

Provisions TWO roles and asserts BOTH reach live+connected: the second agent is the
test that catches host-networking's 7681 collision (msg 8402). agent-probe stands in
for `claude reveille` (join + hold the waiter) so the gate needs no Anthropic login.

Zero broker changes: this touches only src/agentbus via store.seed; the broker code is
unmodified, so smoke_ws stays green in the same `make build`.
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
NET = "reveille-smoke"
BROKER = "reveille-smoke-broker"
ROLES = ["smoke-agent-1", "smoke-agent-2"]


def server_image():
    for line in (REPO / "pyproject.toml").read_text().splitlines():
        if line.startswith("version"):
            return f"reveille-server:{line.split(chr(34))[1]}"
    raise SystemExit("could not read version from pyproject.toml")


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
    """Mint BOUND tokens for every role -- what an operator does in /ui before pasting
    each into `reveille-launch new`."""
    conn = store.connect(db)
    store.migrate(conn, db)
    owner = store.create_user(conn, "smoke", "smoke-pw-not-a-real-secret")
    room = store.create_room(conn, owner["id"], "smoke")
    secrets = {}
    for role in ROLES:
        tok = store.create_token(conn, owner["id"], role, agent_name=role)
        store.assign_room(conn, tok["id"], room["id"], owner["id"])
        secrets[role] = tok["secret"]
    conn.close()
    return secrets


def launch(launch_db, *args, **kw):
    env = dict(os.environ, REVEILLE_LAUNCH_DB=launch_db)
    return subprocess.run([sys.executable, LAUNCHER, *args], env=env,
                          text=True, cwd=REPO, **kw)


def docker(*args, check=True):
    return subprocess.run(["docker", *args], check=check,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main():
    port = free_port()
    tmp = tempfile.mkdtemp()
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    db = os.path.join(data, "broker.db")
    launch_db = os.path.join(tmp, "launcher.db")
    secrets = seed(db)

    # Idempotent start: a crashed prior run can leave containers/volumes behind, and
    # the new refuse-unless-replace guard would then reject provisioning. Clear ours.
    for role in ROLES:
        docker("rm", "-f", f"rev-{role}", check=False)
        docker("volume", "rm", f"rev-{role}-claude", check=False)
    docker("network", "create", NET, check=False)
    docker("rm", "-f", BROKER, check=False)
    docker("run", "-d", "--name", BROKER, "--network", NET, "-p", f"{port}:8765",
           "-e", "REVEILLE_DB=/data/broker.db", "-v", f"{data}:/data", server_image())
    broker_url = f"http://{BROKER}:8765"
    health_url = f"http://127.0.0.1:{port}"
    try:
        wait_health(port)

        for role in ROLES:
            r = launch(launch_db, "new", role, "/nonexistent", "--broker", broker_url,
                       "--health-url", health_url, "--network", NET,
                       "--boot-cmd", "agent-probe", "--timeout", "150",
                       input=secrets[role] + "\n")
            assert r.returncode == 0, f"{role} reported unhealthy (exit {r.returncode})"

        # Both agents are now running on one shared network -- the 7681 collision that
        # host-networking would cause (msg 8402) shows up here or nowhere.
        assert all(secrets[role].encode() not in pathlib.Path(launch_db).read_bytes()
                   for role in ROLES), "TOKEN LEAKED into launcher.db"
        ls = launch(launch_db, "ls", capture_output=True)
        assert all(role in ls.stdout for role in ROLES), f"ls: {ls.stdout!r}"
        assert ls.stdout.count("running") == len(ROLES), f"not all running: {ls.stdout!r}"

        # Re-provision without --replace is refused (the unprompted-destroy guard).
        refused = launch(launch_db, "new", ROLES[0], "/nonexistent", "--broker",
                         broker_url, "--network", NET, input="x\n",
                         capture_output=True)
        assert refused.returncode != 0 and "already exists" in refused.stderr, \
            f"re-provision should refuse without --replace: {refused.stderr!r}"

        print(f"launch-smoke OK: {len(ROLES)} agents live+connected on {NET}, "
              "no port collision, launcher.db token-free, re-provision guarded")
    finally:
        for role in ROLES:
            launch(launch_db, "destroy", role, "--purge", capture_output=True)
        docker("rm", "-f", BROKER, check=False)
        docker("network", "rm", NET, check=False)


if __name__ == "__main__":
    main()
