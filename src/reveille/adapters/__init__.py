"""Runtime integrations. No broker, process or credential mutations at import time.

Adapters own CLI-specific paths and operations. The caller owns provisioning
order, identity validation, durable spooling and error reporting.
"""
from abc import ABC, abstractmethod
from pathlib import Path
import json
import os
import shutil
import tempfile

from .. import __version__


class AdapterError(RuntimeError):
    """A runtime cannot safely perform the requested operation."""


class RuntimeAdapter(ABC):
    name: str

    def validate_install(self, project: Path) -> None:
        """Local preflight, before login, minting or configuration writes."""

    @abstractmethod
    def credential_path(self, project: Path) -> Path: ...

    @abstractmethod
    def write_credential(self, project: Path, url: str, name: str, token: str) -> Path: ...

    @abstractmethod
    def mcp_registered(self, project: Path, url: str) -> bool: ...

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

    def state_dir(self, project: Path) -> Path:
        """Where this runtime's per-agent files live in a project.

        The credential's own directory, which is also where the parked marker,
        the repo-status artifact and the ignore rule that covers them belong:
        `.claude/` for one runtime, `.codex/` for the other. One answer, so a
        daemon never spells either of them.
        """
        return self.credential_path(Path(project)).parent

    def credential_env(self, project: Path) -> dict:
        """The credential file's `env` object, or {} -- read loosely.

        Both runtimes keep the identity in the same shape, so this is read once
        here. Loose on purpose: callers that need a COMPLETE credential use
        identity(), and callers that need to know who a directory belongs to
        use claimed_agent(), which is true of half a credential too.
        """
        try:
            data = json.loads(self.credential_path(Path(project)).read_text())
            env = data.get("env")
            return env if isinstance(env, dict) else {}
        except (OSError, ValueError, AttributeError):
            return {}

    def credential_mtime(self, project: Path) -> int:
        """When this identity's credential last changed, or 0."""
        try:
            return self.credential_path(Path(project)).stat().st_mtime_ns
        except OSError:
            return 0

    def claimed_agent(self, project: Path) -> str:
        """The identity this directory CLAIMS, complete credential or not.

        A DIRECTORY NAME IS NOT AN IDENTITY (operator, 2026-09-20): the registry
        maps name -> path and paths get reused, so a ring asks the directory
        who it belongs to. This read is deliberately LOOSER than identity():
        half a credential still names an owner, and ringing a directory that
        says it belongs to somebody else is the failure being prevented --
        whether or not that somebody can currently authenticate.

        Both runtimes keep the same {"env": {...}} shape in their own file, so
        the claim is read once here rather than re-derived per runtime.
        """
        name = self.credential_env(project).get("REVEILLE_AGENT_ROLE", "")
        return name if isinstance(name, str) else ""

    @abstractmethod
    def instruction_path(self, project: Path) -> Path: ...

    def instructions_body(self, name: str, agent_type: str) -> str:
        """The managed text THIS runtime writes between its markers.

        Shared bus rules are one body; where a runtime loads them, and what it
        may claim about hooks, delivery or environment, is the runtime's own.
        A runtime that has none yet says so rather than writing another's.
        """
        raise AdapterError(f"{self.name} managed instructions are not implemented yet")

    def sync_instructions(self, project: Path, name: str, agent_type: str,
                          version: str = __version__) -> tuple[Path, str]:
        """Write or refresh the managed block in this runtime's instruction file.

        The body is the runtime's; the marker discipline is shared, so every
        runtime repairs a tampered block and preserves every byte outside it
        the same way. Returns (path, 'created' | 'updated' | 'repaired' |
        'appended' | 'unchanged').
        """
        from ..instructions import sync_managed_block
        return sync_managed_block(self.instruction_path(Path(project)),
                                  self.instructions_body(name, agent_type), version)

    def headers_command(self, project: Path) -> list:
        """The exact command this runtime's registration tells the CLI to run."""
        return ["reveille-headers"]

    def verify_headers(self, project: Path, name: str) -> str:
        """Run the headers command AS REGISTERED. "" when it answers `name`.

        A DEPLOY MUST VERIFY THE PATH THE CONSUMER USES. The registration names
        a console script by NAME, so what answers is whatever is first on PATH
        -- which is not necessarily the build that wrote the registration.
        Measured 2026-09-23: an older reveille on PATH answered `{}` for a
        Codex project, the session connected ANONYMOUSLY, and the only symptom
        was a refusal at the broker with nothing local saying why.

        `{}` is the correct answer for a directory that is not an agent, so the
        helper cannot refuse on its own behalf. This asks the question only the
        installer can: it just wrote this directory's credential, so the helper
        must now name THIS agent, and anything else is a reachability failure
        at install time rather than a mystery at first use.
        """
        import json as _json
        import subprocess
        command = self.headers_command(project)
        try:
            run = subprocess.run(command, cwd=str(project), capture_output=True,
                                 text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as e:
            return f"`{' '.join(command)}` could not run in this directory: {e}"
        if run.returncode != 0:
            return (f"`{' '.join(command)}` exited {run.returncode}: "
                    f"{(run.stderr or run.stdout).strip()[:200]}")
        try:
            said = _json.loads(run.stdout or "{}")
        except ValueError:
            return f"`{' '.join(command)}` did not print JSON: {run.stdout[:200]}"
        got = said.get("X-Agent", "") if isinstance(said, dict) else ""
        if got == name:
            return ""
        which = shutil.which(command[0]) or "not on PATH"
        return (f"`{' '.join(command)}` answered {got or 'no identity'} in this "
                f"directory, not {name!r}. The MCP registration names that "
                f"command, so a session here would connect as nobody and the "
                f"broker would refuse it. The one answering is {which} -- put "
                f"the reveille that wrote this credential first on PATH")

    @abstractmethod
    def register_mcp(self, project: Path, url: str, executable: str) -> str: ...

    @abstractmethod
    def install_hooks(self, project: Path) -> str: ...

    @abstractmethod
    def sessions(self, project: Path) -> list:
        """Every LIVE session in `project` a ring could start a turn in.

        Opaque handles: what a session IS differs per runtime (Claude publishes
        a descriptor per session, Codex holds threads in one app-server), and
        nothing outside the adapter may care which. The doorbell asks only
        whether the list is empty, so `no live session` means the same thing
        for both.
        """

    @abstractmethod
    def session_key(self, session) -> str:
        """What makes this session ONE session, for a census of arrivals."""

    @abstractmethod
    def conversation_id(self, session) -> str:
        """What makes this session the same CONVERSATION across a restart.

        A RESUME IS NOT A BODY WITHOUT MEMORY. The census rings `boot` when a
        session arrives so a new body goes and gets its digest; a resumed one
        already holds what it read at its real boot, so ringing it spends a
        turn and the whole digest on memory it has. The two runtimes keep that
        identity in different places -- Claude in the conversation behind a new
        pid, Codex in the thread id a session resumes -- and this is the one
        place that difference lives.
        """

    @abstractmethod
    def deliver(self, project: Path, agent: str, frame: dict) -> tuple[int, str]: ...


class ClaudeAdapter(RuntimeAdapter):
    name = "claude"

    def credential_path(self, project):
        return Path(project) / ".claude" / "settings.local.json"

    def write_credential(self, project, url, name, token):
        from reveille.cli import write_credential
        return write_credential(url, name, token, project)

    def mcp_registered(self, project, url):
        from reveille import doorbell
        return doorbell.mcp_enabled(project, expect_url=url)

    def instruction_path(self, project):
        """CLAUDE.local.md, NOT CLAUDE.md (architect 12167).

        Claude Code loads both at session start, but CLAUDE.md is the PROJECT's
        file -- tracked, shared, and written by whoever owns the repo. This
        block is PER-AGENT: it carries the agent's own name and role, so in a
        shared checkout two people's agents would overwrite each other's block
        in a tracked file and commit the fight. The .local.md variant is the
        documented per-developer, untracked home, which is exactly what a
        per-agent block is.
        """
        return Path(project) / "CLAUDE.local.md"

    def instructions_body(self, name, agent_type):
        from ..instructions import doctrine_body
        return doctrine_body(name, agent_type)

    def register_mcp(self, project, url, executable):
        from reveille.cli import register_mcp_local
        return register_mcp_local(url, project, executable)

    def install_hooks(self, project):
        from reveille import install
        if install.main():
            raise AdapterError("Claude Stop hook installation failed")
        return "Stop hook installed"

    def sessions(self, project):
        from reveille import doorbell
        return doorbell.inboxes_for(str(project))

    def session_key(self, session):
        return str(session[0])

    def conversation_id(self, session):
        from reveille import doorbell
        return doorbell.session_id(session[0])

    def deliver(self, project, agent, frame):
        """Write the ring on every live session's own inbox socket.

        ALL of them, not the newest: a duplicate ring costs one turn and a
        skipped one costs every ring.
        """
        from reveille import doorbell
        found = self.sessions(project)
        if not found:
            return 0, "no live session in that directory"
        text = doorbell.ring_text(frame)
        rung, why = 0, []
        for pid, sock_path, token in found:
            err = doorbell.ring_one(sock_path, token, text)
            if err:
                why.append(f"pid {pid}: {err}")
            else:
                rung += 1
        return rung, "" if rung else "; ".join(why)


class CodexAdapter(RuntimeAdapter):
    name = "codex"

    def validate_install(self, project):
        """The one Codex precondition an installer cannot supply for itself.

        Codex reads a project's `.codex/` layer ONLY for a project marked
        trusted in Codex's own home config -- config reference,
        `projects.<path>.trust_level`: "Untrusted projects skip project-scoped
        `.codex/` layers, including project-local config, hooks, and rules."
        Writing a registration into an untrusted project produces a file
        nothing reads: installed, and unreachable.

        Trust is also what lets that directory run HOOKS, so it is never taken
        as a side effect of installing. This refuses and names all three ways
        to give it.
        """
        from .codex_config import codex_home, trusted
        if trusted(project):
            return
        raise AdapterError(
            f"Codex does not trust {Path(project).resolve()}, so it would ignore "
            f"the .codex configuration this writes. Give trust in any one of "
            f"three ways: re-run with --trust-project, answer the wizard, or "
            f"open `codex` in that directory once and say yes -- it records the "
            f"same entry in {codex_home() / 'config.toml'}")

    def credential_path(self, project):
        return Path(project) / ".codex" / "reveille.json"

    def write_credential(self, project, url, name, token):
        """Write it, and RECORD WHERE IT WROTE (ruling 24286).

        A host-wide waked enumerates identities from ~/.reveille/agents, and a
        credential writer is the only act that knows an identity's directory.
        Claude's writer has always done this; Codex's did not, so a Codex agent
        was invisible to waked -- no census, no boot ring, no delivery, with
        every local artifact present and correct. The entry is a path, never a
        secret, and last init wins.
        """
        from reveille import spool
        from .codex_config import write_credential
        spool.register(name, str(Path(project)))
        return write_credential(project, url, name, token)

    def mcp_registered(self, project, url):
        from .codex_config import registered
        return registered(project, url)

    def instruction_path(self, project):
        project = Path(project)
        override = project / "AGENTS.override.md"
        # Codex loads only one file per directory. Never create an override
        # that would hide the project's existing instructions.
        if override.exists() and override.read_text().strip():
            return override
        return project / "AGENTS.md"

    def headers_command(self, project):
        import shlex
        from .codex_config import helper_command
        return shlex.split(helper_command(project))

    def register_mcp(self, project, url, executable):
        from .codex_config import register
        return register(project, url)

    def install_hooks(self, project):
        """Codex hooks are OFFERED, never installed silently.

        A non-managed Codex hook does nothing until a human reviews it under
        `/hooks`, and trust is recorded against the hook's HASH -- so every
        later edit silently un-trusts it again. An installer that wrote one and
        reported success would be claiming a gate that is not running. Nothing
        on this runtime's reachability path depends on a hook: the host waked
        censuses sessions through the app-server, which is why this can be an
        offer rather than a requirement.
        """
        return ("no hook installed -- Codex reachability runs through waked and "
                "the app-server. A Stop-hook gate is available but must be "
                "trusted by a human under `/hooks` before it runs")

    def instructions_body(self, name, agent_type):
        """The SHARED rules, with no identity in them.

        `name` and `agent_type` are accepted and deliberately NOT written:
        Codex's instruction file is the project's own AGENTS.md, which is
        tracked and shared, and a per-agent name in it is both a leak and a
        fight two agents in one checkout would commit. The identity is read
        off the bus instead -- see instructions.shared_body().
        """
        from ..instructions import shared_body
        return shared_body()

    def sessions(self, project):
        from .codex_app_server import sessions
        return sessions(project)

    def session_key(self, session):
        return str(session)

    def conversation_id(self, session):
        # A Codex thread id IS the conversation: resuming one keeps the id, so
        # the census recognises a returning body without asking anything else.
        return str(session)

    def deliver(self, project, agent, frame):
        from reveille import doorbell
        from .codex_app_server import ring
        return ring(project, doorbell.ring_text(frame))


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


def find_project(start=".", runtime="auto"):
    """The nearest directory at or above `start` that a runtime has claimed.

    A session does not always start at the agent's own root -- `codex --cd
    sub` and a shell one directory in both hand the headers helper nothing but
    that cwd. Measured 2026-09-23: Codex runs `http_headers_helper` with the
    SESSION's cwd and no CODEX_HOME in its environment, and Claude Code runs
    its headersHelper with the project directory. So the project is found the
    way git finds its root, by walking up -- which is also what keeps an
    absolute path OUT of the config file that names the helper, because a path
    written into a project file is wrong on every other machine and leaks a
    home directory into a shared repo.

    Returns `start` when nothing above it is an agent directory, so a caller
    that is simply not in one gets the same inert answer as before.
    """
    start = Path(start).resolve()
    names = [runtime] if runtime != "auto" else list(ADAPTERS)
    for directory in (start, *start.parents):
        if any(get_adapter(name).credential_path(directory).exists() for name in names):
            return directory
    return start


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
