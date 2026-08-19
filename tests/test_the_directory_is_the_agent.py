"""THE DIRECTORY IS THE AGENT (operator ruling, 2026-08-13).

`reveille init` used to write one ~/.reveille/agent.env per HOST and ship a
reveille-agent wrapper to source it -- one machine, one identity, and a plain
`claude` session was silently deaf. The credential now lands in the agent
directory's .claude/settings.local.json env block, which Claude Code injects at
session start: plain `claude` run THERE is the agent, and every directory can
be its own agent. What is gated here is the write itself -- its converge
semantics, what it preserves, what it refuses, and its mode -- because this
file is the credential and the settings file is shared real estate.
"""
import json
import types
from unittest import mock
import os

import pytest

from reveille import cli


def test_a_fresh_directory_gets_the_env_block(tmp_path):
    path = cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    assert path == tmp_path / ".claude" / "settings.local.json"
    cfg = json.loads(path.read_text())
    assert cfg["env"] == {"REVEILLE_URL": "http://b:8765",
                          "REVEILLE_AGENT_ROLE": "dev-agent",
                          "REVEILLE_TOKEN": "sekrit",
                          "CAVEMAN_DEFAULT_MODE": "ultra",
                          "PONYTAIL_DEFAULT_MODE": "full"}
    # this file IS the credential: another account reading it ends the boundary
    assert oct(path.stat().st_mode)[-3:] == "600"


def test_modes_are_seeded_never_overridden(tmp_path):
    """A credential has one correct value and converges; a mode is the user's
    preference and only an ABSENCE is filled. A hand-tuned level surviving a
    re-run is the difference between an installer and an editor."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text(json.dumps(
        {"env": {"CAVEMAN_DEFAULT_MODE": "lite"}}))
    cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    env = json.loads((d / "settings.local.json").read_text())["env"]
    assert env["CAVEMAN_DEFAULT_MODE"] == "lite", "it overrode a tuned level"
    assert env["PONYTAIL_DEFAULT_MODE"] == "full", "it did not fill the absence"


def test_existing_settings_are_preserved_and_wrong_values_converge(tmp_path):
    """Converge, never detect presence: a wrong-but-present value that survives
    a re-run is permanent, and re-running the installer then CONFIRMS the broken
    state -- the exact shape of the agent.env-era idempotence defect. And the
    settings file is not the installer's file: every key it does not own rides
    through untouched."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text(json.dumps({
        "permissions": {"allow": ["mcp__reveille"]},
        "env": {"OTHER_VAR": "kept",
                "REVEILLE_URL": "http://old-bus:1",
                "REVEILLE_AGENT_ROLE": "stale-name",
                "REVEILLE_TOKEN": "revoked"}}))
    cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    cfg = json.loads((d / "settings.local.json").read_text())
    assert cfg["permissions"] == {"allow": ["mcp__reveille"]}, "dropped a key it does not own"
    assert cfg["env"]["OTHER_VAR"] == "kept"
    assert cfg["env"]["REVEILLE_URL"] == "http://b:8765"
    assert cfg["env"]["REVEILLE_AGENT_ROLE"] == "dev-agent"
    assert cfg["env"]["REVEILLE_TOKEN"] == "sekrit"


def test_a_correct_file_converges_byte_identical(tmp_path):
    cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    path = tmp_path / ".claude" / "settings.local.json"
    first = path.read_text()
    cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    assert path.read_text() == first


