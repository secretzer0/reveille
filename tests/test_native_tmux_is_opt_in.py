"""Native tmux is OPT-IN, and the launcher is run as shell to prove it.

Operator directive: tmux must not be the default on a native box even when it
is installed. It exists in this project for the CONTAINER, where ttyd attaches
to the session docker/entrypoint.sh starts itself ("agent") -- that path never
calls agent-launch, so the native default costs the browser plane nothing.

The old behaviour fired on `command -v tmux` alone: any box with tmux on PATH
was silently re-execed into a session it never asked for, and there was no way
to decline -- `--no-tmux` fell through the -* arm to claude and broke the launch
with an error naming the wrong program. Both halves are pinned here, and both
are EXECUTED: a stub tmux and a stub claude on PATH record which one the
launcher actually reached, because the whole defect was a control-flow
question that reading the file is what got wrong in the first place.
"""
import os
import pathlib
import subprocess

import pytest

LAUNCH = pathlib.Path(__file__).resolve().parent.parent / "src" / "reveille" / "agent-launch"

STUB = """#!/bin/sh
printf '%s\\n' "$1" >> {log}
exit 0
"""


@pytest.fixture
def box(tmp_path):
    """A PATH holding only our stubs, and a HOME with no agent.env -- so the
    launcher cannot pick up this machine's real credential file."""
    binn = tmp_path / "bin"
    binn.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    for name in ("tmux", "claude", "curl"):
        p = binn / name
        p.write_text(STUB.format(log=tmp_path / f"{name}.log"))
        p.chmod(0o755)

    def run(*args, **env_extra):
        env = dict(os.environ, PATH=f"{binn}:{os.environ['PATH']}",
                   HOME=str(home), REVEILLE_ENV_FILE=str(home / "nonexistent"))
        env.pop("TMUX", None)
        env.pop("REVEILLE_TMUX", None)
        env.update(env_extra)
        subprocess.run(["bash", str(LAUNCH), "--role", "probe", "--token", "t",
                        "--url", "http://127.0.0.1:1", *args],
                       env=env, capture_output=True, text=True, timeout=120)
        ran = lambda n: (tmp_path / f"{n}.log").exists()  # noqa: E731
        return ran("tmux"), ran("claude")

    return run


def test_tmux_is_not_the_default_even_when_installed(box):
    """The directive, stated as the test that would have caught it: tmux is on
    PATH and must still not be reached."""
    tmux_ran, claude_ran = box()
    assert not tmux_ran, (
        "the launcher re-execed into tmux with nobody asking -- tmux is "
        "installed on this box, which used to be the entire condition")
    assert claude_ran, "claude was never reached"


def test_the_flag_opts_in(box):
    tmux_ran, _ = box("--tmux")
    assert tmux_ran, "--tmux did not start a session"


def test_the_env_var_opts_in(box):
    tmux_ran, _ = box(REVEILLE_TMUX="1")
    assert tmux_ran, "REVEILLE_TMUX=1 did not start a session"


def test_the_flag_can_decline_what_the_env_asked_for(box):
    """--no-tmux must be a case in the parser, not a fall-through: the -* arm
    forwards anything unrecognised to claude, so this used to launch claude
    with a flag claude does not have."""
    tmux_ran, claude_ran = box("--no-tmux", REVEILLE_TMUX="1")
    assert not tmux_ran, "--no-tmux did not override REVEILLE_TMUX"
    assert claude_ran, "--no-tmux broke the launch instead of declining tmux"


def test_no_tmux_never_reaches_claude_as_an_argument(box, tmp_path):
    """The silent half of the same defect: the flag landing on claude."""
    box("--no-tmux")
    passed = (tmp_path / "claude.log").read_text()
    assert "--no-tmux" not in passed, (
        f"--no-tmux was forwarded to claude: {passed!r}")
