"""Runtime integrations. No broker, process or credential mutations at import time.

Adapters own CLI-specific paths and operations. The caller owns provisioning
order, identity validation, durable spooling and error reporting.
"""
from abc import ABC, abstractmethod
from pathlib import Path
import json


class AdapterError(RuntimeError):
    """A runtime cannot safely perform the requested operation."""


class RuntimeAdapter(ABC):
    name: str

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
    found = [a for a in ADAPTERS.values() if a.credential_path(Path(project)).exists()]
    if len(found) == 1:
        return found[0]
    reason = "multiple runtime credentials" if found else "no runtime credentials"
    raise AdapterError(f"{reason} in {project}; select --runtime explicitly")
