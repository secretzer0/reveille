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


def test_idle_nudge_one_per_interval_never_a_burst(tmp_path):
    # W3 gate: no rings -> exactly one nudge per interval. 3.5s at interval 1
    # (checker granularity 1s) admits 2-3 entries; a burst would show many.
    p = _waked_nudging(tmp_path, 1)
    try:
        time.sleep(3.5)
    finally:
        p.terminate()
        p.wait(timeout=5)
    entries = spool.entries("a1", base=str(tmp_path))
    assert 2 <= len(entries) <= 3, f"{len(entries)} nudges in 3.5s at interval 1"
    stamps = []
    for e in entries:
        with open(e) as f:
            obj = json.loads(f.read())
        assert obj["reason"] == "idle-nudge" and obj["idle_seconds"] == 1
        stamps.append(int(os.path.basename(e).split(".")[0]))
    for a, b in zip(stamps, stamps[1:]):
        assert b - a >= 0.9 * 10**9, "nudges closer than the interval: a burst"


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
