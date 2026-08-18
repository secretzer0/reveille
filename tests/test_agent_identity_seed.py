"""The refusal list, and the one-shot that clears it.

The backfill's whole safety property is that it cannot invent an owner: a name
it cannot resolve stops the migration and gets printed. So the two things worth
gating are that the list is COMPLETE (a name history carries but agents does not
is on it) and that it is not OVER-BROAD (people are not agents, and a name
already claimed is not pending).
"""
import importlib.util
import pathlib
import time

from reveille import store

_path = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seed_agent_identities.py"
_spec = importlib.util.spec_from_file_location("seed_agent_identities", str(_path))
seed = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed)


def db(tmp_path):
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','tmelhiser','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES('r1','room','u1',1)")
    return conn, path


def msg(conn, sender, recipient="*", ts=1):
    conn.execute("INSERT INTO messages(sender, recipient, subject, body, room, ts_ns) "
                 "VALUES(?,?,'s','b','r1',?)", (sender, recipient, ts))


def test_a_name_with_history_and_no_row_is_unresolved(tmp_path):
    conn, _ = db(tmp_path)
    msg(conn, "reveille-devops")
    names = [e["name"] for e in store.unresolved_agent_names(conn)]
    assert names == ["reveille-devops"]


def test_a_web_user_is_not_an_agent(tmp_path):
    # People send messages too. A person appearing as a recoverable identity is
    # how a human ends up owned by themselves as an agent.
    conn, _ = db(tmp_path)
    msg(conn, "tmelhiser")
    assert store.unresolved_agent_names(conn) == []


def test_a_claimed_name_leaves_the_list(tmp_path):
    conn, _ = db(tmp_path)
    msg(conn, "reveille-devops")
    conn.execute("INSERT INTO agents(id, owner_id, name, created_ns, retired_ns) "
                 "VALUES('a1','u1','reveille-devops',1,2)")
    assert store.unresolved_agent_names(conn) == []


def test_the_seeder_reports_and_refuses_without_an_assignment(tmp_path, capsys):
    conn, path = db(tmp_path)
    msg(conn, "reveille-devops")
    conn.close()
    assert seed.main([path]) == 1                     # refuses: nothing written
    out = capsys.readouterr()
    assert "reveille-devops" in out.out
    conn = store.connect(path)
    assert conn.execute("SELECT count(*) FROM agents").fetchone()[0] == 0


def test_the_seeder_mints_one_retired_row_per_name_and_is_idempotent(tmp_path):
    conn, path = db(tmp_path)
    msg(conn, "reveille-devops")
    msg(conn, "reveille-architect")
    conn.close()

    assert seed.main([path, "--assign", "tmelhiser"]) == 0
    conn = store.connect(path)
    rows = conn.execute("SELECT name, owner_id, retired_ns FROM agents "
                        "ORDER BY name").fetchall()
    assert [r["name"] for r in rows] == ["reveille-architect", "reveille-devops"]
    assert all(r["owner_id"] == "u1" for r in rows)
    # RETIRED, not live: a live row claims idx_agents_live, so re-minting the
    # name later would collide with a ghost that nothing is running.
    assert all(r["retired_ns"] is not None for r in rows)
    conn.close()

    assert seed.main([path, "--assign", "tmelhiser"]) == 0     # second run
    conn = store.connect(path)
    assert conn.execute("SELECT count(*) FROM agents").fetchone()[0] == 2


def test_the_seeder_refuses_an_owner_who_is_not_a_user(tmp_path):
    conn, path = db(tmp_path)
    msg(conn, "reveille-devops")
    conn.close()
    assert seed.main([path, "--assign", "nobody"]) == 2
    conn = store.connect(path)
    assert conn.execute("SELECT count(*) FROM agents").fetchone()[0] == 0


def test_the_name_a_live_agent_still_uses_is_listed_too(tmp_path):
    """Present is not the same as claimed. An agent working right now with no
    identity row is exactly the case the backfill must not skip -- being alive
    is not ownership."""
    conn, _ = db(tmp_path)
    msg(conn, "reveille-senior-dev", ts=time.time_ns())
    assert [e["name"] for e in store.unresolved_agent_names(conn)] \
        == ["reveille-senior-dev"]
