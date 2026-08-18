"""The chain, measured as a chain: which steps RUN from each start version.

Every existing migration test asserts an EFFECT and then that the version reads
SCHEMA_VERSION. Both can be true of a chain that skipped a step -- that is
exactly what happened: the v9 through v13 arms never called _upgrade_v15, and
_upgrade_v13's full _SCHEMA replay laid the agents table anyway, so every
assertion any test could make came out green. Additive drift heals; the version
stamp said "current" regardless; the gap was invisible from both ends.

So this file asserts the thing those tests structurally cannot: the SET OF STEPS
that executed. It spies on the real functions rather than reimplementing the
chain, because a test that computes the expected chain from its own copy of the
table would agree with a wrong table.
"""
import pathlib

import pytest

from reveille import store


@pytest.fixture
def spy(monkeypatch):
    """Record which _upgrade_vN functions run, wrapping the real ones."""
    ran = []
    for v, name in list(store._UPGRADES.items()):
        real = getattr(store, name)

        def wrap(conn, db_path, _v=v, _fn=real):
            ran.append(_v)
            return _fn(conn, db_path)
        monkeypatch.setattr(store, name, wrap)
    return ran


def db_at(tmp_path, version):
    """A database carrying the CURRENT schema but stamped at an older version --
    the shape a partially-migrated live database has, and the shape every
    existing migration test already uses."""
    path = str(tmp_path / f"v{version}.db")
    conn = store.connect(path)
    store.migrate(conn, path)
    conn.execute(f"PRAGMA user_version={version}")
    return conn, path


def test_every_start_version_runs_every_step_after_it(tmp_path, spy):
    # The defect in one assertion: from v9 the old chain stopped at _upgrade_v14.
    # _upgrade_v15 is the agents table -- the first thing DES-007 needs and the
    # step no v9-v13 database ever ran.
    conn, path = db_at(tmp_path, 9)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert spy == [9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27], spy
    assert conn.execute("PRAGMA user_version").fetchone()[0] == store.SCHEMA_VERSION


# Start versions 7 and up only, and the reason is about the FIXTURE rather than
# the chain: db_at() stamps an OLD version onto the CURRENT schema, and the steps
# below 7 mutate columns the current schema no longer has (v2 drops
# tokens.revoked_ns, which is not there to drop). Those steps own tests that build
# the real old shape; what this one measures is that no arm is short, and that is
# measurable from any start version whose fixture is honest.
@pytest.mark.parametrize("start", [7, 8, 9, 10, 11, 12, 13, 14, 15])
def test_the_chain_from_each_version_is_contiguous(tmp_path, start, spy):
    conn, path = db_at(tmp_path, start)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    # Contiguous from the start version, no gaps, nothing before it: a chain with
    # a hole in it is the failure this file exists for, whichever end the hole is.
    assert spy == [v for v in range(start, store.SCHEMA_VERSION) if v in store._UPGRADES], spy


def test_a_step_that_fails_leaves_the_version_where_it_completed(tmp_path, monkeypatch):
    """The other half of stamping your own target: a chain that dies mid-way must
    say where it got to. Under the old scheme every step stamped SCHEMA_VERSION,
    so a database that had run two steps of six still claimed to be current, and
    the next start ran nothing at all."""
    conn, path = db_at(tmp_path, 12)

    def boom(conn, db_path):
        raise store.BusError("step 13 refuses")

    monkeypatch.setattr(store, "_upgrade_v13", boom)
    with pytest.raises(store.BusError):
        store.migrate(conn, path)
    # 12 ran and stamped 13; 13 died. The database says 13, which is true.
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 13
    monkeypatch.undo()
    assert store.migrate(conn, path) == store.SCHEMA_VERSION   # resumes, does not skip


def test_a_step_that_does_not_advance_is_refused_not_looped(tmp_path, monkeypatch):
    conn, path = db_at(tmp_path, 14)
    monkeypatch.setattr(store, "_upgrade_v14", lambda conn, db_path: None)
    with pytest.raises(store.BusError, match="did not advance"):
        store.migrate(conn, path)


