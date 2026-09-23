"""A conflict verdict is judged once, not once per agent.

Every agent in a room folds over the same rows, so every agent's conflict pass
offered the writer the same pairs. Measured 2026-09-21: roc-api-dev and
shared-dev shared 95 of 95 candidate pairs; roc-api-dev's pass took 03:16:02
to 03:18:38, and roughly seventeen OverSiteAI bodies were each going to pay
that again for identical answers -- on the writer the voice path shares.

The key is the pair of FULL uids plus the JUDGE. A uid never changes (an edit
writes a new row), so a verdict cannot go stale on content; a changed frame or
model is a different question, and misses.
"""
import pytest

from reveille import daemon, store

from test_store import _mem_kw, fixture


def test_a_fresh_database_has_the_table():
    c, *_ = fixture()
    # GATE THE PROPERTY, NOT THE NUMBER. Pinning the literal made every LATER
    # migration fail this file, which says nothing about conflict verdicts.
    assert store._version(c) == store.SCHEMA_VERSION
    assert store._table_exists(c, "conflict_verdicts")


def test_a_v46_database_upgrades_in_place(tmp_path):
    db = str(tmp_path / "old.db")
    c = store.connect(db)
    store.migrate(c, db)
    c.execute("DROP TABLE conflict_verdicts")
    c.execute("PRAGMA user_version=46")
    assert store.migrate(c, db) == store.SCHEMA_VERSION
    assert store._table_exists(c, "conflict_verdicts")
    assert store.migrate(c, db) == store.SCHEMA_VERSION   # idempotent


def test_the_pair_is_the_key_in_either_order():
    c, *_ = fixture()
    assert store.conflict_verdict_put(c, "b" * 32, "a" * 32, "j1", "CONFLICT", "x vs y")
    assert store.conflict_verdict_get(c, "a" * 32, "b" * 32, "j1") == ("CONFLICT", "x vs y")
    assert store.conflict_verdict_get(c, "b" * 32, "a" * 32, "j1") == ("CONFLICT", "x vs y")


def test_a_different_judge_misses():
    """A changed frame or model is a different question."""
    c, *_ = fixture()
    store.conflict_verdict_put(c, "a" * 32, "b" * 32, "j1", "AGREE")
    assert store.conflict_verdict_get(c, "a" * 32, "b" * 32, "j2") is None


def test_an_unparsed_judgement_is_never_stored():
    """Not evidence of anything -- caching it would make the absence permanent."""
    c, *_ = fixture()
    assert store.conflict_verdict_put(c, "a" * 32, "b" * 32, "j1", "UNPARSED") is False
    assert store.conflict_verdict_get(c, "a" * 32, "b" * 32, "j1") is None


def test_the_judge_id_moves_with_the_frame_and_the_model(monkeypatch):
    monkeypatch.setattr(daemon, "_script_model", "writer")
    a = daemon.conflict_judge_id()
    monkeypatch.setattr(daemon, "_script_model", "another-writer")
    assert daemon.conflict_judge_id() != a
    monkeypatch.setattr(daemon, "_script_model", "writer")
    monkeypatch.setattr(daemon, "_CONFLICT_FRAME", daemon._CONFLICT_FRAME + " changed")
    assert daemon.conflict_judge_id() != a


def _room_with_pairs(n=4):
    c, admin, room, tok = fixture()
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    # UNRELATED ROWS AROUND THEM, as a real room has. In a corpus of only the
    # near-identical rows, every shared word occurs in every row, so its IDF is
    # zero and the rows score nothing against each other.
    for i, topic in enumerate(("turbine blade pitch", "invoice rounding mode",
                               "camera exposure gain", "tank level telemetry",
                               "oauth redirect whitelist", "tts voice bank")):
        store.memory_add(c, **kw(kind="decision", fact=f"{topic} ruling number {i}"))
    for i in range(n):
        store.memory_add(c, **kw(kind="decision",
                                 fact=f"the spool lock rule for the wake daemon, variant {i}"))
    rows = c.execute("SELECT * FROM memories WHERE status='live' AND kind='decision'").fetchall()
    return c, rows


def _judge(monkeypatch, answers):
    calls = []

    def fake(url, model, token, messages, timeout, max_tokens=0):
        calls.append(1)
        yield answers(len(calls))
    monkeypatch.setattr(daemon, "_llm_stream", fake)
    monkeypatch.setattr(daemon, "_digest_yield", lambda *a: None)
    monkeypatch.setattr(daemon, "_script_model", "writer")
    return calls


class P:
    name = "roc-api-dev"


def test_the_second_agent_in_the_room_asks_the_writer_nothing(monkeypatch):
    c, rows = _room_with_pairs()
    assert store.digest_conflict_pairs(c, rows), "fixture made no candidate pairs"
    calls = _judge(monkeypatch, lambda n: "CONFLICT\nfield 52 twice" if n == 1 else "AGREE\nsame")
    first = daemon._digest_conflicts(c, P(), rows)
    asked = len(calls)
    assert asked > 0
    second = daemon._digest_conflicts(c, P(), rows)
    assert len(calls) == asked, f"the second pass asked the writer {len(calls) - asked} more times"
    assert second == first, "a cached CONFLICT must still reach the note"


def test_an_unparsed_reply_is_asked_again_next_time(monkeypatch):
    c, rows = _room_with_pairs(2)
    calls = _judge(monkeypatch, lambda n: "I am not sure")
    daemon._digest_conflicts(c, P(), rows)
    once = len(calls)
    daemon._digest_conflicts(c, P(), rows)
    assert len(calls) == 2 * once, "an unparsed reply was cached as if it were an answer"


def test_a_writer_that_goes_away_mid_pass_keeps_what_it_answered(monkeypatch):
    """Verdicts land one at a time, so a pass cut short by the writer is a
    pass the next fold resumes rather than repeats."""
    c, rows = _room_with_pairs(5)
    pairs = store.digest_conflict_pairs(c, rows)
    if len(pairs) < 2:
        pytest.skip("fixture needs two pairs")

    def fake(url, model, token, messages, timeout, max_tokens=0):
        fake.n = getattr(fake, "n", 0) + 1
        if fake.n > 1:
            raise OSError("writer gone")
        yield "AGREE\nsame"
    monkeypatch.setattr(daemon, "_llm_stream", fake)
    monkeypatch.setattr(daemon, "_digest_yield", lambda *a: None)
    monkeypatch.setattr(daemon, "_script_model", "writer")
    daemon._digest_conflicts(c, P(), rows)
    stored = c.execute("SELECT count(*) FROM conflict_verdicts").fetchone()[0]
    assert stored == 1