def test_a_file_that_cannot_be_parsed_is_refused_not_clobbered(tmp_path):
    """It holds settings this installer does not own; overwriting it on a parse
    error would trade a credential problem for a lost-configuration problem."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text("{not json")
    with pytest.raises(RuntimeError, match="not valid JSON"):
        cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    assert (d / "settings.local.json").read_text() == "{not json", \
        "a refusal must leave the file exactly as it found it"


def test_the_write_is_atomic_no_tmp_residue(tmp_path):
    cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    names = os.listdir(tmp_path / ".claude")
    assert names == ["settings.local.json"], names


# ---- the headers come from the directory ------------------------------------
# ${VAR} headers expand from the process env at connect time, BEFORE project
# settings env is injected -- the acceptance run measured the session's Bash
# seeing the identity while the MCP headers expanded empty. So identity rides
# a headersHelper reading the settings file, and these gates pin both halves.

def test_the_helper_reads_the_env_block(tmp_path):
    from reveille import headers
    cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    assert headers.gather(tmp_path) == {"Authorization": "Bearer sekrit",
                                        "X-Agent": "dev-agent"}


def test_a_directory_that_is_not_an_agent_yields_no_headers(tmp_path):
    """No file, malformed file, or HALF a credential all read as anonymous --
    half an identity authenticates as nobody and muddies the refusal."""
    from reveille import headers
    assert headers.gather(tmp_path) == {}
    d = tmp_path / ".claude"
    d.mkdir()
    (d / "settings.local.json").write_text("{not json")
    assert headers.gather(tmp_path) == {}
    (d / "settings.local.json").write_text(json.dumps(
        {"env": {"REVEILLE_TOKEN": "sekrit"}}))     # token without a name
    assert headers.gather(tmp_path) == {}


def test_the_registration_is_local_scope_and_leaves_nothing_in_the_tree(tmp_path):
    """Architect 12167: nothing per-agent is tracked. Project scope means a
    .mcp.json FILE IN THE REPO -- no secret in it, but still per-agent config in
    a shared tree that two people's agents collide over. Local scope keys the
    same registration to this project path in ~/.claude.json."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(cli.subprocess, "run", fake_run):
        where = cli.register_mcp_local("http://b:8765", tmp_path, "claude")
    assert "local" in where
    assert not (tmp_path / ".mcp.json").exists(), "nothing lands in the tree"
    cmd = calls[-1]
    assert cmd[:5] == ["claude", "mcp", "add-json", "--scope", "local"]
    spec = json.loads(cmd[-1])
    assert spec == {"type": "http", "url": "http://b:8765/mcp",
                    "headersHelper": "reveille-headers"}


def test_a_failed_registration_is_raised_not_swallowed(tmp_path):
    def fake_run(cmd, **kw):
        return types.SimpleNamespace(returncode=1, stdout="", stderr="no such flag")

    with mock.patch.object(cli.subprocess, "run", fake_run):
        with pytest.raises(RuntimeError, match="add-json"):
            cli.register_mcp_local("http://b:8765", tmp_path, "claude")


def test_an_earlier_project_scope_entry_is_migrated_away(tmp_path):
    """Two registrations for one server is how a body authenticates twice by
    different rules. Every OTHER server in that file is somebody else's."""
    (tmp_path / ".mcp.json").write_text(json.dumps({
        "mcpServers": {"other": {"type": "stdio", "command": "kept"},
                       "reveille": {"type": "http", "url": "http://old/mcp"}}}))
    cli.drop_project_mcp_entry(tmp_path)
    cfg = json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]
    assert cfg == {"other": {"type": "stdio", "command": "kept"}}, "theirs survives"


def test_a_file_left_holding_nothing_is_removed(tmp_path):
    """An empty config in a tracked tree is litter that outlives its reason."""
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "reveille": {"type": "http", "url": "http://old/mcp"}}}))
    cli.drop_project_mcp_entry(tmp_path)
    assert not (tmp_path / ".mcp.json").exists()


def test_a_tracked_mcp_json_is_emptied_and_left_for_its_owner_to_delete(tmp_path):
    """Removing OUR entry is correctness. Deleting a file git is watching is a
    staged deletion the person did not ask for and may not notice until a commit
    takes it -- so the file stays and the removal is their commit."""
    import subprocess
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {
        "reveille": {"type": "http", "url": "http://old/mcp"}}}))
    for argv in (["git", "init", "-q"], ["git", "add", ".mcp.json"]):
        subprocess.run(argv, cwd=tmp_path, capture_output=True, check=True)
    cli.drop_project_mcp_entry(tmp_path)
    assert (tmp_path / ".mcp.json").exists(), "not ours to delete"
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"] == {}


def test_a_mcp_json_we_cannot_parse_is_left_alone(tmp_path):
    """It may name servers this installer does not own -- refusing to touch it
    is the same discipline the write path had."""
    (tmp_path / ".mcp.json").write_text("{not json")
    assert cli.drop_project_mcp_entry(tmp_path) is None
    assert (tmp_path / ".mcp.json").read_text() == "{not json"


