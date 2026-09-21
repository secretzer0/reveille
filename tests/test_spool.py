#!/usr/bin/env python3
"""DES-003 W1 gate: spool semantics. The properties that kill the waiter
lesson family -- pre-existing entry fires immediately (I3), concurrent
watchers are indistinguishable from one (I2), drain-then-rearm never re-rings
a processed entry, and the daemon flock is a real singleton."""
import fcntl
import json
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import spool  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
RING = '{"wake":true,"reason":"message","unread":3}'


def _env(tmp_path):
    return dict(os.environ, REVEILLE_SPOOL=str(tmp_path),
                PYTHONPATH=str(REPO / "src"))


def _watch(tmp_path, agent="a1", timeout=15):
    return subprocess.run(
        [sys.executable, "-m", "reveille.watch", agent],
        env=_env(tmp_path), capture_output=True, text=True, timeout=timeout)


def test_write_ring_is_atomic_and_sorted(tmp_path):
    base = str(tmp_path)
    p1 = spool.write_ring("a1", RING, base=base)
    p2 = spool.write_ring("a1", '{"wake":true,"unread":4}', base=base)
    assert spool.entries("a1", base=base) == [p1, p2]  # oldest first
    assert not os.listdir(os.path.join(spool.agent_dir("a1", base=base), "tmp"))
    assert spool.oldest("a1", base=base)[1] == RING


def test_preexisting_entry_fires_immediately(tmp_path):
    # I3: a ring that arrived while unarmed is delivered at the next arm.
    spool.write_ring("a1", RING, base=str(tmp_path))
    t0 = time.monotonic()
    r = _watch(tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["unread"] == 3
    assert time.monotonic() - t0 < 5  # immediate, not a poll cycle later


def test_watcher_fires_on_later_delivery_and_concurrent_watchers_agree(tmp_path):
    # I2: N watchers see the same file, all exit 0 with the same ring, none
    # deletes it -- duplicates are harmless by construction.
    procs = [subprocess.Popen(
        [sys.executable, "-m", "reveille.watch", "a1"], env=_env(tmp_path),
        stdout=subprocess.PIPE, text=True) for _ in range(2)]
    time.sleep(1.0)                          # both blocked on an empty spool
    spool.write_ring("a1", RING, base=str(tmp_path))
    outs = [p.communicate(timeout=15)[0] for p in procs]
    assert all(p.returncode == 0 for p in procs)
    assert [json.loads(o)["unread"] for o in outs] == [3, 3]
    assert len(spool.entries("a1", base=str(tmp_path))) == 1  # nobody deleted


def test_drain_then_rearm_does_not_reloop(tmp_path):
    # The spool analog of ack-before-rearm: the session deletes what it
    # processed, so the next watcher blocks instead of instantly re-firing
    # on the same entry -- the self-ring loop is structurally gone.
    p = spool.write_ring("a1", RING, base=str(tmp_path))
    r = _watch(tmp_path)
    assert r.returncode == 0
    os.unlink(p)                              # drain: delete what we processed
    w = subprocess.Popen([sys.executable, "-m", "reveille.watch", "a1"],
                         env=_env(tmp_path), stdout=subprocess.PIPE, text=True)
    time.sleep(1.5)
    assert w.poll() is None, "watcher re-fired on a drained entry"
    spool.write_ring("a1", '{"wake":true,"unread":1}', base=str(tmp_path))
    out, _ = w.communicate(timeout=15)
    assert json.loads(out)["unread"] == 1     # fresh ring, fresh fire


def _waked_nudging(tmp_path, nudge_s, agent="a1"):
    # The URL resolves nowhere on purpose: the nudge must fire on the daemon's
    # wall clock even while the broker is unreachable (W3).
    return subprocess.Popen(
        [sys.executable, "-m", "reveille.waked",
         "--url", "ws://127.0.0.1:1/wake", "--name", agent,
         "--idle-nudge", str(nudge_s)],
        env=_env(tmp_path), stderr=subprocess.DEVNULL)


def test_nudge_due_is_pure_and_zero_disables():
    S = 10**9
    assert spool  # keep import obvious
    from reveille import waked
    assert waked.nudge_due(0, 3 * S, 3) is True
    assert waked.nudge_due(0, 2 * S, 3) is False
    assert waked.nudge_due(0, 10**15, 0) is False      # 0 never nudges
    assert json.loads(waked.nudge_frame(1800)) == \
        {"wake": True, "reason": "idle-nudge", "idle_seconds": 1800}


def _wait_for_entries(tmp_path, n, timeout_s, what):
    """Poll until the spool holds n entries, or fail NAMING THE STEP that never
    finished rather than the whole test."""
    deadline = time.monotonic() + timeout_s
    while True:
        entries = spool.entries("a1", base=str(tmp_path))
        if len(entries) >= n:
            return entries
        assert time.monotonic() < deadline, what
        time.sleep(0.05)


def test_the_idle_nudge_asks_once_and_then_waits_for_a_reason(tmp_path):
    """W3 gate, REWRITTEN to the rule it now follows.

    It used to assert a SECOND nudge arrived one interval after the first --
    "one per interval, never a burst" -- and that is exactly the behaviour the
    operator cut on 2026-09-20: "there is NO REASON to force tokens to be
    wasted by communicating a poll that had no data to return... if nothing was
    there and it wrote no rings there is no reason to disturb".

    Measured on the host that day: 33 idle-nudges against 18 messages and one
    mail, TEN of them inside the same second because the host daemon serves
    eleven identities whose idle timers run together. Every one spent a model
    turn to find nothing.

    The nudge still does its whole job -- restart an agent that parked work in
    an earlier turn -- by firing once after any activity, daemon start
    included. What it no longer does is ask again, on a timer, of an agent that
    answered the first one by having nothing to do.
    """
    p = _waked_nudging(tmp_path, 1)
    try:
        entries = _wait_for_entries(tmp_path, 1, 10.0, "no first nudge within 10s")
        with open(entries[0]) as f:
            obj = json.loads(f.read())
        assert obj["reason"] == "idle-nudge" and obj["idle_seconds"] == 1
        # FIVE intervals pass with no real ring. Under the old rule that was
        # five more nudges and five more model turns.
        time.sleep(5.0)
        again = spool.entries("a1", base=str(tmp_path))
        assert len(again) == 1, (
            f"{len(again)} nudges in 5 intervals with nothing to report -- "
            f"the nudge is polling again")
    finally:
        p.terminate()
        p.wait(timeout=5)


def test_idle_nudge_zero_writes_none_ever(tmp_path):
    p = _waked_nudging(tmp_path, 0)
    try:
        time.sleep(2.5)
    finally:
        p.terminate()
        p.wait(timeout=5)
    assert spool.entries("a1", base=str(tmp_path)) == []


def test_unarmed_nudge_fires_at_next_arm(tmp_path):
    # I3 must hold for synthetic rings too: a nudge that landed while no
    # watcher was armed waits in the spool and fires immediately on arm.
    from reveille import waked
    spool.write_ring("a1", waked.nudge_frame(3), base=str(tmp_path))
    r = _watch(tmp_path)
    assert r.returncode == 0 and json.loads(r.stdout)["reason"] == "idle-nudge"


def test_waked_flock_singleton_second_start_exits_zero(tmp_path):
    # The Stop hook spawns blindly; the loser must exit 0 on its own, before
    # ever touching the network (the URL below resolves nowhere).
    lock = open(spool.lock_path("a1", base=str(tmp_path)), "w")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)   # we are the "daemon"
    r = subprocess.run(
        [sys.executable, "-m", "reveille.waked",
         "--url", "ws://127.0.0.1:1/wake", "--name", "a1"],
        env=_env(tmp_path), capture_output=True, text=True, timeout=15)
    assert r.returncode == 0
    assert "already held" in r.stderr
    lock.close()


