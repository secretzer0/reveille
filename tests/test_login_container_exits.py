"""The login container's own exit condition, EXECUTED rather than read.

The boot script is shell, so it is run as shell: a stub `tmux` on PATH that
always reports the login session alive stands in for the REPL `claude /login`
returns to. The whole defect lives in that gap -- the session outlives the
login, so a script waiting on the session waits forever, and the container only
ever went away because a browser was still polling /login/status.

Everything else here (the picker auto-advance, the URL) is pinned by the docker
gates; this file pins the one line that decides whether a successful login
leaves a container running.
"""
import importlib.util
import os
import pathlib
import subprocess

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    str(pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py"))
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)

# has-session ALWAYS succeeds: this is the REPL that outlives the login, which
# is the condition the old script waited on. capture-pane answers the picker
# immediately so the boot script's 60s picker loop does not dominate the test.
TMUX_STUB = """#!/bin/sh
case "$1" in
  capture-pane) echo "Select login method" ;;
  *) exit 0 ;;
esac
"""


def run_boot(tmp_path, credential):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    if credential:
        (home / ".claude" / ".credentials.json").write_text("{}")
    binn = tmp_path / "bin"
    binn.mkdir()
    (binn / "tmux").write_text(TMUX_STUB)
    (binn / "tmux").chmod(0o755)
    env = dict(os.environ, HOME=str(home), SEED="pass",
               PATH=f"{binn}:{os.environ['PATH']}")
    try:
        return subprocess.run(["sh", "-c", rl._LOGIN_BOOT], env=env,
                              capture_output=True, timeout=4).returncode
    except subprocess.TimeoutExpired:
        return None          # still running: the defect


def test_the_container_ends_itself_when_the_credential_lands(tmp_path):
    # The successful login: session still alive (claude sitting in its REPL),
    # credential written. The script must leave -- PID 1 leaving is the
    # container exiting, and nothing else in the system is obliged to notice.
    assert run_boot(tmp_path, credential=True) == 0


def test_an_unfinished_login_keeps_the_container_up(tmp_path):
    # The other half, so the exit is not simply "always leaves": with no
    # credential the flow is still live and the container must stay, or the
    # human loses the pane holding their URL mid-login.
    assert run_boot(tmp_path, credential=False) is None