def test_the_credential_cannot_be_committed_from_its_own_directory(tmp_path):
    """The credential is written INTO a git working tree, and whether it was
    ignored used to be a property of the person's own machine -- a personal
    global ignore covered it on one laptop and nowhere else. A fresh clone left
    a live agent token untracked-but-not-ignored, one `git add -A` from a public
    repo. The fix stays inside what we own: a .gitignore in the .claude
    directory this installer creates -- never the repo's own .gitignore, and
    never .git/info/exclude, which is the user's git config."""
    path, wrote = cli.ignore_the_credential(tmp_path)
    assert wrote and path == tmp_path / ".claude" / ".gitignore"
    assert "settings.local.json" in path.read_text()
    # Idempotent, and it never duplicates the line.
    path2, wrote2 = cli.ignore_the_credential(tmp_path)
    assert wrote2 is False and path2.read_text().count("settings.local.json") == 1


def test_an_existing_claude_gitignore_is_appended_to_not_replaced(tmp_path):
    d = tmp_path / ".claude"
    d.mkdir()
    (d / ".gitignore").write_text("something-else\n")
    path, wrote = cli.ignore_the_credential(tmp_path)
    text = path.read_text()
    assert wrote and "something-else" in text and "settings.local.json" in text
    # BOTH secrets this directory can hold (architect blocking on #151): waked
    # writes the spent credential to .reveille-parked beside the live one, and
    # an ignore file naming one of two secrets is a published-identity hole.
    assert ".reveille-parked" in text.split()


def test_a_dir_init_already_touched_still_gains_the_parked_line(tmp_path):
    """The early return `if "settings.local.json" in text.split(): return` was
    a permanent ceiling wearing a check's face: no directory init had ever
    touched could gain a line, so fixing the wanted set alone would have fixed
    new dirs and zero existing ones."""
    d = tmp_path / ".claude"
    d.mkdir()
    (d / ".gitignore").write_text("settings.local.json\n")   # an old init's file
    path, wrote = cli.ignore_the_credential(tmp_path)
    text = path.read_text()
    assert wrote and ".reveille-parked" in text.split()
    assert text.split().count("settings.local.json") == 1
    # And still idempotent once complete.
    _, wrote2 = cli.ignore_the_credential(tmp_path)
    assert wrote2 is False


def test_the_helper_the_registration_names_is_shipped(tmp_path):
    """A doc that prescribes a command must be true as written, and .mcp.json
    is a doc the next session executes: the helper it names must be a console
    script this package ships."""
    import pathlib
    import tomllib
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with mock.patch.object(cli.subprocess, "run", fake_run):
        cli.register_mcp_local("http://b:8765", tmp_path, "claude")
    helper = json.loads(calls[-1][-1])["headersHelper"]
    scripts = tomllib.loads(
        (pathlib.Path(cli.__file__).parents[2] / "pyproject.toml")
        .read_text())["project"]["scripts"]
    assert helper in scripts, (
        f".mcp.json names {helper!r}; [project.scripts] ships {sorted(scripts)}")


def test_a_committable_doctrine_is_warned_about_not_silently_ignored(tmp_path):
    """The honest half of the ignore story. `.claude/.gitignore` covers the
    CREDENTIAL because that file lives in a directory this installer creates.
    CLAUDE.local.md lives at the project root, where a .gitignore of ours cannot
    reach it -- only the repo's own .gitignore (the project's, not ours) or
    .git/info/exclude (the user's git config) could, and neither is ours to
    write. So init says so rather than pretending it is covered."""
    import subprocess
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    target = tmp_path / "CLAUDE.local.md"
    target.write_text("x")
    warn = cli.warn_if_committable(tmp_path, target)
    assert "can be committed" in warn and "CLAUDE.local.md" in warn

    # Ignored -> silent. Nothing to say when nothing is at risk.
    (tmp_path / ".gitignore").write_text("CLAUDE.local.md\n")
    assert cli.warn_if_committable(tmp_path, target) == ""


def test_outside_a_git_work_tree_there_is_nothing_to_warn_about(tmp_path):
    target = tmp_path / "CLAUDE.local.md"
    target.write_text("x")
    assert cli.warn_if_committable(tmp_path, target) == ""
