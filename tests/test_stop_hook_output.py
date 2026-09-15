"""The Stop hook's two silent failures, gated.

Neither made a sound. A block verdict that was not JSON, so the block never
blocked; and a lock probe that read every failure of its OWN as "somebody holds
it", so the daemon it supervises was never spawned. Both end the same way --
exit 0, no message, an agent that looks supervised and is deaf.
"""
import fcntl
import json
import os
import pathlib
import shutil
import subprocess
import sys
import time


HOOK = pathlib.Path(__file__).parents[1] / "src" / "reveille" / "agent-stop-hook"

# Every executable the hook's run reaches, enumerated by reading the hook
# rather than discovered by flakes: a harness that stubs SOME binaries runs the
# rest for real (lesson a03ac949). `cd`, `pwd` and `printf` are bash builtins.
# `date` lives only in the launcher block, which needs $HOME/.reveille/
# launcher.env -- no fixture here writes one, so that block never runs. The
# three the fixture controls are `flock`, `pgrep` and `nohup`.
_REACHED = ("cat", "dirname", "readlink", "mkdir")


def _stub(path, body):
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


def _fixture(tmp_path, *, flock_exit=None, watcher_armed=True):
    """A PATH holding the hook's dependencies and nothing else.

    `flock_exit=None` IS the macOS fixture: the command is simply not there,
    which is the whole defect this file exists for. An integer installs a
    `flock` that answers with exactly that code.
    """
    commands = tmp_path / "bin"
    commands.mkdir()
    for name in _REACHED:
        real = shutil.which(name)
        assert real, f"{name} not on PATH -- the fixture cannot run honestly"
        (commands / name).symlink_to(real)
    if flock_exit is not None:
        _stub(commands / "flock", f"exit {flock_exit}\n")
    # The watcher block is a different subject; say it is armed so the hook
    # runs to its end without printing a verdict, unless a test wants the one.
    _stub(commands / "pgrep", f"exit {0 if watcher_armed else 1}\n")
    _stub(commands / "nohup", f'printf "%s\\n" "$*" > {tmp_path / "spawned"}\n')
    return commands


def _spawned(tmp_path):
    marker = tmp_path / "spawned"
    return marker.read_text() if marker.exists() else ""


def _wait_for_group(pgid, tmp_path, deadline_s=30.0):
    """Return once every process the hook started has exited.

    The spawn is a BACKGROUND job, so the hook exits before its child has
    written anything: asserting on the marker straight after wait() is a race
    that reads as "did not spawn" under load, and a sleep only moves the
    coin-flip. The process group emptying is the run's own observable end
    (lesson 592e81d9 -- fix the instrument, keep the assertion), and a timeout
    names what never finished.
    """
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.005)
    raise AssertionError(
        f"the hook's process group {pgid} never emptied in {deadline_s}s; "
        f"spawn marker so far: {_spawned(tmp_path)!r}")


def _run_hook(commands, tmp_path, *, python=None, role="mac-agent"):
    env = {
        "HOME": str(tmp_path / "home"),
        "PATH": str(commands),
        "REVEILLE_AGENT_ROLE": role,
        "REVEILLE_URL": "http://127.0.0.1:8765",
    }
    if python is not None:
        env["REVEILLE_HOOK_PYTHON"] = python
    proc = subprocess.Popen(
        ["/bin/bash", str(HOOK)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=env, start_new_session=True)
    out, err = proc.communicate("{}", timeout=30)
    assert proc.returncode == 0, f"hook exited {proc.returncode}, stderr: {err}"
    _wait_for_group(proc.pid, tmp_path)
    return out


def _hold(tmp_path, role="mac-agent"):
    """Take the spool lock the way a live waked takes it."""
    spool = tmp_path / "home" / ".reveille" / "spool" / role
    spool.mkdir(parents=True)
    fd = os.open(spool / ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    return fd


WAKED_ARGV = "reveille-waked --url ws://127.0.0.1:8765/wake --name mac-agent"


def test_the_unarmed_watcher_verdict_is_json_a_client_can_parse(tmp_path):
    """A `\\"` inside a SINGLE-quoted printf format is a printf escape, not a
    shell one: it reached stdout as a bare `"` and closed the JSON string
    early. Unparseable on every platform since the line was written, so the
    block never blocked -- a guard whose failure was printed and discarded."""
    commands = _fixture(tmp_path, flock_exit=1, watcher_armed=False)
    out = _run_hook(commands, tmp_path, python=sys.executable)

    verdict = json.loads(out)
    assert verdict["decision"] == "block"
    assert 'command="wake-watch --follow mac-agent"' in verdict["reason"]


def test_flock_answering_held_stops_a_second_daemon(tmp_path):
    """The Linux path's one positive answer. util-linux flock exits 1, and
    only 1, when the lock is genuinely taken."""
    commands = _fixture(tmp_path, flock_exit=1)
    _run_hook(commands, tmp_path, python=sys.executable)

    assert _spawned(tmp_path) == "", "a held lock means a live daemon already"


def test_flock_failing_for_its_own_reasons_still_spawns(tmp_path):
    """util-linux keeps its own failures OFF the conflict code -- measured on
    2.39.3: 1 conflict, 64 usage, 66 cannot-open. Reading 66 as "held" would
    make an unopenable lock file mean a supervised-looking, deaf agent."""
    commands = _fixture(tmp_path, flock_exit=66)
    _run_hook(commands, tmp_path, python=sys.executable)

    assert _spawned(tmp_path).startswith(WAKED_ARGV)


def test_without_flock_the_python_probe_spawns_when_the_lock_is_free(tmp_path):
    """macOS ships no `flock`. Before the fallback existed the command was not
    found, the probe read 127 as "held", and reveille-waked never started on a
    Mac at all: joined, watcher armed, and no wake socket."""
    commands = _fixture(tmp_path)
    _run_hook(commands, tmp_path, python=sys.executable)

    assert _spawned(tmp_path).startswith(WAKED_ARGV)


def test_without_flock_the_python_probe_holds_off_when_the_lock_is_taken(tmp_path):
    """The test that gives the other two their meaning. Since a probe that
    cannot decide now spawns, "it spawned" on its own proves only that nothing
    said 3 -- an fcntl probe that never ran would pass those. This one fails
    unless the fallback POSITIVELY decides, so it is what separates a working
    probe from a broken one that spawns by default."""
    commands = _fixture(tmp_path)
    fd = _hold(tmp_path)
    try:
        _run_hook(commands, tmp_path, python=sys.executable)
    finally:
        os.close(fd)

    assert _spawned(tmp_path) == "", "the fcntl probe did not see a held lock"


def test_a_probe_that_cannot_decide_spawns(tmp_path):
    """Ruling 17377. waked takes this same lock at startup and THAT is the
    singleton guard, so a blind spawn is safe and the loser exits 0 -- the
    hook's probe is an optimisation, never the authority. A missing
    interpreter, a stale REVEILLE_HOOK_PYTHON or an unimportable fcntl used to
    read as "held": exit 0, no daemon, no message. The same macOS symptom the
    fallback was added to fix, one layer down."""
    commands = _fixture(tmp_path)
    _run_hook(commands, tmp_path, python="/nonexistent/python")

    assert _spawned(tmp_path).startswith(WAKED_ARGV)
