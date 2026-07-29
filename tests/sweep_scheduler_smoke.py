#!/usr/bin/env python3
"""The sweep RUNS BY ITSELF (reveille-architect msg 8558).

grant-smoke and tenancy-smoke already prove _sweep_once does the right thing --
by calling it. That is exactly the gate that let `sweep --loop` ship, pass
review, and never once execute on the live box: nothing invoked it, so grant
expiry was enforced only at the doorway and the 24h idle stop had never fired.

So this gate calls NOTHING. It starts `serve`, plants a condition the sweep must
react to, waits past one interval, and asserts the reaction happened. The only
process that could have done it is the scheduler inside serve.

What it plants: a session recorded as seen last tick, whose grant is long
expired, on a container that does not exist. The tick observes the session gone
(no container -> no live sessions) and must write a DETACH audit line and clear
the sessions_seen row. No docker container is needed for that -- which is the
point: the gate tests the SCHEDULE, and the decisions it schedules are unit
tested in tests/test_reveille_launch.py.
"""
import contextlib
import os
import pathlib
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

REPO = pathlib.Path(__file__).resolve().parent.parent
LAUNCH = REPO / "scripts" / "reveille_launch.py"

SWEEP_SECONDS = 1
DEADLINE = 30  # generous: a docker probe on a cold daemon is not instant


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def plant(db, now_ns):
    """One container, one long-expired grant, one session seen last tick."""
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS containers(
          user TEXT NOT NULL, agent TEXT NOT NULL, repo_url TEXT, container TEXT,
          image TEXT, broker_url TEXT, created_ns INTEGER, PRIMARY KEY(user, agent));
        CREATE TABLE IF NOT EXISTS grants(
          id TEXT PRIMARY KEY, user TEXT, agent TEXT, grantee TEXT, mode TEXT,
          issued_ns INTEGER, expiry_ns INTEGER, revoked_ns INTEGER);
        CREATE TABLE IF NOT EXISTS sessions_seen(
          user TEXT, agent TEXT, session TEXT, first_seen_ns INTEGER,
          PRIMARY KEY(user, agent, session));
    """)
    conn.execute("INSERT OR REPLACE INTO containers(user, agent, container, created_ns) "
                 "VALUES('ana','scout','reveille-ana-scout',?)", (now_ns,))
    conn.execute("INSERT OR REPLACE INTO grants(id, user, agent, grantee, mode, issued_ns,"
                 " expiry_ns, revoked_ns) VALUES('g1','ana','scout','bob',"
                 "'driver',?,?,NULL)", (now_ns - 10**11, now_ns - 10**10))
    conn.execute("INSERT OR REPLACE INTO sessions_seen(user, agent, session, first_seen_ns)"
                 " VALUES('ana','scout','d-g1',?)", (now_ns - 10**10,))
    conn.commit()
    conn.close()


def seen_rows(db):
    conn = sqlite3.connect(db)
    try:
        return conn.execute("SELECT session FROM sessions_seen").fetchall()
    finally:
        conn.close()


def main():
    tmp = tempfile.mkdtemp()
    db = os.path.join(tmp, "launcher.db")
    audit = os.path.join(tmp, "audit.log")
    port = free_port()
    plant(db, time.time_ns())

    env = dict(os.environ, REVEILLE_LAUNCH_DB=db, REVEILLE_LAUNCH_AUDIT=audit,
               REVEILLE_LAUNCH_DATA=os.path.join(tmp, "data"))
    proc = subprocess.Popen(
        [sys.executable, str(LAUNCH), "serve", "--host", "127.0.0.1",
         "--port", str(port), "--sweep-seconds", str(SWEEP_SECONDS)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # The banner must SAY the sweep is scheduled. An operator reading a
        # startup line is how anyone would have caught this the first time.
        banner, deadline = "", time.time() + DEADLINE
        while time.time() < deadline and "sweep" not in banner:
            if proc.poll() is not None:
                raise SystemExit(f"serve exited {proc.returncode}:\n"
                                 f"{banner}{proc.stdout.read()}")
            banner += proc.stdout.readline()
        assert "sweep" in banner and f"every {SWEEP_SECONDS}s" in banner, banner

        # Nobody calls sweep. We just wait.
        acted = False
        while time.time() < deadline:
            if (os.path.exists(audit)
                    and "DETACH" in pathlib.Path(audit).read_text()
                    and not seen_rows(db)):
                acted = True
                break
            time.sleep(0.25)
        assert acted, (
            "serve ran for "
            f"{DEADLINE}s with --sweep-seconds {SWEEP_SECONDS} and the planted "
            "expired grant was never swept: no DETACH in the audit log and the "
            "sessions_seen row is still there. The task works (unit tested) -- "
            "what is missing is the thing that RUNS it.")
        line = [ln for ln in pathlib.Path(audit).read_text().splitlines()
                if "DETACH" in ln][0]
        assert "grant=g1" in line and "grantee=bob" in line, line
        assert "observed=sweep-tick" in line, line  # observation, not event time

        # Second proof, and the one that catches a loop that ticks exactly once:
        # plant again and let the NEXT interval pick it up.
        os.truncate(audit, 0)
        plant(db, time.time_ns())
        deadline = time.time() + DEADLINE
        again = False
        while time.time() < deadline:
            if "DETACH" in pathlib.Path(audit).read_text() and not seen_rows(db):
                again = True
                break
            time.sleep(0.25)
        assert again, ("the sweep ran once and stopped -- a scheduler that fires "
                       "only at boot is a boot task, not an interval")

        print(f"sweep-scheduler OK: serve announced the schedule, then swept a "
              f"planted expired grant TWICE at {SWEEP_SECONDS}s intervals with "
              f"nobody calling `sweep` -- DETACH line carries the grantee and "
              f"says its timestamp is an observation")
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)


if __name__ == "__main__":
    main()
