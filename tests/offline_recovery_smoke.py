#!/usr/bin/env python3
"""An agent that ends a turn while the broker is DOWN still recovers by itself
when the broker comes back -- no human keystroke (reveille-architect, msg 8573).

The defect this locks out: the Stop hook opened with `curl $url/version || exit 0`
-- fail-open, so a broker outage could not block every agent's turn. Right
intent, fatal placement: it sat ABOVE the supervision blocks, so an unreachable
broker meant the turn ended with NO WATCHER ARMED and NO WAKED SPAWNED. The
outage disabled the recovery mechanism at the exact moment it was needed, and
when the bus came back nothing re-armed anything, because the only thing that
would runs at a turn boundary the agent could no longer reach.
reveille-senior-ui-ux sat deaf for 21 hours that way, on a healthy bus, with 44
rings in its spool.

Sequence, in order, with real processes:
  1. Broker DOWN (nothing listening). Run the hook -- an ending turn.
  2. Assert it did the LOCAL work anyway: waked spawned against the dead URL,
     and the stop BLOCKED with the arm line, because a watcher reads a local
     directory and needs no bus.
  3. Bring the broker UP at that same address. Nobody touches the agent.
  4. Assert recovery: the waked spawned in step 1 connects on its own and a
     unicast message becomes a ring FILE in the spool.
  5. Assert the watcher fires on that ring -- the arm line the hook printed, run
     as the agent would run it, exits when the ring lands.
"""
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

REPO = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))
HOOK = REPO / "scripts" / "agent-stop-hook"
ROLE = "offline-gate"
from reveille import store  # noqa: E402


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


def rings(spool):
    d = spool / "new"
    return sorted(p.name for p in d.iterdir()) if d.is_dir() else []


def main():
    tmp = pathlib.Path(tempfile.mkdtemp())
    home = tmp / "home"
    home.mkdir()
    spool = tmp / "spool" / ROLE
    port = free_port()          # nothing is listening on it yet: the outage
    url = f"http://127.0.0.1:{port}"

    db = str(tmp / "broker.db")
    conn = store.connect(db)
    store.migrate(conn, db)
    u = store.setup_first_admin(conn, "ana", "hunter2hunter2")
    room = store.create_room(conn, u["id"], "gate")
    tok = store.create_token(conn, u["id"], ROLE, agent_name=ROLE)
    store.assign_room(conn, tok["id"], room["id"], u["id"])
    admin = store.create_token(conn, u["id"], "ana", agent_name="ana")
    store.assign_room(conn, admin["id"], room["id"], u["id"])
    # The agent had JOINED before the outage -- that is the scenario. Membership
    # is what makes a unicast deliverable; a waked socket-holder is deliberately
    # invisible to presence and does not count as joined, so without this the
    # broker would refuse the recovery probe with "is it joined?" and the gate
    # would be measuring the probe, not the recovery.
    store.join(conn, ROLE, tag=ROLE, room_id=room["id"], token_id=tok["id"])
    conn.commit()
    conn.close()

    env = dict(os.environ, HOME=str(home), REVEILLE_AGENT_ROLE=ROLE,
               REVEILLE_URL=url, REVEILLE_TOKEN=tok["secret"],
               REVEILLE_SPOOL=str(tmp / "spool"))
    env["PATH"] = str(REPO / ".venv" / "bin") + os.pathsep + env["PATH"]
    broker = None
    try:
        # -- 1+2. a turn ends while the broker is unreachable -----------------
        assert not wait_health(port, timeout=0.5), "the port must be dead here"
        r = subprocess.run([str(HOOK)], input="{}", env=env, capture_output=True,
                           text=True, timeout=30)
        assert r.returncode == 0, (r.returncode, r.stderr)
        verdict = json.loads(r.stdout)
        assert verdict.get("decision") == "block", r.stdout
        assert "wake-watch" in verdict["reason"] and ROLE in verdict["reason"], \
            "the block must name the exact arm line -- a blocked stop with no "\
            "instruction is a wedge, not a backstop"
        time.sleep(3)
        pg = subprocess.run(["pgrep", "-f", f"reveille-waked.*{ROLE}"],
                            capture_output=True, text=True)
        waked_pids = pg.stdout.split()
        # Two independent witnesses, because a pgrep pattern can match the wrong
        # thing (a shell whose command line happens to quote it): a live pid AND
        # the daemon's own log showing it dialling the dead address.
        log = spool / "waked.log"
        assert waked_pids and log.exists() and "retrying" in log.read_text(), \
            ("no waked spawned against a dead broker. This is the whole defect: "
             "the socket holder retries forever, so spawning it during an outage "
             "is what makes the agent already waiting when the bus returns.")
        assert rings(spool) == [], "no rings can exist yet -- the broker is down"

        # -- 3. the broker comes back at the same address ---------------------
        broker = subprocess.Popen(
            ["reveille-daemon"],
            env=dict(os.environ, REVEILLE_DB=db, REVEILLE_PORT=str(port),
                     REVEILLE_HOST="127.0.0.1",
                     PATH=str(REPO / ".venv" / "bin") + os.pathsep + os.environ["PATH"]),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        assert wait_health(port), "the repaired broker never came up"

        # -- 4. recovery, with nobody touching the agent ----------------------
        # The waked from step 1 must attach on its own; then a unicast becomes a
        # ring file. Retried because the connect loop sleeps between attempts.
        deadline = time.time() + 60
        while time.time() < deadline and not rings(spool):
            req = urllib.request.Request(
                f"{url}/send", method="POST",
                data=json.dumps({"from": "ana", "to": ROLE,
                                 "subject": "are you there",
                                 "body": "recovery probe"}).encode(),
                headers={"Authorization": f"Bearer {admin['secret']}",
                         "Content-Type": "application/json"})
            with contextlib.suppress(Exception):
                urllib.request.urlopen(req, timeout=5).read()
            time.sleep(2)   # the connect loop sleeps between attempts; so do we
        assert rings(spool), (
            "the agent never recovered: the broker is healthy again and a unicast "
            "produced no ring, so the waiter spawned during the outage never "
            "attached. That is 21 hours of deafness on a working bus.")

        # -- 5. the watcher the hook told it to arm fires on that ring --------
        watcher = subprocess.Popen([str(REPO / ".venv" / "bin" / "wake-watch"), ROLE],
                                   env=env, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True)
        try:
            watcher.wait(timeout=30)
        except subprocess.TimeoutExpired:
            watcher.kill()
            raise SystemExit("wake-watch did not fire on a spooled ring -- the "
                             "arm line the hook prints is what turns a ring into "
                             "the agent's next turn") from None
        assert watcher.returncode == 0, watcher.returncode

        print(f"offline-recovery OK: a turn that ended with the broker DOWN still "
              f"spawned the waiter and blocked with the arm line; the broker came "
              f"back at the same address and the agent recovered with no "
              f"keystroke -- {len(rings(spool))} ring(s) spooled and wake-watch "
              f"fired on them")
    finally:
        # Exact pids, never pkill: a name-matched kill on this host reaches into
        # containers across the pid namespace (paid-for lesson), and this gate
        # runs on the same box as the real fleet.
        for pid in locals().get("waked_pids", []):
            with contextlib.suppress(ProcessLookupError, ValueError):
                os.kill(int(pid), 15)
        if broker:
            broker.terminate()
            with contextlib.suppress(Exception):
                broker.wait(timeout=5)


if __name__ == "__main__":
    main()
