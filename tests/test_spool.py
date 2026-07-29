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
