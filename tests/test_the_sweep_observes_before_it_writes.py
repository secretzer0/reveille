"""The sweep holds the write lock for its writes, never for its observations
(ruling 13366's ask, measured before believed).

The wait series (2.1 / 2.7 / 7.1 / 7.5 / 7.0s) went bimodal, and the high
cluster was three points inside half a second -- the shape of a cause, not of
contention noise. The holder was then MEASURED in the field: one write-lock
hold span of 7.54s per 300s interval, the sweep tick to the second. The old
_sweep_once opened its implicit write transaction at the FIRST container's
sessions_seen DELETE and held it across every LATER container's docker execs
(two to three per body) plus _stop_superseded's per-body docker inspect and
broker HTTP read, committing once at the end -- so any launcher open during a
tick waited the remainder of the whole fleet walk.

The fix is the architect's sentence: do the read outside the transaction.
Observation (docker, HTTP, SELECTs -- none of which need the write lock)
runs first; the handful of tiny sessions_seen rows land in one short write
transaction at the end. The kills, audits and stops keep their order.

Proven red on main a6db7aa: with three seeded containers, a second connection
cannot BEGIN IMMEDIATE during containers two and three's observation calls.
"""
import importlib.util
import pathlib
import sqlite3

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def _seed(tmp_path, bodies=3):
    path = str(tmp_path / "launcher.db")
    conn = rl._db(path)
    for i in range(bodies):
        conn.execute(
            "INSERT INTO containers(user, agent, created_ns) VALUES(?,?,1)",
            ("u", f"a{i}"))
        conn.execute(
            "INSERT INTO sessions_seen(user, agent, session, first_seen_ns) "
            "VALUES(?,?,?,1)", ("u", f"a{i}", "d-stale"))
    conn.commit()
    return path, conn


def _quiet(monkeypatch):
    monkeypatch.setattr(rl, "_harvest_gate_audit", lambda u, a, g: None)
    monkeypatch.setattr(rl, "sweep_actions", lambda g, live, s, n: ([], []))
    monkeypatch.setattr(rl, "_stop_superseded", lambda conn: [])


def test_the_lock_is_free_while_the_sweep_observes(tmp_path, monkeypatch):
    """The measured defect as a deterministic gate: a second opener probes
    BEGIN IMMEDIATE from inside each container's observation call. On main
    the first container's DELETE has already opened the write transaction,
    so every later probe blocks -- the 7.5s hold in miniature."""
    path, conn = _seed(tmp_path)
    _quiet(monkeypatch)
    probes = []
    def observe(user, agent):
        c2 = sqlite3.connect(path, timeout=0.05)
        try:
            c2.execute("BEGIN IMMEDIATE")
            c2.rollback()
            probes.append(True)
        except sqlite3.OperationalError:
            probes.append(False)
        finally:
            c2.close()
        return {}
    monkeypatch.setattr(rl, "_live_grant_sessions", observe)
    rl._sweep_once(conn)
    assert len(probes) == 3, "the sweep did not walk every container"
    assert all(probes), (
        f"probes={probes}: the write lock was HELD during another container's "
        f"observation -- the transaction still spans the docker walk")


def test_the_writes_still_land_after_the_observation(tmp_path, monkeypatch):
    """Moving the writes must not lose them: the stale rows seeded per body
    are gone after the tick (live={} everywhere -> emptied), which is the
    DELETE+INSERT phase executing for every container."""
    path, conn = _seed(tmp_path)
    _quiet(monkeypatch)
    monkeypatch.setattr(rl, "_live_grant_sessions", lambda u, a: {})
    rl._sweep_once(conn)
    left = conn.execute("SELECT count(*) c FROM sessions_seen").fetchone()["c"]
    assert left == 0, f"{left} stale session rows survived the tick"


def test_the_corpse_stop_keeps_its_place_in_the_walk():
    """_stop_superseded stays in the OBSERVATION phase, after the per-body
    walk and before the write transaction -- its docker and HTTP reads are
    exactly what must not run under the lock, and its order relative to the
    idle probe is load-bearing (12320 A)."""
    src = pathlib.Path(rl.__file__).read_text()
    body = src[src.index("def _sweep_once("):src.index("def cmd_sweep(")]
    stop = body.index("_stop_superseded(conn)")
    first_write = body.index('conn.execute("DELETE FROM sessions_seen')
    commit = body.rindex("conn.commit()")
    assert stop < first_write < commit, (
        "the write phase must be the LAST thing in the tick, after the corpse "
        "stop's reads")
