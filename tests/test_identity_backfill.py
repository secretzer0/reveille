"""The identity backfill: what it fills, and what it refuses to guess.

The refusal is the feature. Every column this step writes is a historical NAME
resolved against the agents table, and a name with no row cannot be resolved --
so the two ways past it, inventing an owner and leaving the id permanently NULL,
are both forbidden. What is gated here is that it refuses BEFORE writing, that
it attributes correctly when it can, and that the state-note rescope moves a
note from a token to an identity, which is the migration's one user-visible
payoff.
"""
import pathlib

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


def test_a_note_whose_token_is_gone_still_moves_to_its_author(tmp_path):
    """THIS TEST USED TO ASSERT THE DEFECT.

    It said a note whose token had vanished should keep its old scope, and it
    passed while 47 of the operator's 50 state notes were being stranded exactly
    that way -- tokens are SUPERSEDED on every re-mint, so "the token is gone" is
    the normal state of any note older than one re-provision, not an edge case.
    Once the readers moved to the identity, those rows were reachable by nobody.

    The author's NAME survives every token rotation, and pre-migration history is
    one identity per name by definition, so that is the durable link.
    """
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops")
    store.claim_unresolved_names(conn, "u1")
    aid = conn.execute("SELECT id FROM agents WHERE name='reveille-devops'").fetchone()[0]
    # no tokens row at all: the credential that wrote this is long superseded
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('m1','state','agent:dead-token','x',"
                 "'reveille-devops','live',1)")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT scope FROM memories WHERE uid='m1'").fetchone()[0] \
        == f"agent:{aid}"


def test_a_note_whose_author_is_not_an_agent_stays_put(tmp_path):
    """The refusal half, unchanged: with no agents row for the author there is
    nothing to resolve to, and inventing one is what the backfill refuses at
    migration time. It stays where it is rather than being guessed at."""
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops")
    store.claim_unresolved_names(conn, "u1")
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('m2','state','agent:dead','x','someone-else',"
                 "'live',1)")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT scope FROM memories WHERE uid='m2'").fetchone()[0] \
        == "agent:dead"


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


def test_a_human_sender_is_not_an_identity(tmp_path):
    """A person posting from the web is a user, not an agent: the refusal list
    excludes them by name, so the in-transaction recount must too. Field
    incident 2026-07-31: preflight passed (humans excluded), then the recount
    counted 74 human-sent messages and the broker restart-looped on a database
    the preflight had just blessed. Their id stays NULL forever -- there is no
    agents row to point at and inventing one is forbidden."""
    conn, path = at_v17(tmp_path)
    mid = msg(conn, "tmelhiser")
    msg(conn, "reveille-devops")
    store.claim_unresolved_names(conn, "u1")
    assert conn.execute("SELECT count(*) FROM agents WHERE name='tmelhiser'"
                        ).fetchone()[0] == 0
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT sender_agent_id FROM messages WHERE id=?",
                        (mid,)).fetchone()[0] is None


def test_the_step_is_registered_in_the_chain(tmp_path):
    assert store._UPGRADES.get(17) == "_upgrade_v17"
    assert callable(getattr(store, "_upgrade_v17"))


def test_a_state_note_is_visible_to_the_readers_after_the_move(tmp_path):
    """THE INCIDENT, both halves. v17 moved state notes to agent:<agent_id> while
    memory_add, recall and brief all still computed agent:<token_id> -- so the
    rows sat on disk at a scope nothing asked for and the operator's agents could
    not see their own state (architect, msg 8971).

    Both directions are asserted: a note written under the OLD token scope and a
    note written under the NEW identity scope must BOTH be readable, because the
    fix that only handles one of them recreates the incident an hour younger.
    """
    conn, path = at_v17(tmp_path)
    msg(conn, "reveille-devops")
    conn.execute("INSERT INTO tokens(id, owner_id, secret_hash, label, agent_name, "
                 "mem_tier, created_ns) VALUES('t1','u1','h','l','reveille-devops',"
                 "'state',1)")
    store.claim_unresolved_names(conn, "u1")
    aid = conn.execute("SELECT id FROM agents WHERE name='reveille-devops'").fetchone()[0]

    # written BEFORE the move, at the token scope
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('old','state','agent:t1','before the move',"
                 "'reveille-devops','live',1)")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION

    # written INTO THE GAP: after v17 ran, by a writer that had not moved yet
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('gap','state','agent:t1','written in the gap',"
                 "'reveille-devops','live',2)")
    # the same step that closes the gap, run as the next boot would run it
    conn.execute("PRAGMA user_version=18")
    assert store.migrate(conn, path) == store.SCHEMA_VERSION

    scopes = {r["uid"]: r["scope"] for r in
              conn.execute("SELECT uid, scope FROM memories WHERE kind='state'")}
    assert scopes["old"] == f"agent:{aid}", scopes
    assert scopes["gap"] == f"agent:{aid}", "the gap note was left where nothing reads"

    # and the readers ask for that scope rather than the token's
    assert store.agent_scope(conn, "t1") == f"agent:{aid}"


