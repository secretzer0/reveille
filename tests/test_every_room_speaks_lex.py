"""Every room speaks LEX (ruling 98868dc2: default-ON, no agent opt-out, humans
exempt). A room is born with the seed as its v1; v49 -> v50 gives every room
that had none the same. And a LEX room can still be emptied: a vote on a
deleted proposal, and a deleted room's table, go with what they belong to --
on 0.2.318 both were FOREIGN KEY failures waiting for the first purge."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import lex, store  # noqa: E402
import ident  # noqa: E402


@pytest.fixture
def c(tmp_path):
    path = str(tmp_path / "b.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    return conn


def _admin(c):
    return store.setup_first_admin(c, "travis", "hunter2hunter2")["id"]


def _agent(c, name, rid):
    ident.join(c, name, rid)
    return store.agent_principal(c.execute("SELECT id FROM agents WHERE name=?", (name,))
                                 .fetchone()[0])


def test_a_room_is_born_speaking_the_seed(c):
    admin = _admin(c)
    rid = store.create_room(c, admin, "bridge")["id"]
    t = store.lex_table(c, rid)
    assert t["ver"] == 1 and t["table"] == lex.seed() and t["adopted_by"] == store.LEX_SEEDED_BY
    quark = _agent(c, "quark", rid)
    with pytest.raises(store.BusError, match="not LEX v1"):
        store.send(c, quark, "*", "hello there", room=rid)
    store.send(c, store.user_principal(admin), "*", "hello there", room=rid)


def test_the_migration_gives_every_room_without_a_lex_the_seed_and_no_other(c, tmp_path):
    admin = _admin(c)
    bare = store.create_room(c, admin, "bare")["id"]
    own = store.create_room(c, admin, "own")["id"]
    store.lex_adopt(c, own, 'L?P<n>="pull request {}"', ver=9, adopted_by="a")
    c.execute("DELETE FROM lex_versions WHERE room=?", (bare,))   # as every room was at v49
    c.execute("PRAGMA user_version=49")
    store.migrate(c, str(tmp_path / "b.db"))
    assert store.lex_table(c, bare)["ver"] == 1
    assert store.lex_table(c, bare)["adopted_by"] == store.LEX_SEEDED_BY
    assert store.lex_table(c, own)["ver"] == 9


def test_a_lex_room_with_a_voted_proposal_can_still_be_purged(c):
    admin = _admin(c)
    rid = store.create_room(c, admin, "bridge")["id"]
    quark, odo = _agent(c, "quark", rid), _agent(c, "odo", rid)
    p = store.send(c, quark, "*", 'L?P<n>="pull request {}"', room=rid)
    store.lex_vote(c, odo, p["id"], True, {rid: "bridge"})
    store.purge_room(c, rid, admin)
    assert c.execute("SELECT COUNT(*) FROM lex_versions WHERE room=?", (rid,)).fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM lex_votes").fetchone()[0] == 0
