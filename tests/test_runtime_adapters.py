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


def test_codex_hooks_are_offered_and_never_silently_installed(tmp_path):
    """A Codex hook does nothing until a human trusts its HASH under /hooks, so
    an installer that wrote one and reported success would claim a gate that is
    not running."""
    said = get_adapter("codex").install_hooks(tmp_path)
    assert "no hook installed" in said and "/hooks" in said
    assert list(tmp_path.iterdir()) == []


def test_codex_delivery_without_an_app_server_leaves_the_ring_spooled(tmp_path, monkeypatch):
    """No socket is not a delivery: it is a reason, and the spool entry stands."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home"))
    count, reason = get_adapter("codex").deliver(tmp_path, "bob", {"reason": "mail"})
    assert count == 0 and "app-server socket" in reason
    assert get_adapter("codex").sessions(tmp_path) == []


def test_claude_adapter_rings_every_live_session_itself(tmp_path, monkeypatch):
    """Claude's transport is the ADAPTER's, not the doorbell's: the doorbell
    routes and refuses, the runtime speaks its own protocol."""
    from reveille import doorbell
    rung = []
    monkeypatch.setattr(doorbell, "inboxes_for",
                        lambda project: [(11, "/s1.sock", "t1"), (12, "/s2.sock", "t2")])
    monkeypatch.setattr(doorbell, "ring_one",
                        lambda sock, token, text: rung.append((sock, token, text)) or "")
    assert get_adapter("claude").deliver(tmp_path, "alice", {"reason": "mail"}) == (2, "")
    assert [r[0] for r in rung] == ["/s1.sock", "/s2.sock"]
    assert "reason=mail" in rung[0][2]


def test_claude_delivery_with_nobody_home_is_a_reason_not_a_delivery(tmp_path, monkeypatch):
    from reveille import doorbell
    monkeypatch.setattr(doorbell, "inboxes_for", lambda project: [])
    assert get_adapter("claude").deliver(tmp_path, "alice", {"reason": "mail"}) == (
        0, "no live session in that directory")


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


def test_claude_instruction_block_is_unchanged_by_the_generic_synchroniser(tmp_path):
    """The extraction may not move one byte of what `reveille init` writes."""
    from reveille import instructions
    adapter = get_adapter("claude")
    path, what = adapter.sync_instructions(tmp_path, "example-agent", "devops")
    assert (path, what) == (tmp_path / "CLAUDE.local.md", "created")
    assert path.read_text() == instructions.doctrine_block("example-agent", "devops")
    assert instructions.doctrine_body("example-agent", "devops") in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o644


def test_a_runtime_without_instructions_writes_none(tmp_path):
    """A runtime with no body of its own may never inherit another's (defect 7)."""
    with pytest.raises(AdapterError, match="not implemented"):
        get_adapter("codex").sync_instructions(tmp_path, "bob", "devops")
    assert list(tmp_path.iterdir()) == []


def test_the_managed_block_discipline_is_runtime_agnostic(tmp_path):
    """Every runtime repairs a tampered block and keeps every byte outside it."""
    from reveille.instructions import DOCTRINE_END, sync_managed_block
    path = tmp_path / "AGENTS.md"
    path.write_text("Project rules a human wrote.\n")
    path.chmod(0o640)
    _, what = sync_managed_block(path, "shared bus rules\n", "1.0")
    assert what == "appended"
    assert path.read_text().startswith("Project rules a human wrote.\n")
    assert path.stat().st_mode & 0o777 == 0o640, "a file we did not create keeps its mode"
    assert sync_managed_block(path, "shared bus rules\n", "1.0")[1] == "unchanged"
    assert sync_managed_block(path, "shared bus rules\n", "1.1")[1] == "updated"
    tampered = path.read_text().replace("shared bus rules", "somebody's edit")
    path.write_text(tampered)
    assert sync_managed_block(path, "shared bus rules\n", "1.1")[1] == "repaired"
    text = path.read_text()
    assert "somebody's edit" not in text
    assert text.startswith("Project rules a human wrote.\n")
    assert text.count(DOCTRINE_END) == 1


def test_a_malformed_marker_never_eats_the_surrounding_prose(tmp_path):
    """An end marker with no begin is somebody's text, not ours to rewrite."""
    from reveille.instructions import DOCTRINE_END, sync_managed_block
    path = tmp_path / "AGENTS.md"
    path.write_text(f"before\n{DOCTRINE_END}\nafter\n")
    _, what = sync_managed_block(path, "body\n", "1.0")
    assert what == "appended"
    text = path.read_text()
    assert text.startswith(f"before\n{DOCTRINE_END}\nafter\n")
    assert "body\n" in text
