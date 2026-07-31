"""The identity backfill: what it fills, and what it refuses to guess.

The refusal is the feature. Every column this step writes is a historical NAME
resolved against the agents table, and a name with no row cannot be resolved --
so the two ways past it, inventing an owner and leaving the id permanently NULL,
are both forbidden. What is gated here is that it refuses BEFORE writing, that
it attributes correctly when it can, and that the state-note rescope moves a
note from a token to an identity, which is the migration's one user-visible
payoff.
"""
import pytest

from reveille import store


def at_v17(tmp_path, name="b.db"):
    """A current database stamped back to 17: the shape a deployment has the
    moment before this step runs."""
    path = str(tmp_path / name)
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) "
                 "VALUES('u1','tmelhiser','x','admin',1)")
    conn.execute("INSERT INTO rooms(id, name, owner_id, created_ns) "
                 "VALUES('r1','room','u1',1)")
    return conn, path


def msg(conn, sender, recipient="*", ts=1):
    return conn.execute(
        "INSERT INTO messages(sender, recipient, subject, body, room, ts_ns) "
        "VALUES(?,?,'s','b','r1',?)", (sender, recipient, ts)).lastrowid


def back_to_17(conn):
    conn.execute("PRAGMA user_version=17")


def test_it_refuses_a_name_it_cannot_attribute_and_changes_nothing(tmp_path):
    conn, path = at_v17(tmp_path)
    mid = msg(conn, "reveille-devops")
    back_to_17(conn)
    with pytest.raises(store.BusError, match="REFUSES"):
        store.migrate(conn, path)
    # Not half-migrated: the version did not move and no id was written. A
    # database that cannot be migrated must be left exactly as it was.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 17
    assert conn.execute("SELECT sender_agent_id FROM messages WHERE id=?",
                        (mid,)).fetchone()[0] is None


def test_the_refusal_names_every_name_and_what_it_holds(tmp_path):
    """A refusal that says "some names" makes the operator go and find them.
    The list IS the instruction -- it is what they paste into the seeder."""
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops")
    msg(conn, "reveille-architect")
    back_to_17(conn)
    with pytest.raises(store.BusError) as e:
        store.migrate(conn, path)
    text = str(e.value)
    assert "reveille-devops" in text and "reveille-architect" in text
    assert "messages" in text
    assert "seed_agent_identities.py" in text, "the refusal must name the way out"


def test_it_attributes_history_to_the_identity_once_the_names_are_claimed(tmp_path):
    conn, path = at_v17(tmp_path)
    mid = msg(conn, "reveille-devops")
    store.claim_unresolved_names(conn, "u1")
    aid = conn.execute("SELECT id FROM agents WHERE name='reveille-devops'").fetchone()[0]
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT sender_agent_id FROM messages WHERE id=?",
                        (mid,)).fetchone()[0] == aid
    # THE NAME STAYS. Routing, `to=` and every human reader use it; dropping it
    # would break every address on the wire (DES-007 2.3).
    assert conn.execute("SELECT sender FROM messages WHERE id=?",
                        (mid,)).fetchone()[0] == "reveille-devops"


def test_a_state_note_moves_from_the_token_to_the_identity(tmp_path):
    """DES-007 4.2, and the payoff that makes "recreate resumes its old state"
    true rather than aspirational: a note scoped agent:<token_id> is orphaned the
    moment an agent is recreated, because recreating mints a new token."""
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops")
    conn.execute("INSERT INTO tokens(id, owner_id, secret_hash, label, agent_name, "
                 "mem_tier, created_ns) VALUES('t1','u1','h','l','reveille-devops',"
                 "'state',1)")
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('m1','state','agent:t1','what I was doing',"
                 "'reveille-devops','live',1)")
    store.claim_unresolved_names(conn, "u1")
    aid = conn.execute("SELECT id FROM agents WHERE name='reveille-devops'").fetchone()[0]
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT scope FROM memories WHERE uid='m1'").fetchone()[0] \
        == f"agent:{aid}"


def test_a_note_whose_token_is_gone_keeps_its_scope(tmp_path):
    """The other half, and it is a refusal too: with no token row there is
    nothing to resolve the note through, and guessing would attach one agent's
    memory to another. An orphaned note stays orphaned rather than becoming
    somebody else's."""
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops")
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('m1','state','agent:gone','x','reveille-devops',"
                 "'live',1)")
    store.claim_unresolved_names(conn, "u1")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT scope FROM memories WHERE uid='m1'").fetchone()[0] \
        == "agent:gone"


def test_reads_and_members_carry_the_identity_too(tmp_path):
    """4.3 and 4.6. reads is the sharp one: a declined resurrect under a reused
    name would otherwise mint an agent that appears to have already read mail it
    has never seen, and inbox() would skip it silently."""
    conn, path = at_v17(tmp_path)
    mid = msg(conn, "reveille-devops")
    conn.execute("INSERT INTO reads(message_id, agent, read_ns) VALUES(?,?,1)",
                 (mid, "reveille-devops"))
    conn.execute("INSERT INTO members(room_id, name, tag, joined_ns, seen_ns) "
                 "VALUES('r1','reveille-devops','reveille-devops',1,1)")
    store.claim_unresolved_names(conn, "u1")
    aid = conn.execute("SELECT id FROM agents WHERE name='reveille-devops'").fetchone()[0]
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT agent_id FROM reads").fetchone()[0] == aid
    assert conn.execute("SELECT agent_id FROM members").fetchone()[0] == aid


def test_a_broadcast_recipient_is_not_an_identity(tmp_path):
    """`*` is an address, not an agent. It must not appear in the refusal list
    and it must not acquire an id -- a name that cannot be an identity is not a
    name that failed to resolve."""
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops", recipient="*")
    store.claim_unresolved_names(conn, "u1")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT count(*) FROM agents WHERE name='*'").fetchone()[0] == 0


def test_the_step_is_registered_in_the_chain(tmp_path):
    assert store._UPGRADES.get(17) == "_upgrade_v17"
    assert callable(getattr(store, "_upgrade_v17"))
