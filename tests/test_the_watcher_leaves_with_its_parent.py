"""When the parent leaves, the watcher leaves (operator 24200, ruled 24202).

wake-watch is armed by a shell each turn; when that shell dies -- the
harness stopped tracking it, the session ended -- the watcher must not live
on reparented to init. Driven for real: a bash parent runs the watcher, the
parent is killed with SIGKILL (nothing forwarded, the way a harness kill
looks from the child), and the child must be gone within a couple of ticks.
Both halves are exercised: the kernel death signal on Linux and the ppid
check on the tick; the mutation that drops both leaves the child alive.
"""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _child_of(parent_pid):
    r = subprocess.run(["pgrep", "-P", str(parent_pid)], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()]


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def test_the_watcher_dies_when_its_parent_shell_is_killed(tmp_path):
    env = {**os.environ, "REVEILLE_SPOOL": str(tmp_path), "PYTHONPATH": str(ROOT / "src")}
    parent = subprocess.Popen(
        ["bash", "-c", f"{sys.executable} -m reveille.watch ana-{os.getpid()}; true"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    # bash -c with a trailing command does NOT exec the watcher: it forks it
    # and waits, so the watcher's parent is bash and killing bash is exactly
    # what a harness kill or a session end looks like from the child.
    deadline = time.monotonic() + 5
    kids = []
    while not kids and time.monotonic() < deadline:
        kids = _child_of(parent.pid)
        time.sleep(0.05)
    assert kids, "the watcher never started under its parent"
    child = kids[0]
    time.sleep(0.5)
    assert _alive(child), "the watcher died on its own before the parent did"
    parent.send_signal(signal.SIGKILL)
    parent.wait(timeout=5)
    deadline = time.monotonic() + 5
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "the watcher outlived the shell that armed it"
