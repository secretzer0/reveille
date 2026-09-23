"""Runtime selection must not leak one project's identity into another CLI."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reveille.adapters import AdapterError, get_adapter, select_adapter
from reveille.headers import gather, main


def credential(project, runtime, name):
    path = get_adapter(runtime).credential_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"env": {
        "REVEILLE_URL": "https://example.test", "REVEILLE_AGENT_ROLE": name,
        "REVEILLE_TOKEN": f"test-{name}",
    }}))
    return path


def test_two_projects_keep_separate_identities(tmp_path):
    claude, codex = tmp_path / "one", tmp_path / "two"
    credential(claude, "claude", "alice")
    credential(codex, "codex", "bob")
    assert select_adapter(claude).name == "claude"
    assert select_adapter(codex).name == "codex"
    assert gather(claude)["Authorization"] == "Bearer test-alice"
    assert gather(codex)["Authorization"] == "Bearer test-bob"
    assert get_adapter("codex").identity(codex)["REVEILLE_AGENT_ROLE"] == "bob"


def test_ambiguous_project_fails_closed(tmp_path):
    credential(tmp_path, "claude", "alice")
    credential(tmp_path, "codex", "bob")
    with pytest.raises(AdapterError, match="multiple runtime"):
        select_adapter(tmp_path)
    assert gather(tmp_path) == {}
    assert gather(tmp_path, "codex")["X-Agent"] == "bob"


def test_missing_project_never_inherits_shell_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("REVEILLE_TOKEN", "unrelated-secret")
    monkeypatch.setenv("REVEILLE_AGENT_ROLE", "unrelated-agent")
    assert gather(tmp_path) == {}
    with pytest.raises(AdapterError, match="no runtime"):
        select_adapter(tmp_path)


@pytest.mark.parametrize("contents", ["bad json", "[]", '{"env": []}', '{"env": {}}'])
def test_malformed_identity_returns_no_headers(tmp_path, contents):
    path = credential(tmp_path, "codex", "bob")
    path.write_text(contents)
    assert gather(tmp_path) == {}
    assert get_adapter("codex").identity(tmp_path) == {}


def test_helper_explicit_project_does_not_depend_on_cwd(tmp_path, monkeypatch, capsys):
    credential(tmp_path / "project", "codex", "bob")
    monkeypatch.chdir(tmp_path)
    assert main(["--runtime", "codex", "--project", str(tmp_path / "project")]) == 0
    assert json.loads(capsys.readouterr().out)["X-Agent"] == "bob"


def test_codex_instructions_preserve_discovery_semantics(tmp_path):
    adapter = get_adapter("codex")
    normal = tmp_path / "AGENTS.md"
    normal.write_text("Project rules")
    assert adapter.instruction_path(tmp_path) == normal
    override = tmp_path / "AGENTS.override.md"
    override.write_text("")
    assert adapter.instruction_path(tmp_path) == normal
    override.write_text("Override rules")
    assert adapter.instruction_path(tmp_path) == override
    assert normal.read_text() == "Project rules"


def test_unimplemented_codex_cannot_claim_install_or_delivery(tmp_path):
    adapter = get_adapter("codex")
    with pytest.raises(AdapterError, match="not implemented"):
        adapter.register_mcp(tmp_path, "https://example.test", "codex")
    with pytest.raises(AdapterError, match="not implemented"):
        adapter.install_hooks(tmp_path)
    count, reason = adapter.deliver(tmp_path, "bob", {})
    assert count == 0 and "spooled" in reason


def test_claude_adapter_preserves_existing_transport(tmp_path, monkeypatch):
    from reveille import doorbell
    calls = []
    def knock(agent, project, frame):
        calls.append((agent, project, frame))
        return 1, ""
    monkeypatch.setattr(doorbell, "knock", knock)
    assert get_adapter("claude").deliver(tmp_path, "alice", {"id": 4}) == (1, "")
    assert calls == [("alice", str(tmp_path), {"id": 4})]
