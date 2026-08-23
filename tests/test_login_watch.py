"""The deaf-claude detector reads the BOTTOM of the pane, and only that
(operator 14028: sample the last 5 lines of every agent via tmux; a login
prompt marks the agent, a usable shared login heals it).

Pure-function coverage only: the tick's docker IO is thin glue around
pane_needs_login, which is where a wrong judgement would strand a fleet --
a prompt missed leaves a body dead with nobody told; a prompt hallucinated
sends Escape into a working claude's session.
"""
import importlib.util
import pathlib

_spec = importlib.util.spec_from_file_location(
    "reveille_launch",
    pathlib.Path(__file__).resolve().parent.parent / "scripts" / "reveille_launch.py")
rl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rl)


def test_boot_time_picker_is_a_login_prompt():
    assert rl.pane_needs_login(
        "Welcome to Claude Code\n\n Select login method:\n"
        " > 1. Claude account with subscription\n   2. Anthropic Console\n")


def test_mid_session_expiry_is_a_login_prompt():
    assert rl.pane_needs_login(
        "some earlier output\n"
        "API Error: 401 OAuth token has expired.\n"
        "Please run /login\n> ")


def test_paste_code_stage_is_a_login_prompt():
    assert rl.pane_needs_login("Paste code here if prompted > ")


def test_ordinary_repl_output_is_not():
    assert not rl.pane_needs_login(
        "I updated the deploy script.\n"
        "Tests pass: 12 passed in 3.2s\n> ")


def test_a_prompt_scrolled_off_the_tail_is_not():
    # the signature above 5 non-empty lines of ordinary output: claude
    # recovered, the pane moved on -- the OLD prompt must not re-mark it
    old = "OAuth token has expired\n"
    recovered = "\n".join(f"line {i}" for i in range(6)) + "\n> "
    assert not rl.pane_needs_login(old + recovered)


def test_blank_lines_do_not_shield_the_prompt():
    # tmux pads the pane bottom with blank rows; the window is 5 NON-EMPTY
    # lines, so padding must not push the prompt out of judgement
    assert rl.pane_needs_login("Select login method:\n" + "\n" * 30)


def test_empty_pane_is_not_a_prompt():
    assert not rl.pane_needs_login("")
    assert not rl.pane_needs_login(None)
