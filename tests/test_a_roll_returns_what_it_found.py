"""A maintenance action returns the system to the state it found
(P1 from the 0.2.28 trip, ruled 13457).

The field case: the idle auto-roll walked thirteen STOPPED bodies -- stopped
by the operator, by hand, twenty minutes earlier, curating which of them ran
on a fresh account -- rolled each to the new image and LEFT IT RUNNING, with
no human present and each one booting claude against their subscription. The
health gate needs the new container running; nothing put it back.

Ruled, three properties: (1) the roll RECORDS what it found, durably in
launcher.db, BEFORE the first mutation -- a crash between the start and the
restore must leave a row that knows the body should be down, not a lost
local variable; (2) the restore happens PER BODY, immediately after the
health gate -- the fleet-wide restore-at-the-end is the race the operator's
own first fix lost; (3) the NEXT TICK reconciles an orphaned record: desired
stopped but running -> stopped, with a line naming why. And the log line
names the FINAL state ("0.2.28, stopped as found"), never just the
transition.

Proven red on main dec4cc7: no column, no record, no restore, no reconcile,
and the sweep's own line says "(was stopped; now running)".
"""
import importlib.util
import pathlib
import types

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

SRC = pathlib.Path(rl.__file__).read_text()


def _seeded(tmp_path):
    path = str(tmp_path / "launcher.db")
    conn = rl._db(path)
    conn.execute("INSERT INTO containers(user, agent, created_ns) VALUES('u','a',1)")
    conn.commit()
    return conn


def _wip(conn):
    return conn.execute(
        "SELECT roll_desired_running FROM containers WHERE user='u' AND agent='a'"
    ).fetchone()["roll_desired_running"]


def test_the_record_is_durable_and_the_restore_clears_it(tmp_path, monkeypatch):
    conn = _seeded(tmp_path)
    stops = []
    monkeypatch.setattr(rl, "_docker",
                        lambda *a, **k: stops.append(a) or
                        types.SimpleNamespace(returncode=0, stdout="", stderr=""))
    rl.record_found_state(conn, "u", "a", running=False, timeout=120)
    assert _wip(conn) == 0, "the found state must be IN THE DB before the start"
    dl = conn.execute("SELECT roll_deadline_ns FROM containers "
                      "WHERE user='u' AND agent='a'").fetchone()[0]
    import time as _t
    assert dl > _t.time_ns(), "the deadline must be in the roller's future"
    final = rl.restore_found_state(conn, "u", "a", was_running=False)
    assert final == "stopped as found"
    assert any("stop" in a for call in stops for a in call), (
        "a body found stopped was left running")
    assert _wip(conn) is None, "the restore must clear the in-flight record"
    # the found-running body: no stop, same clearing, final names the state
    stops.clear()
    rl.record_found_state(conn, "u", "a", running=True, timeout=120)
    assert _wip(conn) == 1
    assert rl.restore_found_state(conn, "u", "a", was_running=True) == "running"
    assert stops == [], "a body found running must not be stopped"
    assert _wip(conn) is None


def test_the_roll_records_before_the_stop_and_restores_after_the_gate():
    """Position gates on upgrade_agent (no docker harness -- named in the
    ship message): the record precedes the old container's stop, the restore
    follows the boot-report half of the health gate, and the rollback path
    clears the record too -- a rolled-back world is the found world."""
    body = SRC[SRC.index("def upgrade_agent("):SRC.index("def behind_image(")]
    rec = body.index("record_found_state(conn, user, agent")
    stop = body.index('_docker("stop", name')
    assert rec < stop, "the found state must be recorded before the stop"
    gate = body.index("read_boot_report(user, agent)")
    rest = body.index("restore_found_state(conn, user, agent")
    assert gate < rest, "the restore belongs after the health gate"
    rb = body.index("def rollback(")
    rb_end = body.index("try:", rb)
    assert "roll_desired_running" in body[rb:rb_end], (
        "a rollback must clear the in-flight record -- the old world is back")


def test_the_log_line_names_the_final_state():
    assert "(was stopped; now running)" not in SRC, (
        "the transition is not the outcome -- the line names the final state")
    assert SRC.count("res['final']") + SRC.count('res["final"]') >= 1
    assert SRC.count("out['final']") + SRC.count('out["final"]') >= 1


