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
import types

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


class _Conn:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, _sql):
        rows = self.rows
        class _Cur:
            def fetchall(self):
                return rows
        return _Cur()


def _tick(monkeypatch, pane, calls, running=True):
    """One watch tick over a single stuck agent, docker recorded, no IO."""
    monkeypatch.setattr(rl, "_inspect_container",
                        lambda name: {"running": running, "env": {}, "image": "i",
                                      "cmd": [], "network": "n", "image_id": "x"})
    monkeypatch.setattr(rl, "_agent_pane_tail", lambda name: pane)
    monkeypatch.setattr(rl, "_shared_login_usable", lambda user: True)
    monkeypatch.setattr(rl, "sync_before_start", lambda user, agent: None)
    monkeypatch.setattr(rl, "_audit", lambda *a, **k: None)
    monkeypatch.setattr(rl, "_docker",
                        lambda *a, **k: calls.append(a) or types.SimpleNamespace(
                            returncode=0, stdout=""))
    rl._login_watch_once(_Conn([("u", "a")]), "http://x")


def test_heal_nudges_once_then_restarts(monkeypatch):
    """Operator 14036: a nudge is not PROVEN to make claude re-read the file,
    so it does not get to be the only door. Tick 1 nudges; a pane still at a
    prompt on tick 2 gets the deterministic restart -- a claude started after
    the credential landed always reads it."""
    rl._needs_login.clear(); rl._nudged.clear(); rl._login_alerted.clear()
    stuck = "Select login method:\n> 1. subscription\n"
    calls = []
    _tick(monkeypatch, stuck, calls)
    keys = [c for c in calls if "send-keys" in c]
    assert len(keys) == 2 and not [c for c in calls if c[0] == "restart"], \
        "tick 1 must nudge (Escape, Enter) and not restart"
    calls.clear()
    _tick(monkeypatch, stuck, calls)
    assert [c for c in calls if c[0] == "restart"] and \
        not [c for c in calls if "send-keys" in c], \
        "tick 2 still stuck must restart, not nudge again"


def test_recovery_ends_the_stint(monkeypatch):
    """A recovered pane clears the nudge memory: the NEXT expiry starts at a
    nudge again, never at a restart inherited from last month."""
    rl._needs_login.clear(); rl._nudged.clear(); rl._login_alerted.clear()
    calls = []
    _tick(monkeypatch, "Select login method:\n", calls)          # nudge
    _tick(monkeypatch, "working fine\n> ", [])                   # recovered
    calls.clear()
    _tick(monkeypatch, "Select login method:\n", calls)          # new stint
    assert [c for c in calls if "send-keys" in c] and \
        not [c for c in calls if c[0] == "restart"], \
        "a fresh stint must start with a nudge"
