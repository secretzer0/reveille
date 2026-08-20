"""THE STATE NOTE GETS ROOM (operator 12743/12746/12754, rulings 12744/12747/
12750/12753/12757): kind=state grows to 8192 hard / 4096 soft / 2048 target,
and the write over the soft line SUCCEEDS and carries a procedural nudge --
a length comparison and a constant string, never a model, never a refusal.
A cap that refuses at the worst moment is a data-loss mechanism wearing a
quality-control costume: the note is written inside a swap window seconds
wide, on a credential that is already dying.

NO SCHEMA CHECK AT ALL (12759, sharpening 12757 by the operator's 12756/
12758): SQLite gives nothing for a declared bound -- TEXT is variable-length,
the old 1000 was a design choice wearing a schema constraint's clothes, and
removing a CHECK costs the same rebuild as changing one. So the one rebuild
strips it, and EVERY number lives in code as a named constant: FACT_MAX
128000 as the disaster backstop, state 8192/4096/2048 as policy,
doctrine/contract/decision keep 1000 because they compete for brief()'s
shared budget and the distillation a refusal forces is the feature there.

Proven RED at 96bcbc6: a 5000-char state fact raises "fact is over 1000
chars", the constants do not exist, and the schema still carries the CHECK.
The no-CHECK sharpening proven RED again at a945963, where the interim
128000 CHECK sat in the schema.
"""
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


def test_every_number_lives_in_code_and_none_in_the_schema():
    """Pin the ruled numbers by name (the fleet's own gate custom), and pin
    the layer split ruled in 12759: fact is bare TEXT -- NO length CHECK of
    any size -- and the disaster backstop is FACT_MAX in code, where it can
    move without a table rebuild. A number reappearing in the schema is the
    brittleness 12754 called out, at any value."""
    assert store.FACT_MAX == 128_000
    assert store.STATE_FACT_MAX == 8192
    assert store.STATE_FACT_SOFT == 4096
    assert store.STATE_FACT_TARGET == 2048
    assert store.MEMORY_FACT_MAX == 1000
    assert "length(fact)" not in store._MEMORIES_SCHEMA, (
        "no schema bound on fact, of any size")


def test_the_backstop_is_real_not_decorative(monkeypatch):
    """FACT_MAX guards beneath the per-kind policy: even if a future tune
    raises a kind's cap past it, the disaster bound still refuses. Proven by
    raising the state policy above FACT_MAX and watching FACT_MAX hold."""
    c, _ = db()
    u, room, tok = bound_agent(c)
    monkeypatch.setattr(store, "STATE_FACT_MAX", store.FACT_MAX * 2)
    with pytest.raises(store.BusError, match="128000"):
        add_state(c, tok, "x" * (store.FACT_MAX + 1))


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
    # The old ceiling is GONE at the SQL layer: a raw insert past every code
    # bound lands, because the schema no longer carries any number (12759) --
    # the backstop lives in memory_add, gated separately above.
    now = time.time_ns()
    c.execute("INSERT INTO memories(uid, kind, scope, fact, author, created_ns)"
              " VALUES ('raw-ok', 'state', 'agent:x', ?, 'w', ?)",
              ("x" * (store.FACT_MAX + 1), now))