def test_the_next_tick_reconciles_an_orphaned_record(tmp_path, monkeypatch):
    """The ruled crash case, EXECUTED: a roll died between the start and the
    restore -- simulated by seeding the WIP row the dead roll left -- and the
    next sweep tick finds the body running against a desired-stopped record,
    stops it, says why, and clears the row. Without this, property 1 is a
    variable with extra steps."""
    conn = _seeded(tmp_path)
    conn.execute("UPDATE containers SET roll_desired_running=0, "
                 "roll_deadline_ns=1 WHERE user='u' AND agent='a'")
    conn.commit()
    monkeypatch.setattr(rl, "_harvest_gate_audit", lambda u, a, g: None)
    monkeypatch.setattr(rl, "sweep_actions", lambda g, live, s, n: ([], []))
    monkeypatch.setattr(rl, "_stop_superseded", lambda c: [])
    monkeypatch.setattr(rl, "_live_grant_sessions", lambda u, a: {})
    audits = []
    monkeypatch.setattr(rl, "_audit", lambda evt, **kw: audits.append((evt, kw)))
    calls = []
    def fake_docker(*args, check=True, capture=True):
        calls.append(args)
        out = "true" if "inspect" in args else ""
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    rl._sweep_once(conn)
    assert any("stop" in a for call in calls for a in call), (
        "the orphaned desired-stopped body was left running")
    assert _wip(conn) is None, "the reconcile must clear the record it acted on"
    assert any("ROLLRECONCILE" == evt and "deadline passed" in kw.get("reason", "")
               for evt, kw in audits), (
        "the reason must name the staleness -- an orphan is distinguishable "
        "from a tick that guessed")


def test_a_live_roll_survives_the_tick_untouched(tmp_path, monkeypatch):
    """13460's negative, the one that would have caught the race: the sweep
    is a thread in serve, roll_idle is a separate process, and during a roll
    of a STOPPED body the row says desired-stopped while the new container
    is DELIBERATELY running for wait_healthy. A record whose deadline is
    still in the future is a LIVE roll -- the tick must not stop the body
    and must not clear the row, or a roll of a stopped body fails at random
    by tick timing wearing an unhealthy-image mask."""
    import time as _t
    conn = _seeded(tmp_path)
    conn.execute("UPDATE containers SET roll_desired_running=0, "
                 "roll_deadline_ns=? WHERE user='u' AND agent='a'",
                 (_t.time_ns() + 240 * 10**9,))
    conn.commit()
    monkeypatch.setattr(rl, "_harvest_gate_audit", lambda u, a, g: None)
    monkeypatch.setattr(rl, "sweep_actions", lambda g, live, s, n: ([], []))
    monkeypatch.setattr(rl, "_stop_superseded", lambda c: [])
    monkeypatch.setattr(rl, "_live_grant_sessions", lambda u, a: {})
    monkeypatch.setattr(rl, "_audit", lambda evt, **kw: None)
    stops = []
    def fake_docker(*args, check=True, capture=True):
        if "stop" in args:
            stops.append(args)
        return types.SimpleNamespace(returncode=0, stdout="true", stderr="")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    rl._sweep_once(conn)
    assert stops == [], "the tick stopped a roll still inside its own deadline"
    assert _wip(conn) == 0, "the tick cleared a live roll's record"


def test_an_orphaned_wanted_running_record_is_cleared_without_a_stop(tmp_path, monkeypatch):
    """The other polarity: desired running, body running -- nothing to act
    on, but a WIP row must not live forever."""
    conn = _seeded(tmp_path)
    conn.execute("UPDATE containers SET roll_desired_running=1, "
                 "roll_deadline_ns=1 WHERE user='u' AND agent='a'")
    conn.commit()
    monkeypatch.setattr(rl, "_harvest_gate_audit", lambda u, a, g: None)
    monkeypatch.setattr(rl, "sweep_actions", lambda g, live, s, n: ([], []))
    monkeypatch.setattr(rl, "_stop_superseded", lambda c: [])
    monkeypatch.setattr(rl, "_live_grant_sessions", lambda u, a: {})
    monkeypatch.setattr(rl, "_audit", lambda evt, **kw: None)
    stops = []
    def fake_docker(*args, check=True, capture=True):
        if "stop" in args:
            stops.append(args)
        return types.SimpleNamespace(returncode=0, stdout="true", stderr="")
    monkeypatch.setattr(rl, "_docker", fake_docker)
    rl._sweep_once(conn)
    assert stops == [], "a wanted-running body must not be stopped"
    assert _wip(conn) is None
