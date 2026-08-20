"""The login container's own exit condition, EXECUTED rather than read.

The boot script is shell, so it is run as shell: a stub `tmux` on PATH that
always reports the login session alive stands in for the REPL `claude /login`
returns to.

TWO defects live in this one wait loop. The first (8643): the session outlives
the login, so a script waiting on the session waits forever. The second
(ruling 13353, measured 2026-08-20): the fix for the first waited on
`[ -f .credentials.json ]` -- a sentinel that PRE-EXISTS a RE-login, so the
script broke its wait on the PREVIOUS account's credential about a second
after the picker rendered and the container died before anyone saw a URL.
Field numbers: picker visible at tick 7 (0.2s ticks), container exited tick 8,
credential mtime unmoved across three probes.

The rule: A SENTINEL THAT PRE-EXISTS THE WORK CANNOT SIGNAL THE WORK.
Completion is FINGERPRINT MOVED -- the baseline stamped into $FP0 before the
flow starts, "nothing" when no file exists, so the first login falls out of
the same rule instead of being a special case.

Proven red on main (9628967): the re-login fixture's script exits 0 on the
pre-existing credential -- the measured 2-second suicide in a test's clothes.
"""
import importlib.util
import os
import pathlib
import subprocess
import time

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

# has-session ALWAYS succeeds: this is the REPL that outlives the login.
# capture-pane answers the picker immediately so the boot script's 60s picker
# loop does not dominate the test.
TMUX_STUB = """#!/bin/sh
case "$1" in
  capture-pane) echo "Select login method" ;;
  *) exit 0 ;;
esac
"""

CRED = ".claude/.credentials.json"


def _fingerprint(home):
    """Computed the way the launcher stamps it -- str(st_mtime_ns), absent is
    "nothing". Deliberately NOT rl.login_fingerprint: the boot gates must red
    on main's BEHAVIOR, not on a helper main does not have."""
    try:
        return str(os.stat(home / CRED).st_mtime_ns)
    except FileNotFoundError:
        return "nothing"


def _arena(tmp_path, credential):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if credential:
        (home / CRED).write_text('{"account": "previous"}')
    binn = tmp_path / "bin"
    binn.mkdir()
    (binn / "tmux").write_text(TMUX_STUB)
    (binn / "tmux").chmod(0o755)
    env = dict(os.environ, HOME=str(home), SEED="pass",
               PATH=f"{binn}:{os.environ['PATH']}",
               FP0=_fingerprint(home))
    return home, env


def _boot(env):
    return subprocess.Popen(["sh", "-c", rl._LOGIN_BOOT], env=env,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL)


def test_a_relogin_survives_its_own_old_credential(tmp_path):
    """Run 3 in a test's clothes: credential present at t0, flow started --
    the container must SURVIVE past the picker. On main the script leaves on
    the previous account's credential within a second."""
    home, env = _arena(tmp_path, credential=True)
    p = _boot(env)
    try:
        time.sleep(1.5)   # picker answered instantly; wait loop has cycled
        assert p.poll() is None, (
            f"the boot script left with rc={p.poll()} on a credential that "
            f"PRE-EXISTS the flow -- a sentinel that pre-exists the work "
            f"cannot signal the work (13353)")
        # ... and completion fires exactly when the fingerprint MOVES: claude
        # writing the NEW account's credential is an mtime_ns change.
        (home / CRED).write_text('{"account": "new"}')
        assert p.wait(timeout=4) == 0, (
            "the fingerprint moved and the script did not leave")
    finally:
        p.kill()


def test_a_first_login_is_the_same_rule_not_a_special_case(tmp_path):
    """Absent-at-start is the fingerprint "nothing": the file appearing IS a
    fingerprint change, so the first-login path needs no second condition."""
    home, env = _arena(tmp_path, credential=False)
    assert env["FP0"] == "nothing"
    p = _boot(env)
    try:
        time.sleep(1.5)
        assert p.poll() is None, "no credential yet -- the flow is still live"
        (home / CRED).write_text('{"account": "first"}')
        assert p.wait(timeout=4) == 0
    finally:
        p.kill()


def test_a_flow_that_never_mints_keeps_the_container_up(tmp_path):
    """The ruled negative, boot side: no new credential, no completion -- or
    the human loses the pane holding their URL mid-login."""
    for credential in (True, False):
        _, env = _arena(tmp_path / str(credential), credential=credential)
        p = _boot(env)
        try:
            time.sleep(2.5)
            assert p.poll() is None, (
                f"credential={credential}: the script left without a new "
                f"credential -- a flow that mints nothing is never complete")
        finally:
            p.kill()


def test_a_missing_baseline_fails_loud(tmp_path):
    """No $FP0 must be a refusal, never a quiet fall-back to existence: a
    degraded sentinel is exactly the defect this file exists to keep out."""
    _, env = _arena(tmp_path, credential=True)
    del env["FP0"]
    p = _boot(env)
    try:
        assert p.wait(timeout=3) not in (None, 0), (
            "the boot script ran without its baseline")
    finally:
        p.kill()