def test_follow_emits_each_ring_once_and_never_exits(tmp_path):
    # THE STORM, measured 2026-08-19 (red-shirt-01, msg 12378): wrapping the
    # one-shot in `while true; do wake-watch ...; done` re-fires on the SAME
    # undeleted spool file until the harness suppresses the flood -- ~20
    # notifications for one ring. --follow is what replaces that loop, so it
    # must emit a file ONCE and then stay silent on it whether or not the
    # session has drained it, and it must not exit (that is the whole point:
    # one arm covers a session, no re-arm ritual).
    spool.write_ring("a1", RING, base=str(tmp_path))
    w = subprocess.Popen(
        [sys.executable, "-m", "reveille.watch", "--follow", "a1"],
        env=_env(tmp_path), stdout=subprocess.PIPE, text=True)
    try:
        time.sleep(1.0)          # the pre-existing ring is delivered at arm
        spool.write_ring("a1", '{"wake":true,"unread":1}', base=str(tmp_path))
        time.sleep(3.0)          # several poll cycles with both files present
        assert w.poll() is None, "--follow exited; it is meant to run the session"
    finally:
        w.terminate()
    out, _ = w.communicate(timeout=10)
    unread = [json.loads(line)["unread"] for line in out.splitlines() if line.strip()]
    assert unread == [3, 1], f"each ring exactly once, in order: {out!r}"
    assert len(spool.entries("a1", base=str(tmp_path))) == 2   # I4: deletes nothing


