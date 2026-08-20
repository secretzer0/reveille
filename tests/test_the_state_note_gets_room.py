"""THE STATE NOTE GETS ROOM (operator 12743/12746/12754, rulings 12744/12747/
12750/12753/12757): kind=state grows to 8192 hard / 4096 soft / 2048 target,
and the write over the soft line SUCCEEDS and carries a procedural nudge --
a length comparison and a constant string, never a model, never a refusal.
A cap that refuses at the worst moment is a data-loss mechanism wearing a
quality-control costume: the note is written inside a swap window seconds
wide, on a credential that is already dying.

THE SCHEMA CARRIES THE SANITY BOUND, THE CODE CARRIES THE POLICY (12757,
correcting 12753 by the operator's 12754): the one rebuild moves the CHECK to
length(fact) <= 128000 -- ~15x the largest policy anyone proposed, so policy
never touches the schema again and every future cap tune is a constant
change, never another rebuild. Per-kind policy in named constants: state
8192/4096/2048; doctrine/contract/decision keep 1000, because they compete
for brief()'s shared budget and the distillation a refusal forces is the
feature there.

Proven RED at 96bcbc6: a 5000-char state fact raises "fact is over 1000
chars", the constants do not exist, and the schema CHECK still reads 1000.
"""
import os
import sqlite3
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
    return c, path


def bound_agent(c):
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    tok = store.create_token(c, u["id"], "body", agent_name="wanderer",
                             create=True, rooms=[room["id"]])
    return u, room, tok


def add_state(c, tok, fact):
    return store.memory_add(
        c, author="wanderer", token_id=tok["id"], agent_id=tok["agent_id"],
        agent_bound=True, tier="state", is_admin=False, rooms={},
        owned_rooms=[], kind="state", fact=fact)


def test_a_state_note_gets_its_room_and_the_others_do_not():
    """The whole point: 5000 chars of handover stores LIVE. And the kinds that
    compete for brief()'s budget keep their tight cap -- the message still
    names the limit that refused it, per kind."""
    c, _ = db()
    u, room, tok = bound_agent(c)
    out = add_state(c, tok, "handover: " + "x" * 4990)
    assert out["status"] == "live"
    row = c.execute("SELECT length(fact) AS n FROM memories WHERE uid=?",
                    (out["id"],)).fetchone()
    assert row["n"] == 5000, "the whole note landed, untruncated"
    with pytest.raises(store.BusError, match="8192"):
        add_state(c, tok, "y" * 8193)
    with pytest.raises(store.BusError, match="1000"):
        store.memory_add(
            c, author="wanderer", token_id=tok["id"], agent_id=tok["agent_id"],
            agent_bound=True, tier="write", is_admin=True,
            rooms={room["id"]: "Hive"}, owned_rooms=[room["id"]],
            kind="contract", scope=room["id"], fact="z" * 1001)


def test_over_the_soft_line_the_write_succeeds_and_the_nudge_rides_the_result():
    """STORE FIRST, THEN NUDGE (12747). The nudge names the target, what is
    NOT compressible, the supersedes mechanism, and that it may be ignored
    mid-handover -- and under the soft line there is silence, no nagging."""
    c, _ = db()
    u, room, tok = bound_agent(c)
    big = "handover: " + "x" * 4990
    out = add_state(c, tok, big)
    note = out.get("note", "")
    assert str(len(big)) in note, "says what it stored and how big"
    assert str(store.STATE_FACT_TARGET) in note, "names the target by reference"
    assert "supersedes" in note, "offers the mechanism"
    assert "ignore" in note, "and permission to ignore it mid-handover"
    assert c.execute("SELECT count(*) FROM memories WHERE uid=?",
                     (out["id"],)).fetchone()[0] == 1, "stored BEFORE nudged"
    quiet = add_state(c, tok, "handover: " + "x" * 1000)
    assert "note" not in quiet, "under the soft line: silence"


def test_the_policy_is_constants_and_the_schema_is_the_sanity_bound():
    """Pin the ruled numbers by name (the fleet's own gate custom), and pin
    the split: policy in code, 128000 sanity bound in the schema. A policy
    number reappearing in the schema is the brittleness 12754 called out."""
    assert store.STATE_FACT_MAX == 8192
    assert store.STATE_FACT_SOFT == 4096
    assert store.STATE_FACT_TARGET == 2048
    assert store.MEMORY_FACT_MAX == 1000
    assert "128000" in store._MEMORIES_SCHEMA
    assert "1000)" not in store._MEMORIES_SCHEMA.split("fact")[1].split(",")[0], (
        "the fact CHECK no longer carries a policy number")


def test_the_rebuild_carries_every_row_and_the_ceiling_actually_moved():
    """The migration is the riskiest shape we have (copy-drop-rename on the
    memories table), so the gate is the failure mode: a PRE-EXISTING row must
    survive byte-identical, and the CHECK must actually have moved -- proven
    at the SQL layer, beneath the code policy, where the old 1000 lived."""
    c, path = db()
    u, room, tok = bound_agent(c)
    kept = add_state(c, tok, "the note that must survive the rebuild")
    before = tuple(c.execute("SELECT * FROM memories WHERE uid=?",
                             (kept["id"],)).fetchone())
    c.execute("PRAGMA user_version=40")
    assert store.migrate(c, path) == store.SCHEMA_VERSION
    after = tuple(c.execute("SELECT * FROM memories WHERE uid=?",
                            (kept["id"],)).fetchone())
    assert before == after, "copy-and-rename lost or changed a row"
    got = store.recall(c, rooms={room["id"]: "Hive"}, token_id=tok["id"],
                       agent_id=tok["agent_id"], caller="wanderer",
                       query="survive")
    assert any(m["id"] == kept["id"] for m in got["memories"]), (
        "the FTS index was rebuilt with the rows in it")
    # The ceiling, at the layer the old one lived: raw SQL, no code policy.
    now = time.time_ns()
    c.execute("INSERT INTO memories(uid, kind, scope, fact, author, created_ns)"
              " VALUES ('raw-ok', 'state', 'agent:x', ?, 'w', ?)",
              ("x" * 10000, now))
    with pytest.raises(sqlite3.IntegrityError):
        c.execute("INSERT INTO memories(uid, kind, scope, fact, author, "
                  "created_ns) VALUES ('raw-no', 'state', 'agent:x', ?, 'w', ?)",
                  ("x" * 128001, now))
