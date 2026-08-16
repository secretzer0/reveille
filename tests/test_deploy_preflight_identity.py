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


def _proxy(tmp_path, serving):
    """A stand-in for `docker inspect` on the PATH: the preflight only reads the
    running proxy's PROXY_SITE, so a script that prints that env is the whole
    container for this purpose. `docker` itself may not exist where the suite
    runs, which is the point of faking it rather than the point of skipping."""
    b = tmp_path / "bin"
    b.mkdir(exist_ok=True)
    fake = b / "docker"
    fake.write_text("#!/usr/bin/env bash\n"
                    "# only the proxy exists; every other container is unknown\n"
                    "case \"$1 $2 ${@: -1}\" in\n"
                    f"  'inspect -f reveille-proxy') printf 'PROXY_SITE={serving}\\n' ;;\n"
                    "  *) exit 1 ;;\n"
                    "esac\n")
    fake.chmod(0o755)
    return str(b)


def _run_site(tmp_path, serving, site):
    root = tmp_path / "data"
    root.mkdir(exist_ok=True)
    env = dict(os.environ, PATH=_proxy(tmp_path, serving) + os.pathsep + os.environ["PATH"])
    return subprocess.run(["bash", SCRIPT, str(root), "no-such-broker", "reveille-proxy", site],
                          capture_output=True, text=True, env=env, timeout=180)


def test_it_refuses_to_demote_a_hostname_proxy_to_a_bare_port(tmp_path):
    """The 0.2.97 deploy: PROXY_SITE forgotten, caddy recreated on :80, public
    URL dark for nine minutes. Same shape as the SERVER_DATA guard: refuse and
    name the command, before anything stops."""
    r = _run_site(tmp_path, "reveille.mythos.org", ":80")
    assert r.returncode != 0, r.stdout + r.stderr
    out = r.stdout + r.stderr
    assert "reveille.mythos.org" in out and "PROXY_SITE=reveille.mythos.org" in out


def test_the_same_hostname_and_a_bare_port_over_a_bare_port_both_pass(tmp_path):
    assert _run_site(tmp_path, "reveille.mythos.org", "reveille.mythos.org").returncode == 0
    assert _run_site(tmp_path, ":80", ":80").returncode == 0
    assert _run_site(tmp_path, ":80", "reveille.mythos.org").returncode == 0
