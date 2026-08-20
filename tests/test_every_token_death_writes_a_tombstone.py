#!/usr/bin/env python3
"""Every token death writes a tombstone naming its reason (ruling 12944 R-A).

The table is the DEATH REGISTER, not the supersede register. Revoke used to
DELETE the token and leave nothing -- so on 2026-08-20 the broker DB could not
answer "was the architect's credential revoked, or did it never exist", the
two states were byte-identical, and the only surviving evidence was a
token_audit tier line found on the fourth query. A row with reason='revoked'
is what answers that question on the first.

Privilege stays separate from the record (the 12485 split): the handover
grace and the return ticket belong to 'superseded' alone, and knock refuses a
revoked hash BY NAME -- a knock will not bring a revoked credential back,
only the owner minting fresh can.

Expected values are constructed independently of the accessors under test
(hashlib for the key), per a-gate-that-reads-through-the-same-wrong-accessor.
"""
import hashlib
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import store  # noqa: E402


def db():
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    return c


def world(c):
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "Hive")
    tok = store.create_token(c, admin["id"], "body-1", agent_name="wanderer",
                             create=True, rooms=[room["id"]])
    return admin, room, tok


def test_revoke_leaves_a_row_naming_revoked():
    """The gate that was red on the unfixed head: revoke_token deleted the
    token and tombstone_for answered None -- revoked and never-existed were
    indistinguishable."""
    c = db()
    admin, room, tok = world(c)
    store.revoke_token(c, tok["id"], admin["id"])
    ts = store.tombstone_for(c, tok["secret"])
    assert ts is not None, "revoke left no tombstone -- the death went unrecorded"
    assert ts["reason"] == "revoked"
    assert ts["agent_name"] == "wanderer"
    row = c.execute("SELECT * FROM token_tombstones WHERE secret_hash=?",
                    (hashlib.sha256(tok["secret"].encode()).hexdigest(),)).fetchone()
    assert row is not None and row["reason"] == "revoked"


def test_knock_refuses_a_revoked_hash_by_name():
    """Dead-ness is the address, privilege is separate: the revoked sentence
    says what happened and that a knock will not bring it back -- never the
    generic bad-token, never the superseded invitation."""
    c = db()
    admin, room, tok = world(c)
    store.revoke_token(c, tok["id"], admin["id"])
    with pytest.raises(store.AuthError) as e:
        store.knock(c, tok["secret"])
    said = str(e.value)
    assert "revoked" in said
    assert "knock will not bring it back" in said
    assert "mint" in said


def test_a_superseded_death_outranks_the_revoke_that_executes_it():
    """supersede_bound_tokens writes the 'superseded' row then calls
    revoke_token on the same credential -- the truer reason must survive.
    This is the both-reasons identity: superseded_hash_for answers the
    superseded hash, never a revoked sibling's."""
    c = db()
    admin, room, tok = world(c)
    superseded = store.supersede_bound_tokens(c, admin["id"], tok["agent_id"])
    assert superseded == [tok["id"]]
    h = hashlib.sha256(tok["secret"].encode()).hexdigest()
    row = c.execute("SELECT reason FROM token_tombstones WHERE secret_hash=?",
                    (h,)).fetchone()
    assert row["reason"] == "superseded", (
        "revoke_token overwrote the supersede tombstone -- the grace and the "
        "return ticket both key on that reason")
    # a second, revoked credential for the same identity must not shadow it
    tok2 = store.create_token(c, admin["id"], "body-2", agent_name="wanderer",
                              rooms=[room["id"]])
    store.revoke_token(c, tok2["id"], admin["id"])
    assert store.superseded_hash_for(c, tok["agent_id"]) == h


def test_revoked_earns_no_handover_grace():
    """The grace is a licence earned by having HELD the identity through a
    supersede. A fresh revoked tombstone buys nothing."""
    c = db()
    admin, room, tok = world(c)
    store.revoke_token(c, tok["id"], admin["id"])
    assert store.handover_grace(c, tok["secret"]) is None


def test_revoked_rows_ride_the_existing_sweep():
    """No new sweep, no new column: a revoked tombstone past TOMBSTONE_TTL_NS
    is deleted by the same sweep as every other."""
    c = db()
    admin, room, tok = world(c)
    store.revoke_token(c, tok["id"], admin["id"])
    future = time.time_ns() + store.TOMBSTONE_TTL_NS + 1
    assert store.sweep_tombstones(c, now=future) == 1


def test_migration_v41_widens_the_reason_check():
    """A v41 table refuses 'revoked' at the CHECK; v42 accepts it and carries
    every existing row across unchanged."""
    path = os.path.join(tempfile.mkdtemp(), "up.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin, room, tok = world(c)
    # regress the table to the v41 shape, then a pre-existing superseded row
    c.execute("ALTER TABLE token_tombstones RENAME TO tt_new")
    c.execute("""CREATE TABLE token_tombstones (
        secret_hash     TEXT PRIMARY KEY,
        agent_id        TEXT NOT NULL,
        reason          TEXT NOT NULL DEFAULT 'superseded'
                        CHECK (reason IN ('superseded','expired-unclaimed')),
        died_ns         INTEGER NOT NULL,
        last_refusal_ns INTEGER)""")
    c.execute("DROP TABLE tt_new")
    c.execute("INSERT INTO token_tombstones(secret_hash, agent_id, reason, died_ns) "
              "VALUES('aaaa', ?, 'superseded', 1)", (tok["agent_id"],))
    import sqlite3
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO token_tombstones(secret_hash, agent_id, reason, "
                  "died_ns) VALUES('bbbb', ?, 'revoked', 2)", (tok["agent_id"],))
    c.execute("PRAGMA user_version=41")
    assert store.migrate(c, path) == store.SCHEMA_VERSION
    old = c.execute("SELECT * FROM token_tombstones WHERE secret_hash='aaaa'").fetchone()
    assert old is not None and old["reason"] == "superseded" and old["died_ns"] == 1
    c.execute("INSERT INTO token_tombstones(secret_hash, agent_id, reason, died_ns) "
              "VALUES('bbbb', ?, 'revoked', 2)", (tok["agent_id"],))
