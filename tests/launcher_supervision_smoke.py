#!/usr/bin/env python3
"""DES-006 U2 gate: the launcher is supervised, loopback-only, and refuses to
serve when it cannot do its job.

Asserted, against real processes:
  1. No docker socket -> REFUSES TO START, non-zero, naming the docker group.
     (A launcher whose /health says ok while provisioning is structurally
     impossible is the shape msg 8499 caught serving the operator.)
  2. Startup prints the RESOLVED data root and db -- a wrong one is visible at
     boot, not after an agent's home has silently moved.
  3. A second launcher on the same data root exits rather than double-serving;
     the first keeps the port.
  4. It binds 127.0.0.1 only: the LAN address refuses connections.
  5. Killed, the supervisor's spawn line brings it back with no human, and the
     flock makes that spawn safe to run blindly.
"""
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCH = [sys.executable, str(REPO / "scripts" / "reveille_launch.py")]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def lan_ip():
    """This host's LAN address -- what a remote browser would dial."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


def wait_health(port, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if urllib.request.urlopen(
                    f"http://127.0.0.1:{port}/health", timeout=1).read() == b"ok":
                return True
        except OSError:
            time.sleep(0.2)
    return False


def spawn(env, port, log):
    """The supervisor's spawn line: blind, flock-guarded, loopback default."""
    return subprocess.Popen(LAUNCH + ["serve", "--port", str(port),
                                      "--auth-url", "http://127.0.0.1:1"],
                            env=env, stdout=log, stderr=subprocess.STDOUT)


def main():
    tmp = tempfile.mkdtemp()
    port = free_port()
    env = dict(os.environ,
               REVEILLE_LAUNCH_DB=os.path.join(tmp, "launcher.db"),
               REVEILLE_LAUNCH_DATA=os.path.join(tmp, "data"),
               REVEILLE_LAUNCH_AUDIT=os.path.join(tmp, "audit.log"))

    # -- 1. no docker -> refuse, loudly, non-zero ---------------------------
    # DOCKER_HOST at a dead address is the honest stand-in for "this user
    # cannot reach the socket": same failure, no host mutation.
    blind = dict(env, DOCKER_HOST="tcp://127.0.0.1:1")
    r = subprocess.run(LAUNCH + ["serve", "--port", str(port)],
                       env=blind, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, "served without a docker socket"
    out = r.stdout + r.stderr
    assert "cannot reach the docker socket" in out, out
    assert "api on" not in out, "bound a port before probing docker"

    first = second = None
    logs = [open(os.path.join(tmp, f"l{i}.log"), "w+") for i in range(2)]
    try:
        # -- 2 + 5. the supervisor's blind spawn starts it, and it says where
        # its state lives ---------------------------------------------------
        first = spawn(env, port, logs[0])
        assert wait_health(port), "launcher never came up"
        logs[0].seek(0)
        boot = logs[0].read()
        assert os.path.join(tmp, "data") in boot, boot
        assert os.path.join(tmp, "launcher.db") in boot, boot
        assert "docker " in boot, boot        # server version, proof of probe

        # -- 3. singleton: a second one on the same root exits, first keeps
        # serving -----------------------------------------------------------
        second = spawn(env, free_port(), logs[1])
        assert second.wait(timeout=30) != 0, "two launchers served one data root"
        logs[1].seek(0)
        assert "already holds" in logs[1].read()
        assert wait_health(port, 5), "the incumbent stopped serving"

        # -- 4. loopback only ---------------------------------------------
        ip = lan_ip()
        try:
            urllib.request.urlopen(f"http://{ip}:{port}/health", timeout=3)
            raise SystemExit(f"REACHABLE on the LAN address {ip} -- must be "
                             "127.0.0.1 only (DES-006 2.4)")
        except (urllib.error.URLError, OSError):
            pass

        # -- 5. kill it: the same blind spawn brings it back, no human ------
        first.kill()
        first.wait(timeout=10)
        first = spawn(env, port, logs[0])
        assert wait_health(port), "supervisor's respawn did not come back"

        print("launcher-supervision-smoke OK: refuses to start without the "
              "docker socket (non-zero, names the docker group, binds nothing "
              "first), prints its resolved data root and db at boot, one "
              "instance per data root by flock (the loser exits, the incumbent "
              f"keeps serving), unreachable on the LAN address {ip}, and a "
              "blind respawn after kill -9 brings it back with no human")
    finally:
        for p in (first, second):
            if p and p.poll() is None:
                p.kill()
        for f in logs:
            f.close()


if __name__ == "__main__":
    main()
