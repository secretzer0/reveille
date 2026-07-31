"""deploy-preflight refuses a database the migration would refuse -- BEFORE the
deploy stops anything.

The refusal inside migrate() is correct and stays. What this covers is its
TIMING: migrate() runs at broker startup, so without this check the operator
meets a correct refusal at the one moment when acting on it costs downtime
(architect, msg 8946). Same rows, same command, one step earlier.
"""
import os
import pathlib
import subprocess

from reveille import store

SCRIPT = str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "deploy-preflight")


def data_root(tmp_path, sender=None):
    root = tmp_path / "data"
    root.mkdir()
    conn = store.connect(str(root / "broker.db"))
    store.migrate(conn, str(root / "broker.db"))
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','t','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES('r1','room','u1',1)")
    if sender:
        conn.execute("INSERT INTO messages(sender, recipient, subject, body, room, "
                     "ts_ns) VALUES(?,'*','s','b','r1',1)", (sender,))
    conn.close()
    return str(root)


def run(root):
    return subprocess.run(["bash", SCRIPT, root, "no-such-container"],
                          capture_output=True, text=True,
                          env=dict(os.environ), timeout=180)


def test_it_refuses_history_the_backfill_cannot_attribute(tmp_path):
    r = run(data_root(tmp_path, sender="reveille-devops"))
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "reveille-devops" in out, "the refusal must name what it found"
    assert "seed_agent_identities.py" in out, "and the command that clears it"


def test_it_passes_once_the_names_are_claimed(tmp_path):
    root = data_root(tmp_path, sender="reveille-devops")
    conn = store.connect(os.path.join(root, "broker.db"))
    # What the operator does with the OLD broker still up: the seeder only
    # INSERTS, so nothing is down while it runs.
    with store.tx(conn):
        store.claim_unresolved_names(conn, "u1")
    conn.close()
    r = run(root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_a_database_with_no_history_is_not_a_refusal(tmp_path):
    r = run(data_root(tmp_path))
    assert r.returncode == 0, r.stdout + r.stderr
