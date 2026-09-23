"""Codex project configuration, independent of lifecycle and broker access."""
from collections.abc import MutableMapping
import json
import os
from pathlib import Path
import shlex
from urllib.parse import urlsplit

import tomlkit

from . import AdapterError
from .files import atomic_write, ignore_files, local_path, refuse_tracked


def load_config(project):
    path = local_path(project, ".codex/config.toml")
    try:
        doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    except (OSError, ValueError) as e:
        raise AdapterError(f"Cannot read Codex configuration at {path}") from e
    return path, doc


def codex_home():
    """Codex's own home, which CODEX_HOME moves (agents-md doc, verified live)."""
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")


def helper_command(project):
    """The headers command, carrying NO path.

    MEASURED 2026-09-23 against codex-cli 0.156.1: the helper runs with the
    SESSION's cwd (`cwd=/home/.../reveille`) and no CODEX_HOME in its
    environment, so it can find the agent directory by walking up from there.
    An absolute path written here would be correct on exactly one machine,
    would break the moment the checkout moved, and would put a home directory
    into a file a team can commit -- for a fact the process can read off its
    own cwd.
    """
    return shlex.join(["reveille-headers", "--runtime", "codex"])


def trusted(project):
    """Codex loads a project's `.codex/` layer ONLY for a trusted project.

    Config reference, `projects.<path>.trust_level`: "Untrusted projects skip
    project-scoped `.codex/` layers, including project-local config, hooks, and
    rules." So a registration written into an untrusted project is a file
    nothing reads -- installed, and not reachable. The entry lives in Codex's
    OWN home config and is keyed by the project's absolute path; Codex writes
    it itself when a human trusts the folder in the TUI.
    """
    path = codex_home() / "config.toml"
    try:
        doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    except (OSError, ValueError) as e:
        raise AdapterError(f"Cannot read Codex configuration at {path}") from e
    entry = (doc.get("projects") or {}).get(str(Path(project).resolve()), {})
    try:
        return entry.get("trust_level") == "trusted"
    except AttributeError:
        raise AdapterError(f"Invalid projects entry for {project} in {path}") from None


def trust(project):
    """Mark the project trusted in Codex's home config. The human's decision.

    Trust is what lets this project's `.codex/` run HOOKS, so it is never
    granted as a side effect of installing: `reveille init` refuses and names
    this, the wizard asks, and --trust-project is the unattended yes.
    """
    path = codex_home() / "config.toml"
    try:
        doc = tomlkit.parse(path.read_text()) if path.exists() else tomlkit.document()
    except (OSError, ValueError) as e:
        raise AdapterError(f"Cannot read Codex configuration at {path}") from e
    projects = doc.setdefault("projects", tomlkit.table())
    if not isinstance(projects, MutableMapping):
        raise AdapterError(f"Codex projects must be a table in {path}")
    entry = projects.setdefault(str(Path(project).resolve()), tomlkit.table())
    if not isinstance(entry, MutableMapping):
        raise AdapterError(f"Invalid projects entry for {project} in {path}")
    entry["trust_level"] = "trusted"
    rendered = tomlkit.dumps(doc)
    tomlkit.parse(rendered)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(path, rendered,
                 mode=path.stat().st_mode & 0o777 if path.exists() else 0o600)
    return str(path)


def mcp_spec(project, url):
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise AdapterError("Broker URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AdapterError("Broker URL cannot contain credentials, query or fragment")
    return {"url": url.rstrip("/") + "/mcp", "http_headers_helper": helper_command(project)}


def register(project, url):
    spec = mcp_spec(project, url)
    path, doc = load_config(project)
    servers = doc.setdefault("mcp_servers", tomlkit.table())
    if not isinstance(servers, MutableMapping):
        raise AdapterError("Codex mcp_servers must be a table")
    server = servers.setdefault("reveille", tomlkit.table())
    if not isinstance(server, MutableMapping):
        raise AdapterError("Codex mcp_servers.reveille must be a table")
    # Transport/authentication is owned by this installer. Retain tool policy,
    # timeouts, other servers and unrelated comments, but remove competing
    # credentials that would override the project helper.
    for key in ("command", "args", "cwd", "env", "env_vars", "experimental_environment",
                "bearer_token_env_var", "http_headers", "env_http_headers", "auth",
                "oauth", "oauth_resource"):
        server.pop(key, None)
    for key, value in spec.items():
        server[key] = value
    server["enabled"] = True
    rendered = tomlkit.dumps(doc)
    tomlkit.parse(rendered)  # Validate the final document before replacing it.
    atomic_write(path, rendered, mode=path.stat().st_mode & 0o777 if path.exists() else 0o600)
    return str(path)


def registered(project, url):
    try:
        _, doc = load_config(project)
        server = doc.get("mcp_servers", {}).get("reveille", {})
        return (all(server.get(k) == v for k, v in mcp_spec(project, url).items())
                and server.get("enabled", True) is True
                and not any(k in server for k in ("command", "bearer_token_env_var",
                                                   "http_headers", "env_http_headers", "oauth")))
    except (AdapterError, AttributeError, ValueError):
        return False


def write_credential(project, url, name, token):
    if not all(isinstance(value, str) and value.strip() for value in (url, name, token)):
        raise AdapterError("A complete broker URL, agent name and token are required")
    mcp_spec(project, url)
    path = local_path(project, ".codex/reveille.json")
    refuse_tracked(project, path)
    try:
        data = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as e:
        raise AdapterError(f"Cannot read existing credential at {path}") from e
    if not isinstance(data, dict) or not isinstance(data.get("env", {}), dict):
        raise AdapterError(f"Invalid credential object at {path}")
    env = data.setdefault("env", {})
    env.update(REVEILLE_URL=url, REVEILLE_AGENT_ROLE=name, REVEILLE_TOKEN=token)
    # Ignore before creating even a temporary secret. Temporary files are 0600
    # as well, and must not be captured by a concurrent git add.
    ignore_files(project, ".codex", ["reveille.json", ".reveille-parked", ".reveille-*"])
    return atomic_write(path, json.dumps(data, indent=2) + "\n")
