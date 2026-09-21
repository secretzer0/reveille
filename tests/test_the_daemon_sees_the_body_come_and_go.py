"""waked watches the CLI sessions in each agent's directory (operator,
2026-09-20: "on boot, if the reveille mcp is enabled, it should rehydrate...
this can be handled by the waked noticing a new cli having joined... and when
the waked notices the agent exits, the waked sends a command to the server to
reconcile its hive-mind so it is fully up-to-date when it returns").

TWO EDGES ONLY THIS DAEMON CAN SEE. It already reads the session descriptors it
rings through, so noticing costs nothing new:

  ARRIVAL  -> reason=boot. A body that has just started has no hive memory, and
              until now nothing made the doctrine's "rehydrate at boot"
              actually happen -- the instruction existed and no event carried
              it.
  LAST EXIT -> POST /agent/digest. Whatever that body did is not in its digest
              yet; folding while it is gone means the next one rehydrates
              something current instead of something an hour stale.

The broker owns every reason to refuse the fold -- an interval guard, a
one-at-a-time lock, an activity check -- so the request is fire-and-forget and
a refusal is an answer.
"""
import asyncio
import json

from reveille import waked


def test_the_first_census_rings_nobody():
    """waked restarts on every deploy. Counting every live session as an
    ARRIVAL would wake every body on the machine to fetch memory it has."""
    assert waked.session_events(set(), {101, 102}, first=True) == (set(), set())


def test_arrivals_and_departures_are_a_set_difference():
    assert waked.session_events({101}, {101, 102}, False) == ({102}, set())
    assert waked.session_events({101, 102}, {101}, False) == (set(), {102})
    assert waked.session_events({101}, {101}, False) == (set(), set())


def _drive(monkeypatch, script):
    """Run the watcher over a scripted sequence of session censuses."""
    rings, posts = [], []
    live = {"pids": set(script[0])}
    monkeypatch.setattr(waked, "write_ring",
                        lambda a, f: rings.append((a, json.loads(f)["reason"])))
    monkeypatch.setattr(waked, "_reconcile",
                        lambda url, tok: (posts.append(tok), {"started": True})[1])
    monkeypatch.setattr(waked.doorbell, "inboxes_for",
                        lambda wd, base=None: [(p, "s", "t") for p in live["pids"]])
    state = {"last": 0, "armed": False}

    async def run():
        task = asyncio.create_task(
            waked._session_watcher("ana", "/w", 0.02, state, "ws://x/wake", "tok"))
        for step in script:
            live["pids"] = set(step)
            await asyncio.sleep(0.08)
        task.cancel()
    asyncio.run(run())
    return rings, posts, state


def test_a_body_arriving_gets_a_boot_ring(monkeypatch):
    rings, _posts, state = _drive(monkeypatch, [{101}, {101, 202}])
    assert rings == [("ana", "boot")], rings
    assert state["armed"] is True, "a boot ring is activity: it re-arms the nudge"


def test_a_body_already_there_when_the_daemon_starts_is_left_alone(monkeypatch):
    rings, _posts, _state = _drive(monkeypatch, [{101, 202}, {101, 202}])
    assert rings == [], rings


def test_the_last_exit_reconciles_the_hive_mind(monkeypatch):
    _rings, posts, _state = _drive(monkeypatch, [{101}, set()])
    assert posts == ["tok"], "no digest asked for when the body left"


def test_a_departure_that_leaves_another_session_working_is_not_an_exit(monkeypatch):
    """The identity is still working in another window, and a fold now would
    just be superseded by the next one."""
    _rings, posts, _state = _drive(monkeypatch, [{101, 202}, {101}])
    assert posts == [], "reconciled while a session was still working"


def test_no_token_means_no_reconcile(monkeypatch):
    """Nothing to authenticate with is not a reason to call anyway."""
    posts = []
    monkeypatch.setattr(waked, "_reconcile",
                        lambda url, tok: (posts.append(tok), {})[1])
    monkeypatch.setattr(waked.doorbell, "inboxes_for", lambda wd, base=None: [])
    monkeypatch.setattr(waked, "write_ring", lambda a, f: None)

    async def run():
        t = asyncio.create_task(
            waked._session_watcher("ana", "/w", 0.02, {"last": 0}, "ws://x", ""))
        await asyncio.sleep(0.08)
        t.cancel()
    asyncio.run(run())
    assert posts == []


def test_no_workdir_or_a_zero_interval_disables_the_watch():
    async def run(workdir, interval):
        t = asyncio.create_task(
            waked._session_watcher("ana", workdir, interval, {}, "ws://x", "tok"))
        await asyncio.sleep(0.05)
        done = t.done()
        t.cancel()
        return done
    assert asyncio.run(run("", 5)) is True
    assert asyncio.run(run("/w", 0)) is True


def test_the_boot_ring_names_itself():
    """A body cannot act on a reason it cannot read."""
    assert json.loads(waked.boot_frame()) == {"wake": True, "reason": "boot"}


def test_a_broker_refusal_is_an_answer_not_an_error():
    """The broker owns the interval guard, the fleet-wide lock and the activity
    check; waked asks and takes whatever it is told."""
    out = waked._reconcile("ws://127.0.0.1:1/wake", "tok")
    assert out["started"] is False and out["why"]
