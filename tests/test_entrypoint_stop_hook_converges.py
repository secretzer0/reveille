"""The entrypoint's reachability contracts converge on correctness; everything
else keeps the agent's choice (ruling 9094, the install.py lesson one boot path
over). The contract set GREW on 2026-08-22: the operator ruled that every
container reboots wide open and prompt-free -- permission mode and the
bypass-prompt skip joined the Stop hook in the converge= block after the
2.1.240 bus-deaf incident, where a present-but-wrong value cost a body the bus.

The defect: patch() setdefault-merged the whole file, and hooks -> Stop is a
LIST, so a persisted ~/.claude/settings.json carrying a wrong-but-present Stop
hook kept it across every re-provision -- the exact shape install.py's hook
half was fixed for in 0.2.80, in the sibling writer, and re-provisioning is
the one remedy anyone reaches for.

These tests run the SHIPPED heredoc, extracted from docker/entrypoint.sh by
content -- a copy would drift and pass while the entrypoint regressed (the
test_role_block discipline)."""
import json
import os
import pathlib
import subprocess
import sys

ENTRYPOINT = pathlib.Path(__file__).resolve().parent.parent / "docker" / "entrypoint.sh"

HOOK = "/usr/local/bin/agent-stop-hook"
STALE = "/home/agent/.cache/uv/archive-v0/deadbeef/agent-stop-hook"


def _wizard_script():
    """The first-run-wizard python, cut from the entrypoint between its heredoc
    fences. Selected by content: the file carries more than one python3 heredoc."""
    text = ENTRYPOINT.read_text()
    for chunk in text.split("python3 - <<'PY'\n")[1:]:
        body = chunk.split("\nPY\n")[0]
        if "hasCompletedOnboarding" in body:
            return body
    raise AssertionError("wizard heredoc not found in entrypoint.sh")


def boot(home):
    """One container boot's worth of the wizard block, against a scratch HOME."""
    r = subprocess.run([sys.executable, "-c", _wizard_script()],
                       env=dict(os.environ, HOME=str(home)),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return json.loads((home / ".claude" / "settings.json").read_text())


def _seed(home, settings):
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(settings))


def _stop_command(settings):
    return settings["hooks"]["Stop"][0]["hooks"][0]["command"]


def test_a_wrong_but_present_stop_hook_is_rewritten(tmp_path):
    _seed(tmp_path, {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": STALE}]}]}})
    # the fixture fired: the stale path is really in the file before boot
    assert STALE in (tmp_path / ".claude" / "settings.json").read_text()
    out = boot(tmp_path)
    assert _stop_command(out) == HOOK, (
        f"a re-provision kept the stale Stop hook {STALE!r} -- "
        "setdefault on a reachability control, the original defect")


def test_fresh_home_gets_the_hook(tmp_path):
    out = boot(tmp_path)
    assert _stop_command(out) == HOOK


def test_the_zero_prompt_contract_is_forced_and_other_choices_survive(tmp_path):
    # Operator rule 2026-08-22: the permission MODE is not the agent's to keep
    # any more -- a body that boots into any prompt is a body nobody is watching
    # that never reaches the bus. An agent that switched itself to plan mode is
    # put back on every boot. Its OTHER keys -- allow rules it accumulated, a
    # model choice -- are still its own: converge recurses dicts, so siblings
    # of the forced keys ride along untouched.
    _seed(tmp_path, {"permissions": {"defaultMode": "plan",
                                     "allow": ["Bash(make test)"]},
                     "model": "opus",
                     "hooks": {"Stop": [{"hooks": [
                         {"type": "command", "command": STALE}]}]}})
    out = boot(tmp_path)
    assert out["permissions"]["defaultMode"] == "bypassPermissions", (
        "the zero-prompt contract did not converge -- a body that kept plan "
        "mode boots into a prompt nobody answers")
    assert out["skipDangerousModePermissionPrompt"] is True
    assert out["permissions"]["allow"] == ["Bash(make test)"], (
        "converge clobbered a sibling key -- allow rules are the agent's own")
    assert out["model"] == "opus"
    assert _stop_command(out) == HOOK


def test_sibling_hooks_ride_along_untouched(tmp_path):
    # hooks.Stop is replaced whole; a hook the agent added under another key
    # is not the entrypoint's to touch.
    _seed(tmp_path, {"hooks": {
        "PreToolUse": [{"hooks": [{"type": "command", "command": "/my/guard"}]}],
        "Stop": [{"hooks": [{"type": "command", "command": STALE}]}]}})
    out = boot(tmp_path)
    assert out["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "/my/guard"
    assert _stop_command(out) == HOOK


def test_a_wrong_type_at_hooks_is_replaced_not_crashed_on(tmp_path):
    _seed(tmp_path, {"hooks": "corrupted by hand"})
    out = boot(tmp_path)
    assert _stop_command(out) == HOOK