def test_a_fresh_database_lands_at_the_current_version(tmp_path):
    path = str(tmp_path / "fresh.db")
    conn = store.connect(path)
    assert store.migrate(conn, path) == store.SCHEMA_VERSION
    assert store.migrate(conn, path) == store.SCHEMA_VERSION      # idempotent
    assert pathlib.Path(path).exists()


def test_the_step_table_covers_every_version_that_needs_one():
    """Architect requirement, msg 8903. The loop treats a MISSING entry as a gap
    to step over, which is right for a version that never had work and wrong for
    one whose step was forgotten -- and from inside the loop those are the same
    thing. A SCHEMA_VERSION bump that forgets its table entry would otherwise
    stamp silently past a real migration, which is the short-arm defect wearing
    the new mechanism."""
    gaps = {v for v in range(0, store.SCHEMA_VERSION) if v not in store._UPGRADES}
    assert gaps == store._UPGRADE_GAPS, (
        f"versions {sorted(gaps - store._UPGRADE_GAPS)} have no step and are not "
        f"declared gaps; {sorted(store._UPGRADE_GAPS - gaps)} are declared gaps "
        f"but have steps. A gap must be a decision, written in _UPGRADE_GAPS.")


def test_every_name_in_the_step_table_resolves_to_something_callable():
    """The table holds STRINGS so that patching store._upgrade_vN reaches
    dispatch -- which is what keeps the interrupt gates able to fail. The cost is
    that ruff cannot see inside a string, so a rename or a typo becomes a
    KeyError on somebody's real database, mid-migration, on the one path with no
    undo but the snapshot. This is what turns it back into an import-time-shaped
    failure: a test."""
    for version, name in sorted(store._UPGRADES.items()):
        fn = getattr(store, name, None)
        assert callable(fn), f"_UPGRADES[{version}] = {name!r} resolves to {fn!r}"


def test_the_old_ladder_shape_is_gone(tmp_path):
    """The arms are what could be short. If they come back, this fails -- and a
    reader who reintroduces one gets told why rather than guessing."""
    src = pathlib.Path(store.__file__).read_text()
    body = src[src.index("def migrate("):src.index("def _upgrade_v2(")]
    assert "elif v ==" not in body
    assert "_UPGRADES" in body


def test_no_step_stamps_the_moving_target(tmp_path):
    """SCHEMA_VERSION inside a step is the defect itself: it makes every step
    claim the chain is finished. Only the fresh-schema path and _upgrade_v0 (which
    lays the whole current schema) may name it."""
    src = pathlib.Path(store.__file__).read_text()
    for name in ("_upgrade_v2", "_upgrade_v5", "_upgrade_v9", "_upgrade_v13",
                 "_upgrade_v14", "_upgrade_v15"):
        start = src.index(f"def {name}(")
        body = src[start:src.index("\ndef ", start)]
        assert "user_version={SCHEMA_VERSION}" not in body, name


def test_a_truncated_chain_still_carries_the_data_the_steps_add(tmp_path):
    """NOT a table-exists check, which is the trap: a full _SCHEMA replay makes
    every table appear whether or not the step that fills it ever ran. v9 REBUILDS
    memories_fts from the rows that exist, so an index left empty is an observable
    a replay cannot heal -- the row is there, the table is there, and search
    returns nothing."""
    conn, path = db_at(tmp_path, 9)
    conn.execute("INSERT INTO memories(uid, kind, scope, fact, author, status, "
                 "created_ns) VALUES('u1','decision','r1','a distinctive corpus "
                 "token','a','live',1)")
    conn.execute("INSERT INTO memories_fts(memories_fts) VALUES('delete-all')")  # break it
    store.migrate(conn, path)
    hit = conn.execute("SELECT rowid FROM memories_fts WHERE memories_fts MATCH "
                       "'distinctive'").fetchall()
    assert len(hit) == 1
