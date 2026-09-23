"""Runtime selection must not leak one project's identity into another CLI."""
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from reveille.adapters import AdapterError, get_adapter, select_adapter
from reveille.headers import gather, main
from reveille.adapters import save_runtime, saved_runtime, runtime_path, project_claims


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
        adapter.validate_install(tmp_path)
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


def test_saved_selection_resolves_ambiguity_and_is_idempotent(tmp_path):
    credential(tmp_path, "claude", "alice")
    credential(tmp_path, "codex", "bob")
    path = save_runtime(tmp_path, "codex")
    before = path.stat().st_mtime_ns
    assert select_adapter(tmp_path).name == "codex"
    assert gather(tmp_path)["X-Agent"] == "bob"
    assert select_adapter(tmp_path, "claude").name == "claude"
    assert saved_runtime(tmp_path) == "codex"
    assert save_runtime(tmp_path, "codex") == path
    assert path.stat().st_mtime_ns == before
    assert json.loads(path.read_text()) == {"version": 1, "runtime": "codex"}
    assert path.stat().st_mode & 0o777 == 0o600


def test_corrupt_selection_does_not_fall_back_to_claude(tmp_path):
    credential(tmp_path, "claude", "alice")
    path = runtime_path(tmp_path)
    path.parent.mkdir()
    path.write_text("not json")
    with pytest.raises(AdapterError, match="Cannot read runtime"):
        select_adapter(tmp_path)
    assert gather(tmp_path) == {}
    with pytest.raises(AdapterError):
        save_runtime(tmp_path, "claude")
    assert path.read_text() == "not json"


def test_claims_inspect_unselected_runtime(tmp_path):
    credential(tmp_path, "claude", "alice")
    credential(tmp_path, "codex", "bob")
    save_runtime(tmp_path, "claude")
    assert project_claims(tmp_path) == {"claude": "alice", "codex": "bob"}


def test_init_codex_refuses_before_reading_token_or_contacting_broker(tmp_path, monkeypatch):
    from reveille import cli
    def unexpected(*args, **kwargs):
        pytest.fail("unsupported runtime reached credential or broker work")
    monkeypatch.setattr(cli, "read_token", unexpected)
    monkeypatch.setattr(cli, "verify", unexpected)
    assert cli.main(["init", "--runtime", "codex", "--dir", str(tmp_path),
                     "--no-prompt"]) == 1
    assert list(tmp_path.iterdir()) == []


def test_fresh_unattended_init_requires_runtime(tmp_path, monkeypatch, capsys):
    from reveille import cli
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/" + name)
    monkeypatch.setattr(cli, "read_token", lambda *args: pytest.fail("read token too early"))
    assert cli.main(["init", "--dir", str(tmp_path), "--no-prompt"]) == 1
    assert "--runtime" in capsys.readouterr().err


def test_sole_installed_runtime_reaches_adapter_preflight(tmp_path, monkeypatch, capsys):
    from reveille import cli
    monkeypatch.setattr(cli.shutil, "which", lambda name: "/bin/codex" if name == "codex" else None)
    assert cli.main(["init", "--dir", str(tmp_path), "--no-prompt"]) == 1
    assert "Codex project provisioning" in capsys.readouterr().err


def test_cross_runtime_identity_conflict_precedes_registration(tmp_path, monkeypatch, capsys):
    from reveille import cli
    credential(tmp_path, "codex", "bob")
    monkeypatch.setattr(cli, "read_token", lambda *args: "supplied-token")
    monkeypatch.delenv("REVEILLE_TOKEN", raising=False)
    monkeypatch.setattr(cli, "verify", lambda *args: pytest.fail("contacted broker"))
    assert cli.main(["init", "https://example.test", "alice", "--runtime", "claude",
                     "--dir", str(tmp_path), "--no-prompt"]) == 1
    assert "bob" in capsys.readouterr().err
    assert not runtime_path(tmp_path).exists()