def test_an_unbound_token_still_has_its_own_bucket(tmp_path):
    """The fallback is not legacy tolerance: an unbound token has no identity to
    key on, and state writes already refuse it. A reader with one gets the empty
    bucket it has always had rather than an exception."""
    conn, path = at_v17(tmp_path)
    conn.execute("INSERT INTO tokens(id, owner_id, secret_hash, label, mem_tier, "
                 "created_ns) VALUES('t9','u1','h','l','state',1)")
    assert store.agent_scope(conn, "t9") == "agent:t9"


def test_the_recount_and_the_refusal_cannot_disagree(tmp_path):
    """The field incident, as a property rather than as its instance.

    The recount used to ask "is anything unattributed" in fresh SQL while the
    refusal asked it through unresolved_agent_names. The two spellings disagreed
    about humans, so a database the preflight had just blessed made the broker
    restart-loop. Asserting the human case alone would only pin the instance;
    what is pinned here is that the recount CALLS the refusal, so any future
    exclusion added to one is automatically in the other.
    """
    src = pathlib.Path(store.__file__).read_text()
    body = src[src.index("def _upgrade_v17("):src.index("def _upgrade_v18(")]
    # anchored past the column backfills (now a loop, not four literals) to the
    # recount at the function's tail
    after = body[body.index("_rescope_state_notes(conn)"):]
    assert "unresolved_agent_names(conn)" in after, \
        "the recount must call the refusal, not re-spell it"
    assert "SELECT count(*) FROM messages WHERE sender_agent_id IS NULL" not in after, \
        "a second spelling of the rule is back -- that is the defect, not its instance"


def test_a_human_sender_never_blocks_the_migration(tmp_path):
    """The instance, kept because it is the one that cost a boot: a person
    posting from the web is a user, not an agent, and their messages keep a NULL
    id forever -- there is no agents row to point at."""
    conn, path = at_v17(tmp_path)
    mid = msg(conn, "tmelhiser")
    msg(conn, "reveille-devops")
    store.claim_unresolved_names(conn, "u1")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT sender_agent_id FROM messages WHERE id=?",
                        (mid,)).fetchone()[0] is None


def two_identities(conn, name="reveille-devops"):
    """The shape a declined resurrect produces: one retired identity, one live,
    same label. The fixture the prune gate already uses, rebuilt here because
    with one identity per name this entire class of defect cannot be seen."""
    conn.execute("INSERT INTO agents(id, owner_id, name, created_ns, retired_ns) "
                 "VALUES('old-id','u1',?,1,2)", (name,))
    conn.execute("INSERT INTO agents(id, owner_id, name, created_ns) "
                 "VALUES('new-id','u1',?,3)", (name,))


def test_an_ambiguous_author_is_left_put_and_counted(tmp_path, capsys):
    """Architect finding, msg 9000: the tie-break preferred the LIVE identity,
    so a retired agent's state notes moved to the live agent that later reused
    its name -- one agent's memory handed to another, silently, by a migration.
    Red on that ORDER BY, which happily moves both."""
    conn, path = at_v17(tmp_path)
    two_identities(conn)
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('m1','state','agent:dead-token',"
                 "'the retired agent remembers','reveille-devops','live',1)")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT scope FROM memories WHERE uid='m1'").fetchone()[0] \
        == "agent:dead-token", "an ambiguous author's note moved -- that is the guess"
    assert "more than one identity" in capsys.readouterr().out


def test_an_ambiguous_sender_stays_null_and_is_counted(tmp_path, capsys):
    """NULL is 'not yet attributed', which is recoverable. A wrong id is a false
    record, which is not."""
    conn, path = at_v17(tmp_path)
    two_identities(conn)
    mid = msg(conn, "reveille-devops")
    back_to_17(conn)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert conn.execute("SELECT sender_agent_id FROM messages WHERE id=?",
                        (mid,)).fetchone()[0] is None
    assert "more than one identity" in capsys.readouterr().out


def test_the_write_path_attributes_to_the_live_identity(tmp_path):
    """WRITE time is different from migration time, deliberately: a message
    being written now is being written by the live instance -- that is what
    live means. With no live row and several retired ones, None: unattributed
    is honest, misattributed is not."""
    conn, path = at_v17(tmp_path)
    two_identities(conn)
    assert store.agent_id_for(conn, "reveille-devops") == "new-id"
    conn.execute("UPDATE agents SET retired_ns=9 WHERE id='new-id'")
    assert store.agent_id_for(conn, "reveille-devops") is None
    assert store.agent_id_for(conn, "tmelhiser") is None
