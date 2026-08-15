"""The one-time identity merge re-points everything and loses nothing.

DES-011 section 7. The site list is the thing under test: a merge that misses
a site leaves rows attributed to an id nobody can reach, and the site most
easily missed is not a column -- state notes live in memories.scope as the
string 'agent:<id>'.
"""
import importlib.util
from importlib.machinery import SourceFileLoader
import os
import pathlib
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402


def _tool():
    # The tool ships without a .py suffix (it is a command), so the loader is
    # named rather than inferred -- spec_from_file_location returns None here.
    root = pathlib.Path(__file__).resolve().parent.parent
    path = str(root / "scripts" / "identity-merge")
    spec = importlib.util.spec_from_file_location(
        "identity_merge", path, loader=SourceFileLoader("identity_merge", path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _two_bodies():
    """One owner, one room, two live identities -- the incident's shape."""
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "Reveille")
    loser = store.create_token(c, admin["id"], "a", agent_name="architect",
                               create=True)
    keep = store.create_token(c, admin["id"], "b",
                              agent_name="reveille-architect", create=True)
    for t in (loser, keep):
        store.assign_room(c, t["id"], room["id"], admin["id"])
    return c, path, admin, room, loser, keep


def test_the_merge_repoints_every_site_including_the_scope_string():
    c, path, admin, room, loser, keep = _two_bodies()
    m = _tool()
    src = m.live_agent(c, "architect")
    dst = m.live_agent(c, "reveille-architect")

    # A message from the losing body, and a state note in ITS scope -- the two
    # planes that carry an agent's history.
    c.execute("INSERT INTO messages(sender, recipient, subject, body, room, "
              "sender_agent_id, ts_ns) VALUES('architect','*','s','b',?,?,1)",
              (room["id"], src["id"]))
    c.execute("INSERT INTO memories(id, uid, kind, scope, fact, author, "
              "author_agent_id, status, created_ns) "
              "VALUES(1,'m1','state',?,'f','architect',?,'live',1)",
              (f"agent:{src['id']}", src["id"]))

    before = m.counts(c, src["id"])
    assert before["messages.sender_agent_id"] == 1
    assert before["memories.scope(state)"] == 1

    c.execute("BEGIN IMMEDIATE")
    m.merge(c, src, dst)
    c.execute("COMMIT")

    # Nothing points at the merged-away id any more...
    after = m.counts(c, src["id"])
    assert not any(v for k, v in after.items() if not k.startswith("members"))
    # ...and the survivor holds it all, scope string included.
    assert c.execute("SELECT sender_agent_id FROM messages").fetchone()[0] == dst["id"]
    assert c.execute("SELECT scope FROM memories WHERE uid='m1'").fetchone()[0] \
        == f"agent:{dst['id']}"
    assert c.execute("SELECT author_agent_id FROM memories WHERE uid='m1'"
                     ).fetchone()[0] == dst["id"]


def test_the_loser_is_retired_and_never_deleted():
    c, path, admin, room, loser, keep = _two_bodies()
    m = _tool()
    src = m.live_agent(c, "architect")
    c.execute("BEGIN IMMEDIATE")
    m.merge(c, src, m.live_agent(c, "reveille-architect"))
    c.execute("COMMIT")

    row = c.execute("SELECT name, retired_ns FROM agents WHERE id=?",
                    (src["id"],)).fetchone()
    assert row is not None, "the merged-away identity must survive as a record"
    assert row["name"] == "architect" and row["retired_ns"]


def test_the_ghost_membership_goes_and_room_reach_is_kept():
    c, path, admin, room, loser, keep = _two_bodies()
    m = _tool()
    src, dst = m.live_agent(c, "architect"), m.live_agent(c, "reveille-architect")
    other = store.create_room(c, admin["id"], "OnlyTheLoser")
    now = 1
    # Both in one room (the ghost case), the loser alone in another (the reach
    # case). members is keyed (room_id, name).
    for name, aid in ((src["name"], src["id"]), (dst["name"], dst["id"])):
        c.execute("INSERT INTO members(room_id, name, agent_id, joined_ns, seen_ns)"
                  " VALUES(?,?,?,?,?)", (room["id"], name, aid, now, now))
    c.execute("INSERT INTO members(room_id, name, agent_id, joined_ns, seen_ns)"
              " VALUES(?,?,?,?,?)", (other["id"], src["name"], src["id"], now, now))

    c.execute("BEGIN IMMEDIATE")
    moved = m.merge(c, src, dst)
    c.execute("COMMIT")

    assert moved["members.dropped(ghost)"] == 1
    assert moved["members.relabelled"] == 1
    assert not c.execute("SELECT 1 FROM members WHERE name='architect'").fetchall()
    rooms = {r["room_id"] for r in c.execute(
        "SELECT room_id FROM members WHERE name=?", (dst["name"],))}
    assert rooms == {room["id"], other["id"]}, "no room reach may be lost"


def test_an_ambiguous_name_refuses_rather_than_guessing():
    """A resolution that guesses hands one agent's history to another (msg 9000)."""
    c, path, admin, room, loser, keep = _two_bodies()
    m = _tool()
    with pytest.raises(SystemExit):
        m.live_agent(c, "nobody-by-that-name")
