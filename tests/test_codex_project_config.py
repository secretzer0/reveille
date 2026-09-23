"""Project configuration and secrets stay separate, including on repair."""
import json
import shlex
import subprocess
import tomllib

import pytest

from reveille.adapters import AdapterError, get_adapter
from reveille.headers import gather


@pytest.fixture
def adapter():
    return get_adapter("codex")


def test_registration_preserves_comments_policy_and_other_servers(tmp_path, adapter):
    project = tmp_path / "project with 'quotes'"
    config = project / ".codex/config.toml"
    config.parent.mkdir(parents=True)
    config.write_text('''# My settings
model = "example" # Keep this
[mcp_servers.other]
url = "https://other.test/mcp"
[mcp_servers.reveille]
# Keep tool policy
tool_timeout_sec = 120
disabled_tools = ["send"]
command = "old-proxy"
args = ["--legacy"]
bearer_token_env_var = "OLD_TOKEN"
http_headers = { Authorization = "old-secret" }
''')
    adapter.register_mcp(project, "https://broker.test", "codex")
    rendered = config.read_text()
    data = tomllib.loads(rendered)
    spec = data["mcp_servers"]["reveille"]
    assert "# My settings" in rendered and "# Keep this" in rendered
    assert "# Keep tool policy" in rendered
    assert data["mcp_servers"]["other"]["url"] == "https://other.test/mcp"
    assert spec["disabled_tools"] == ["send"] and spec["tool_timeout_sec"] == 120
    assert not {"command", "args", "bearer_token_env_var", "http_headers"} & spec.keys()
    # NO PATH IN THE COMMAND: measured 2026-09-23, Codex runs the helper with
    # the session's own cwd, so the helper finds the project by walking up and
    # this file stays portable and free of a home directory.
    assert shlex.split(spec["http_headers_helper"]) == ["reveille-headers", "--runtime", "codex"]
    assert str(project) not in spec["http_headers_helper"]
    assert adapter.mcp_registered(project, "https://broker.test")
    before = config.stat().st_mtime_ns
    adapter.register_mcp(project, "https://broker.test", "codex")
    assert config.stat().st_mtime_ns == before
    assert config.read_text() == rendered


@pytest.mark.parametrize("text", ["broken = [", "mcp_servers = 2", '[mcp_servers]\nreveille = 2'])
def test_invalid_configuration_is_not_clobbered(tmp_path, adapter, text):
    config = tmp_path / ".codex/config.toml"
    config.parent.mkdir()
    config.write_text(text)
    with pytest.raises(AdapterError):
        adapter.register_mcp(tmp_path, "https://broker.test", "codex")
    assert config.read_text() == text
    assert not adapter.mcp_registered(tmp_path, "https://broker.test")


def test_rotation_is_private_ignored_and_visible_to_header_helper(tmp_path, adapter):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = adapter.write_credential(tmp_path, "https://broker.test", "alice", "first")
    data = json.loads(path.read_text())
    data["local_note"] = "preserve"
    path.write_text(json.dumps(data))
    path.chmod(0o644)
    adapter.write_credential(tmp_path, "https://broker.test", "alice", "second")
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text())["local_note"] == "preserve"
    assert gather(tmp_path, "codex")["Authorization"] == "Bearer second"
    assert subprocess.run(["git", "-C", str(tmp_path), "check-ignore", "-q", str(path)]).returncode == 0
    assert not list(path.parent.glob(".reveille-????????"))


def test_tracked_credential_refuses_before_any_write(tmp_path, adapter):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / ".codex/reveille.json"
    path.parent.mkdir()
    path.write_text("{}")
    subprocess.run(["git", "-C", str(tmp_path), "add", str(path)], check=True)
    with pytest.raises(AdapterError, match="tracked"):
        adapter.write_credential(tmp_path, "https://broker.test", "alice", "secret")
    assert path.read_text() == "{}"
    assert not (path.parent / ".gitignore").exists()


