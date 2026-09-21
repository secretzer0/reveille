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


def _drive(monkeypatch, script, sids=None, grace=0):
    """Run the watcher over a scripted sequence of session censuses. `sids`
    maps pid -> conversation id; by default every pid is its own conversation,
    which is what a fresh `claude` is."""
    rings, posts = [], []
    live = {"pids": set(script[0])}
    sids = sids or {}
    monkeypatch.setattr(waked.doorbell, "session_id",
                        lambda pid, base=None: sids.get(pid, f"conv-{pid}"))
    monkeypatch.setattr(waked, "write_ring",
                        lambda a, f: rings.append((a, json.loads(f)["reason"])))
    monkeypatch.setattr(waked, "_reconcile",
                        lambda url, tok: (posts.append(tok), {"started": True})[1])
    monkeypatch.setattr(waked.doorbell, "inboxes_for",
                        lambda wd, base=None: [(p, "s", "t") for p in live["pids"]])
    state = {"last": 0, "armed": False}

    async def run():
        task = asyncio.create_task(
            waked._session_watcher("ana", "/w", 0.02, state, "ws://x/wake", "tok",
                                   grace_s=grace))
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


# ---------------------------------------------------------------------------
# A RESUME IS NOT A BODY WITHOUT MEMORY. Measured the day this shipped: an
# interrupt-and-continue made `claude --resume` replace pid 1691395 with
# 1700106, five seconds apart, BOTH carrying conversation 4a8f2471 -- and the
# watcher rang boot, asking a body that had read its digest two minutes
# earlier to read all 44k characters of it again. The pid says a body arrived;
# the sessionId says whether it arrived with its memory.

def test_a_resume_of_a_known_conversation_gets_no_boot_ring(monkeypatch):
    rings, _posts, _state = _drive(
        monkeypatch, [{1691395}, set(), {1700106}],
        sids={1691395: "4a8f2471", 1700106: "4a8f2471"})
    assert rings == [], rings


def test_a_fresh_conversation_in_the_same_directory_still_boots(monkeypatch):
    rings, _posts, _state = _drive(
        monkeypatch, [{101}, {101, 202}], sids={101: "conv-a", 202: "conv-b"})
    assert rings == [("ana", "boot")], rings


def test_a_session_that_cannot_say_which_conversation_it_is_boots():
    """Unknown is not known: a body we cannot prove has memory is told where
    its memory is, because that fall costs a turn and the other costs a mind."""
    assert waked.boot_due("", {"x": True}) is True
    assert waked.boot_due("x", {"x": True}) is False
    assert waked.boot_due("y", {"x": True}) is True


def test_remembered_conversations_are_bounded():
    known = {}
    waked.remember_sessions(known, (f"c{i}" for i in range(waked.KNOWN_SESSIONS_MAX + 50)))
    assert len(known) == waked.KNOWN_SESSIONS_MAX
    assert "c0" not in known and f"c{waked.KNOWN_SESSIONS_MAX + 49}" in known
    waked.remember_sessions(known, ["", None])
    assert len(known) == waked.KNOWN_SESSIONS_MAX


def test_the_session_id_reader_survives_a_missing_or_torn_descriptor(tmp_path):
    from reveille import doorbell
    assert doorbell.session_id(424242, base=str(tmp_path)) == ""
    (tmp_path / "7.json").write_text("{not json")
    assert doorbell.session_id(7, base=str(tmp_path)) == ""
    (tmp_path / "8.json").write_text('{"sessionId": "abc"}')
    assert doorbell.session_id(8, base=str(tmp_path)) == "abc"


# ---------------------------------------------------------------------------
# THE COOL-DOWN. Measured the evening the watcher shipped: an interrupt-and-
# continue took the session away and brought it back as `claude --resume` five
# seconds later -- one census tick -- and the departure had already asked the
# broker to fold. The first time, the fold RAN: a GPU pass for a body that was
# back before it finished, carrying its memory. The last departure now starts
# a cool-down instead, and only an identity that stays gone for all of it folds.

def test_the_field_sequence_a_resume_inside_the_cool_down_folds_nothing(monkeypatch):
    rings, posts, _s = _drive(monkeypatch, [{1691395}, set(), {1700106}],
                              sids={1691395: "4a8f2471", 1700106: "4a8f2471"},
                              grace=60)
    assert posts == [], "a resume five seconds later still asked the broker to fold"
    assert rings == [], "and it was rung to re-read memory it holds"


def test_an_exit_that_stays_gone_folds_once_the_cool_down_ends(monkeypatch):
    _r, posts, _s = _drive(monkeypatch, [{101}, set(), set(), set(), set()], grace=0.15)
    assert posts == ["tok"], posts


def test_nothing_folds_before_the_cool_down_ends(monkeypatch):
    _r, posts, state = _drive(monkeypatch, [{101}, set()], grace=60)
    assert posts == [] and state.get("cooling_since") is not None


def test_one_departure_is_one_fold_however_long_it_stays_gone(monkeypatch):
    """The cool-down clears when it fires; an idle directory must not ask the
    broker again every census tick for the rest of the night."""
    _r, posts, _s = _drive(monkeypatch, [{101}] + [set()] * 8, grace=0.1)
    assert posts == ["tok"], posts


def test_a_fresh_body_inside_the_cool_down_also_cancels_it(monkeypatch):
    """The identity is working again, and its own next exit reconciles. The
    cool-down only ever DELAYS a fold, never drops one."""
    _r, posts, _s = _drive(monkeypatch, [{101}, set(), {202}],
                           sids={101: "conv-a", 202: "conv-b"}, grace=60)
    assert posts == []


def test_the_exit_decision_is_pure():
    assert waked.reconcile_due(None, 100.0, 60, set()) is False       # nothing left
    assert waked.reconcile_due(50.0, 100.0, 60, set()) is False       # still cooling
    assert waked.reconcile_due(40.0, 100.0, 60, set()) is True        # gone the whole time
    assert waked.reconcile_due(40.0, 100.0, 60, {7}) is False         # a body is back


def test_the_cool_down_covers_the_measured_resume_with_room():
    """5 s measured; the constant must clear it by far more than one census."""
    assert waked.RECONCILE_GRACE_S >= 10 * waked.SESSION_WATCH_S
