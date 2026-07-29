#!/usr/bin/env python3
"""DES-002 T3 gate: grants end to end against a real broker + agent container.

What section 5 demands, in order: multi-client mirror, -r enforced server-side,
revoke drops the client <1s, driver-exclusivity race refused naming the holder,
kill-and-reprovision resumes from bus state. Plus the 4.6 expiry sweep and the
4.5.2 audit lines (gate ATTACH harvested, DETACH as observation, REVOKE exact).

Browser stand-in: `docker exec -i ... script -qec "attach-gate attach <tok>"` --
a pty client through the same gate ttyd execs, no browser needed. The token
rides argv exactly as ttyd delivers it (?arg= -> argv); that is the production
shape, not a smoke shortcut.
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import launch_smoke as t2  # noqa: E402  broker/seed/health helpers, reused

REPO = pathlib.Path(__file__).resolve().parent.parent
ROLE = "smoke-grant-agent"
NET = "reveille-grant-smoke"
BROKER = "reveille-grant-broker"
t2.ROLES = [ROLE]
t2.NET = NET
t2.BROKER = BROKER


def launch(launch_db, audit, *args, **kw):
    env = dict(os.environ, REVEILLE_LAUNCH_DB=launch_db, REVEILLE_LAUNCH_AUDIT=audit)
    return subprocess.run([sys.executable, str(REPO / "scripts" / "reveille_launch.py"),
                           *args], env=env, text=True, cwd=REPO, **kw)


def cx(*args, check=True, capture=True):
    return subprocess.run(["docker", "exec", f"rev-{ROLE}", *args], check=check,
                          text=True, capture_output=capture)


def grant(launch_db, audit, grantee, *extra):
    out = launch(launch_db, audit, "grant", ROLE, grantee, *extra,
                 capture_output=True)
    assert out.returncode == 0, f"grant failed: {out.stderr!r}"
    gid = out.stdout.split()[1].rstrip(":")
    token = out.stdout.split("?arg=")[1].split()[0]
    return gid, token


def attach_client(token):
    """A pty client through the gate, like ttyd's exec. stdin pipe = keyboard."""
    # ttyd exports TERM for its clients; docker exec does not, and a tmux client
    # without TERM dies at attach ("terminal does not support clear").
    return subprocess.Popen(
        ["docker", "exec", "-i", "-e", "TERM=xterm-256color", f"rev-{ROLE}",
         "script", "-qec", f"attach-gate attach {token}", "/dev/null"],
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_session(name, present=True, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = cx("tmux", "has-session", "-t", name, check=False)
        if (r.returncode == 0) == present:
            return
        time.sleep(0.3)
    raise SystemExit(f"session {name}: never became {'present' if present else 'absent'}")


def pane(target="agent:probe-sh"):
    return cx("tmux", "capture-pane", "-pt", target).stdout


def type_into(client, session, text, timeout=8):
    """Send keystrokes through a client's pty, focused on the probe-sh window."""
    cx("tmux", "select-window", "-t", f"{session}:probe-sh")
    client.stdin.write((text + "\n").encode())
    client.stdin.flush()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if text in pane():
            return True
        time.sleep(0.3)
    return False


def main():
    port = t2.free_port()
    tmp = tempfile.mkdtemp()
    data = os.path.join(tmp, "data")
    os.makedirs(data)
    secrets = t2.seed(os.path.join(data, "broker.db"))
    db = os.path.join(tmp, "launcher.db")
    audit = os.path.join(tmp, "audit.log")

    t2.docker("rm", "-f", f"rev-{ROLE}", check=False)
    t2.docker("volume", "rm", f"rev-{ROLE}-claude", check=False)
    t2.docker("network", "create", NET, check=False)
    t2.docker("rm", "-f", BROKER, check=False)
    t2.docker("run", "-d", "--name", BROKER, "--network", NET,
              "-p", f"{port}:8765", "-e", "REVEILLE_DB=/data/broker.db",
              "-v", f"{data}:/data", t2.server_image())
    broker_url = f"http://{BROKER}:8765"
    health_url = f"http://127.0.0.1:{port}"
    minted = []
    try:
        t2.wait_health(port)
        r = launch(db, audit, "new", ROLE, "/nonexistent", "--broker", broker_url,
                   "--health-url", health_url, "--network", NET,
                   "--boot-cmd", "agent-probe", "--timeout", "150",
                   input=secrets[ROLE] + "\n")
        assert r.returncode == 0, "provision failed"
        # A bash window to type into: agent-probe's pane echoes nothing.
        cx("tmux", "new-window", "-t", "agent:", "-n", "probe-sh")

        # -- driver: d-<id> handle, writable ------------------------------------
        g1, tok1 = grant(db, audit, "alice", "--mode", "driver")
        minted.append(tok1)
        d1 = attach_client(tok1)
        wait_session(f"d-{g1}")
        assert type_into(d1, f"d-{g1}", "echo DRIVER-WAS-HERE"), \
            "driver keystrokes never reached the pane"

        # -- exclusivity: second driver refused NAMING the holder (4.3) ---------
        g2, tok2 = grant(db, audit, "bob", "--mode", "driver")
        minted.append(tok2)
        refused = cx("attach-gate", "attach", tok2, check=False)
        assert refused.returncode != 0 and g1 in refused.stderr, \
            f"second driver not refused naming {g1}: {refused.stderr!r}"

        # -- flip on: pairing sanctioned; flip off restores the wall ------------
        assert launch(db, audit, "flip", ROLE, "on").returncode == 0
        d2 = attach_client(tok2)
        wait_session(f"d-{g2}")
        assert launch(db, audit, "flip", ROLE, "off").returncode == 0

        # -- revoke drops the attached client <1s --------------------------------
        t0 = time.monotonic()
        assert launch(db, audit, "revoke", ROLE, g2).returncode == 0
        d2.wait(timeout=5)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"revoke->drop took {elapsed:.2f}s (gate is <1s)"
        wait_session(f"d-{g2}", present=False)

        # -- viewer: mirror, and -r enforced SERVER-side -------------------------
        g3, tok3 = grant(db, audit, "carol")  # default viewer
        minted.append(tok3)
        v3 = attach_client(tok3)
        wait_session(f"v-{g3}")
        assert "DRIVER-WAS-HERE" in cx(
            "tmux", "capture-pane", "-pt", f"v-{g3}:probe-sh").stdout, \
            "viewer session does not mirror the shared window"
        assert not type_into(v3, f"v-{g3}", "echo INTRUDER", timeout=2), \
            "viewer keystrokes reached the pane -- -r not enforced"

        # -- audit: sweep harvests gate ATTACH lines; detach is an observation ---
        launch(db, audit, "sweep")
        log = pathlib.Path(audit).read_text()
        assert f"ATTACH role={ROLE} grant={g1} mode=driver grantee=alice src=gate" in log
        assert f"ATTACH role={ROLE} grant={g3} mode=viewer grantee=carol src=gate" in log
        assert f"REVOKE role={ROLE} grant={g2}" in log

        cx("tmux", "detach-client", "-s", f"v-{g3}")  # browser tab closes
        v3.wait(timeout=10)
        wait_session(f"v-{g3}", present=False)  # destroy-unattached reaped it
        launch(db, audit, "sweep")
        log = pathlib.Path(audit).read_text()
        assert f"DETACH role={ROLE} grant={g3}" in log and "observed=sweep-tick" in log

        # -- expiry sweep (4.6): attached past expiry dies on the tick -----------
        g4, tok4 = grant(db, audit, "dave", "--ttl", "1")
        minted.append(tok4)
        v4 = attach_client(tok4)
        wait_session(f"v-{g4}")
        time.sleep(2)
        launch(db, audit, "sweep")
        v4.wait(timeout=5)
        wait_session(f"v-{g4}", present=False)
        log = pathlib.Path(audit).read_text()
        assert f"KILL role={ROLE} grant={g4}" in log and "reason=expired" in log

        # -- no token anywhere durable (4.5.2) ------------------------------------
        blob = pathlib.Path(db).read_bytes()
        for tok in minted:
            assert tok.encode() not in blob, "minted token LEAKED into launcher.db"
            assert tok not in log, "minted token LEAKED into audit.log"

        # -- kill-and-reprovision resumes from bus state --------------------------
        launch(db, audit, "revoke", ROLE, g1)
        launch(db, audit, "destroy", ROLE, capture_output=True)
        gone = launch(db, audit, "grants", ROLE, capture_output=True)
        assert "no grants" in gone.stdout, "grants must die with the container (4.5)"
        r = launch(db, audit, "new", ROLE, "/nonexistent", "--broker", broker_url,
                   "--health-url", health_url, "--network", NET,
                   "--boot-cmd", "agent-probe", "--timeout", "150",
                   input=secrets[ROLE] + "\n")
        assert r.returncode == 0, "re-provision after kill did not resume"

        print("grant-smoke OK: mirror, -r server-side, revoke "
              f"{elapsed * 1000:.0f}ms, exclusivity named {g1}, flip, expiry sweep, "
              "audit honest, tokens nowhere durable, reprovision resumed")
    finally:
        launch(db, audit, "destroy", ROLE, "--purge", capture_output=True)
        t2.docker("rm", "-f", BROKER, check=False)
        t2.docker("network", "rm", NET, check=False)


if __name__ == "__main__":
    main()