def test_symlinked_config_directory_refuses(tmp_path, adapter):
    other = tmp_path / "other"
    other.mkdir()
    (tmp_path / ".codex").symlink_to(other, target_is_directory=True)
    with pytest.raises(AdapterError, match="symlink"):
        adapter.register_mcp(tmp_path, "https://broker.test", "codex")
    with pytest.raises(AdapterError, match="symlink"):
        adapter.write_credential(tmp_path, "https://broker.test", "alice", "secret")
    assert list(other.iterdir()) == []


def test_bad_credential_is_preserved_without_echoing_secret(tmp_path, adapter):
    path = tmp_path / ".codex/reveille.json"
    path.parent.mkdir()
    path.write_text('{"secret":"existing-secret", broken')
    with pytest.raises(AdapterError) as error:
        adapter.write_credential(tmp_path, "https://broker.test", "alice", "new-secret")
    assert "secret" not in str(error.value)
    assert "existing-secret" in path.read_text()


def test_credential_never_lands_in_mcp_config(tmp_path, adapter):
    adapter.register_mcp(tmp_path, "https://broker.test", "codex")
    adapter.write_credential(tmp_path, "https://broker.test", "alice", "private-token")
    text = (tmp_path / ".codex/config.toml").read_text()
    assert "private-token" not in text and "alice" not in text
    assert not adapter.mcp_registered(tmp_path, "https://another.test")


def test_a_codex_project_installs_every_local_artifact(tmp_path, monkeypatch, capsys):
    """THE WHOLE INSTALL, on the runtime that has no `claude` binary anywhere.

    Every artifact a Codex agent needs is local: the registration Codex reads
    (in a project it trusts), the credential its headers helper reads, the
    ignore that keeps that credential out of the repo, the shared instruction
    block, and the runtime selection that makes the choice durable. Nothing
    here shells out to a CLI, because for Codex nothing has to.
    """
    import http.server
    import threading
    from reveille import cli

    class Broker(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"rooms": {}}')

        def log_message(self, *args):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), Broker)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    try:
        home = tmp_path / "home"
        work = tmp_path / "work"
        work.mkdir()
        monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
        monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
        monkeypatch.setenv("REVEILLE_SPOOL", str(tmp_path / "spool"))
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        rc = cli.main(["init", url, "codex-agent", "-", "--runtime", "codex",
                       "--dir", str(work), "--trust-project", "--no-prompt"])
    finally:
        srv.shutdown()
    said = capsys.readouterr()
    assert rc == 0, said.err

    config = tomllib.loads((work / ".codex" / "config.toml").read_text())
    assert config["mcp_servers"]["reveille"]["url"] == url + "/mcp"
    assert config["mcp_servers"]["reveille"]["enabled"] is True
    credential = json.loads((work / ".codex" / "reveille.json").read_text())
    assert credential["env"]["REVEILLE_AGENT_ROLE"] == "codex-agent"
    assert credential["env"]["REVEILLE_TOKEN"] == "sekrit"
    assert (work / ".codex" / "reveille.json").stat().st_mode & 0o777 == 0o600
    assert "/reveille.json" in (work / ".codex" / ".gitignore").read_text()
    assert json.loads((work / ".reveille" / "runtime.json").read_text())["runtime"] == "codex"

    agents = (work / "AGENTS.md").read_text()
    assert "reveille:begin" in agents and "codex-agent" not in agents
    assert not (work / "CLAUDE.local.md").exists()
    # No hook was installed, and the install says so rather than claiming one.
    assert "no hook installed" in said.out
    assert not (work / ".codex" / "hooks.json").exists()


def test_an_untrusted_codex_project_installs_nothing(tmp_path, monkeypatch, capsys):
    """The refusal lands BEFORE any file: a registration Codex will not read is
    worse than no registration, because it reads as configured."""
    from reveille import cli
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "home" / ".codex"))
    monkeypatch.setenv("REVEILLE_TOKEN", "sekrit")
    work = tmp_path / "work"
    work.mkdir()
    assert cli.main(["init", "http://127.0.0.1:1", "codex-agent", "-",
                     "--runtime", "codex", "--dir", str(work), "--no-prompt"]) == 1
    assert "does not trust" in capsys.readouterr().err
    assert list(work.iterdir()) == []
