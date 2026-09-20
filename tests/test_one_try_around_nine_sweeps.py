"""Nine sweeps shared one try, and the fifth raised on every pass.

memories.supersedes_id is a real foreign key, and distill() chains each state
note to the one before it -- so the ORDINARY shape is an expired row pinned by
the live note that superseded it. sweep_expired_state deletes the whole expired
batch in one transaction, so one pinned row swept nothing:

    sqlite3.IntegrityError: FOREIGN KEY constraint failed
      File "/app/src/reveille/store.py", line 7054, in sweep_expired_state
        conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", ids)

Field snapshot: 98 expired state rows, 63 pinned. Zero swept, every hour, for
as long as anyone has chained a state note. The old test seeded ONE state row
with no successor, so it never saw it.

THE REFUSAL IS NOT THE EXPENSIVE HALF and is deliberately left standing (the
delete is a hard delete of the last copy; the operator keeps expired state for
training, and nothing reads it either way). The expensive half is that the
failure ran FIFTH inside a single try, so it also skipped tombstones, knocks
and recalls -- and logged `sweep failed` without naming which one. Same
snapshot: 3 expired return tickets still sitting in `recalls`.
"""
import logging
import sqlite3

import pytest

from reveille import daemon, store

from test_store import _mem_kw, fixture


def _expired_chain(c, admin, room, tok):
    """A superseded-and-expired ancestor, and the LIVE note that replaced it."""
    kw = lambda **o: _mem_kw(c, admin, room, tok, **o)      # noqa: E731
    old = store.memory_add(c, **kw(kind="state", tier="state", fact="older note"))
    new = store.memory_add(c, **kw(kind="state", tier="state", fact="newer note"))
    old_id, new_id = (c.execute("SELECT id FROM memories WHERE uid=?", (u["id"],)).fetchone()[0]
                      for u in (old, new))
    c.execute("UPDATE memories SET supersedes_id=?, expires_ns=NULL WHERE id=?",
              (old_id, new_id))
    c.execute("UPDATE memories SET expires_ns=1, status='superseded' WHERE id=?", (old_id,))
    return old_id, new_id


def test_the_delete_still_refuses_and_that_is_the_point():
    """The pin is REAL and is left in place: this is a hard delete and the bytes
    are the last copy. Nothing reads them either way -- expiry already took the
    row out of _readable_live and out of recall 30 days ago -- so the sweep
    failing costs nothing a reader can see, while the delete succeeding would
    destroy the row for good. The gate is here so the day somebody unblocks it,
    they do it deliberately (retention policy) and not as a tidy-up."""
    c, admin, room, tok = fixture()
    old_id, _ = _expired_chain(c, admin, room, tok)
    with pytest.raises(sqlite3.IntegrityError):
        store.sweep_expired_state(c)
    assert c.execute("SELECT count(*) FROM memories WHERE id=?", (old_id,)).fetchone()[0] == 1


def test_an_expired_row_is_already_dark_to_every_reader():
    """The claim the retention decision rests on: deletion is not the loss
    event, the 30-day expiry is, and it has already happened."""
    c, admin, room, tok = fixture()
    old_id, _ = _expired_chain(c, admin, room, tok)
    got = store.recall(c, rooms={room["id"]: "R"}, token_id=tok["id"], kind="state")
    assert all("older note" not in str(r) for r in got.get("items", [])), "recall still sees it"
    scope = c.execute("SELECT scope FROM memories WHERE id=?", (old_id,)).fetchone()[0]
    live = store._readable_live(c, [scope, room["id"]])
    assert not any(r["id"] == old_id for r in live), "the fold still reads it"


def test_a_raising_sweep_does_not_starve_the_sweeps_behind_it(monkeypatch, caplog):
    monkeypatch.setattr(daemon, "_conn", object())
    boom = lambda _conn: (_ for _ in ()).throw(RuntimeError("nope"))   # noqa: E731
    with caplog.at_level(logging.ERROR):
        assert daemon._sweep_one("expired-state", boom) == 0
    assert "sweep expired-state failed" in caplog.text        # the log NAMES it
    assert daemon._sweep_one("knocks", lambda _conn: 7) == 7  # the next one still runs


def test_the_sweeper_runs_every_sweep_even_when_one_raises(monkeypatch):
    """The order and the isolation together: five sweeps in, one throws, and the
    four behind it are still called."""
    called = []

    def stub(name, fail=False):
        def fn(_conn):
            called.append(name)
            if fail:
                raise RuntimeError(name)
            return 0
        return fn

    monkeypatch.setattr(daemon, "_conn", object())
    monkeypatch.setattr(daemon, "SWEEP_SECS", 0)
    for name in ("sweep_oidc_state", "sweep_retention", "sweep_sessions", "reap_stale",
                 "sweep_tombstones", "sweep_knocks", "sweep_recalls"):
        monkeypatch.setattr(store, name, stub(name))
    monkeypatch.setattr(store, "sweep_expired_state", stub("sweep_expired_state", fail=True))

    async def one_pass():
        with pytest.raises(StopAsyncIteration):
            await daemon._sweeper()

    calls = {"n": 0}

    async def sleep(_secs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise StopAsyncIteration
    monkeypatch.setattr(daemon.asyncio, "sleep", sleep)
    import asyncio
    asyncio.run(one_pass())
    assert called[-3:] == ["sweep_tombstones", "sweep_knocks", "sweep_recalls"]
    assert "sweep_expired_state" in called
