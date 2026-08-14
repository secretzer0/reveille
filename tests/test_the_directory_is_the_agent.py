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
import os

import pytest

from reveille import cli


def test_a_fresh_directory_gets_the_env_block(tmp_path):
    path = cli.write_credential("http://b:8765", "dev-agent", "sekrit", tmp_path)
    assert path == tmp_path / ".claude" / "settings.local.json"
    cfg = json.loads(path.read_text())
    assert cfg["env"] == {"REVEILLE_URL": "http://b:8765",
                          "REVEILLE_AGENT_ROLE": "dev-agent",
                          "REVEILLE_TOKEN": "sekrit"}
    # this file IS the credential: another account reading it ends the boundary
    assert oct(path.stat().st_mode)[-3:] == "600"


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
