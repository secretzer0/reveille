"""Runtime integrations. No broker, process or credential mutations at import time.

Adapters own CLI-specific paths and operations. The caller owns provisioning
order, identity validation, durable spooling and error reporting.
"""
from abc import ABC, abstractmethod
from pathlib import Path
import json
import os
import tempfile


class AdapterError(RuntimeError):
    """A runtime cannot safely perform the requested operation."""


class RuntimeAdapter(ABC):
    name: str

    def validate_install(self, project: Path) -> None:
        """Local preflight, before login, minting or configuration writes."""

    @abstractmethod
    def credential_path(self, project: Path) -> Path: ...

    def identity(self, project: Path) -> dict[str, str]:
        """Read only a complete project identity; never borrow another session's env."""
        try:
            data = json.loads(self.credential_path(Path(project)).read_text())
            env = data.get("env", {})
            keys = ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN")
            if not isinstance(env, dict):
                return {}
            if not all(isinstance(env.get(k), str) and env[k].strip() for k in keys[1:]):
                return {}
            return {k: env.get(k, "").strip() for k in keys
                    if isinstance(env.get(k, ""), str)}
        except (OSError, ValueError, AttributeError):
            return {}

    @abstractmethod
    def instruction_path(self, project: Path) -> Path: ...

    @abstractmethod
    def register_mcp(self, project: Path, url: str, executable: str) -> str: ...

    @abstractmethod
    def install_hooks(self, project: Path) -> None: ...

    @abstractmethod
    def deliver(self, project: Path, agent: str, frame: dict) -> tuple[int, str]: ...


class ClaudeAdapter(RuntimeAdapter):
    name = "claude"

    def credential_path(self, project):
        return Path(project) / ".claude" / "settings.local.json"

    def instruction_path(self, project):
        return Path(project) / "CLAUDE.local.md"

    def register_mcp(self, project, url, executable):
        from reveille.cli import register_mcp_local
        return register_mcp_local(url, project, executable)

    def install_hooks(self, project):
        from reveille import install
        if install.main():
            raise AdapterError("Claude Stop hook installation failed")

    def deliver(self, project, agent, frame):
        from reveille import doorbell
        return doorbell.knock(agent, str(project), frame)


class CodexAdapter(RuntimeAdapter):
    name = "codex"

    def validate_install(self, project):
        raise AdapterError("Codex project provisioning is not implemented yet")

    def credential_path(self, project):
        return Path(project) / ".codex" / "reveille.json"

    def instruction_path(self, project):
        project = Path(project)
        override = project / "AGENTS.override.md"
        # Codex loads only one file per directory. Never create an override
        # that would hide the project's existing instructions.
        if override.exists() and override.read_text().strip():
            return override
        return project / "AGENTS.md"

    def register_mcp(self, project, url, executable):
        raise AdapterError("Codex project provisioning is not implemented yet")

    def install_hooks(self, project):
        raise AdapterError("Codex trusted lifecycle hooks are not implemented yet")

    def deliver(self, project, agent, frame):
        return 0, "Codex app-server delivery is not implemented yet; ring remains spooled"


ADAPTERS = {adapter.name: adapter for adapter in (ClaudeAdapter(), CodexAdapter())}


def get_adapter(name: str) -> RuntimeAdapter:
    try:
        return ADAPTERS[name]
    except KeyError:
        raise AdapterError(f"Unknown runtime {name!r}; choose {', '.join(ADAPTERS)}") from None


def select_adapter(project, runtime="auto") -> RuntimeAdapter:
    """Resolve an explicit choice or existing credentials, never PATH ordering.

    Existing Claude projects retain their behavior. A fresh or ambiguous
    project requires an explicit choice; merely installing a CLI proves no
    intent to give it this project's identity.
    """
    if runtime != "auto":
        return get_adapter(runtime)
    saved = saved_runtime(project)
    if saved:
        return get_adapter(saved)
    found = [a for a in ADAPTERS.values() if a.credential_path(Path(project)).exists()]
    if len(found) == 1:
        return found[0]
    reason = "multiple runtime credentials" if found else "no runtime credentials"
    raise AdapterError(f"{reason} in {project}; select --runtime explicitly")


def runtime_path(project):
    return Path(project) / ".reveille" / "runtime.json"


def saved_runtime(project):
    """Malformed metadata must not silently switch a project's runtime."""
    path = runtime_path(project)
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise AdapterError(f"Cannot read runtime selection at {path}: {e}") from e
    if not isinstance(data, dict) or data.get("version") != 1:
        raise AdapterError(f"Invalid runtime selection at {path}")
    name = data.get("runtime")
    if not isinstance(name, str):
        raise AdapterError(f"Invalid runtime selection at {path}")
    return get_adapter(name).name


def save_runtime(project, runtime):
    """Atomically persist only the runtime choice, never identity or secrets."""
    name = get_adapter(runtime).name
    path = runtime_path(project)
    previous = saved_runtime(project)
    if previous == name:
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    ignore = path.parent / ".gitignore"
    text = ignore.read_text() if ignore.exists() else ""
    if "/runtime.json" not in text.splitlines():
        ignore.write_text(text + ("\n" if text and not text.endswith("\n") else "")
                          + "/runtime.json\n")
    fd, tmp = tempfile.mkstemp(prefix=".runtime-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"version": 1, "runtime": name}, f)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path


def project_claims(project):
    """Inspect every runtime before replacing an identity, even on explicit selection."""
    claims = {}
    for adapter in ADAPTERS.values():
        path = adapter.credential_path(Path(project))
        try:
            data = json.loads(path.read_text())
            env = data.get("env", {})
            name = env.get("REVEILLE_AGENT_ROLE", "")
        except FileNotFoundError:
            continue
        except (OSError, ValueError, AttributeError) as e:
            raise AdapterError(f"Cannot inspect project identity at {path}") from e
        if not isinstance(name, str):
            raise AdapterError(f"Invalid project identity at {path}")
        if name:
            claims[adapter.name] = name
    return claims