def test_the_watch_backend_is_chosen_per_os_and_kqueue_is_wired(tmp_path, monkeypatch):
    """Ring latency on macOS (operator 2026-09-08): the 2s poll was the only
    non-Linux path, so every ring on a Mac ate up to 2s. kqueue is the BSD
    native equivalent and stdlib -- _arm now dispatches inotify -> kqueue ->
    poll. Linux CI cannot RUN kqueue (select has no kqueue here, which is
    itself the dispatch test), so the kqueue branch is proven WIRED under a
    fake select: armed with EV_ADD|EV_CLEAR on VNODE writes, wait() drains
    with the TICK_S timeout (2 s since 24202: the parent check rides on it),
    close() closes both the kq and the dirfd. The
    macOS field run stays honestly unverified until a Mac runs a body."""
    from reveille import watch
    real_kqueue_pair = watch._kqueue_pair

    # Dispatch tail: no inotify, no kqueue -> the poll pair, and its close
    # must be a harmless no-op (the follow loop calls it on every exit).
    monkeypatch.setattr(watch, "_inotify_fd", lambda p: None)
    monkeypatch.setattr(watch, "_kqueue_pair", lambda p: None)
    wait, close = watch._arm(str(tmp_path))
    slept = []
    monkeypatch.setattr(watch.time, "sleep", lambda s: slept.append(s))
    wait()
    close()
    assert slept == [2], "the tail of the dispatch is the 2s poll"

    # The kqueue branch, wired under a fake: the calls a real Mac would make.
    calls = {"control": [], "closed": []}

    class _KQ:
        def control(self, changes, maxev, timeout=None):
            calls["control"].append((changes, maxev, timeout))
            return []
        def close(self):
            calls["closed"].append("kq")

    class _FakeSelect:
        KQ_FILTER_VNODE, KQ_EV_ADD, KQ_EV_CLEAR = -4, 1, 32
        KQ_NOTE_WRITE, KQ_NOTE_EXTEND = 2, 4
        kqueue = staticmethod(_KQ)
        @staticmethod
        def kevent(ident, filt, flags, fflags):
            calls["kevent"] = (filt, flags, fflags)
            return ("ev", ident)

    monkeypatch.setattr(watch, "select", _FakeSelect)
    pair = real_kqueue_pair(str(tmp_path))
    assert pair is not None, "a select WITH kqueue must arm"
    kq, dirfd = pair
    assert calls["kevent"] == (-4, 1 | 32, 2 | 4), (
        "VNODE filter, EV_ADD|EV_CLEAR, NOTE_WRITE|NOTE_EXTEND -- the arm")
    assert calls["control"][0] == ([("ev", dirfd)], 0, 0), "registration"

    monkeypatch.setattr(watch, "_inotify_fd", lambda p: None)
    monkeypatch.setattr(watch, "_kqueue_pair", lambda p: pair)
    wait, close = watch._arm(str(tmp_path))
    wait()
    assert calls["control"][-1] == (None, 4, watch.TICK_S), (
        "wait() drains up to 4 events with the TICK_S timeout")
    close()
    assert calls["closed"] == ["kq"], "close() closed the kq (dirfd proof below)"
    try:
        os.fstat(dirfd)
        raise AssertionError("close() left the directory fd open")
    except OSError:
        pass


def test_one_nudge_per_real_ring():
    """THE NUDGE ASKS ONCE, THEN WAITS FOR A REASON (operator, 2026-09-20:
    "there is NO REASON to force tokens to be wasted by communicating a poll
    that had no data to return").

    Measured on the host that day: 33 idle-nudges against 18 messages and one
    mail, ten of them in the SAME SECOND because the host daemon serves eleven
    identities whose idle timers run together. Each spent a model turn to find
    nothing. The mail probe already reasoned this way -- a spurious ring spends
    a turn and is not idempotent -- and the nudge was the one producer nobody
    applied it to.
    """
    from reveille import waked
    S = 10**9
    # armed at start: whatever was parked before the daemon existed gets asked
    assert waked.nudge_due(0, 3 * S, 3, armed=True) is True
    # having asked, it does not ask again
    assert waked.nudge_due(0, 3 * S, 3, armed=False) is False
    # ...however long it waits
    assert waked.nudge_due(0, 3000 * S, 3, armed=False) is False
    # and 0 still disables it outright, armed or not
    assert waked.nudge_due(0, 10**15, 0, armed=True) is False


def test_a_real_ring_re_arms_the_nudge():
    """The nudge still does its whole job: restart an agent that parked work in
    an earlier turn. It fires once after ANY activity -- it just stops asking
    an agent that answered by having nothing to do."""
    from reveille import waked
    state = {"last": 10**9, "armed": False}      # a nudge has just fired
    assert waked.nudge_due(state["last"], 10**12, 3, state["armed"]) is False
    # a real ring arrives
    state["last"], state["armed"] = 2 * 10**9, True
    assert waked.nudge_due(state["last"], 10**12, 3, state["armed"]) is True


