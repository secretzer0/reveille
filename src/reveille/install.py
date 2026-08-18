#!/usr/bin/env python3
"""`reveille-install-hook`: put the wake-waiter Stop hook into ~/.claude/settings.json.

User scope, so it covers every session on this machine. Idempotent, and
idempotent means CONVERGING ON CORRECTNESS rather than detecting presence: a
settings file already naming a hook that would still run is left byte-identical,
while one naming a cache-archive path or a clone this machine does not have is
RE-POINTED. Reporting a wrong-but-present value as installed is what made
`reveille init` -- the obvious remedy -- confirm a broken machine instead of
repairing it, and what kept 0.2.77's durable-spelling fix away from every
machine that already had the defect (msg 9067).

THE COMMAND IT WRITES IS A NAME ON PATH, not a path into a clone. It used to be
an absolute path to scripts/agent-stop-hook, which works only on a machine that
has this repo checked out -- so a native agent installed with `uv tool install`
got a hook naming a file that was never there, and a hook that cannot run is
indistinguishable from an agent that is simply quiet.

The hook fails open for sessions with no $REVEILLE_AGENT_ROLE, so it stays inert
outside bus-connected agents.
"""
import json
import os
import pathlib
import shutil
import sys

# The installed console script, resolved on PATH at write time so the file names
# something that exists rather than something we hope exists. Falls back to the
# bare name for the case where the hook is installed before the PATH entry is
# visible to this process -- a login shell will find it.
def hook_command():
    """The DURABLE spelling of the hook command, never a cache path.

    which() found uv's cache-archive copy first -- uvx ran from it, so it led
    PATH -- and that path was written into settings.json: a hook that works
    until the next `uv cache prune`, then takes daemon supervision and watcher
    arming with it, silently (ruling 9052). Preference order: the ~/.local/bin
    entry uv tool install creates; any PATH hit that is not under a cache; the
    bare name, resolved at hook-run time by a login shell.
    """
    local = pathlib.Path.home() / ".local" / "bin" / "reveille-stop-hook"
    if local.is_file():
        return str(local)
    w = shutil.which("reveille-stop-hook")
    if w and "/.cache/" not in w:
        return w
    return "reveille-stop-hook"


HOOK = "reveille-stop-hook"


def is_durable(cmd):
    """Would this command still run after `uv cache prune` and a repo move?

    The question install.main() has to ask before leaving an existing entry
    alone, and the one it never asked: a hook that MATCHES the name we install
    is not the same thing as a hook that WORKS. Three shapes are known to be
    present-but-broken -- a uv cache-archive path (dies at the next prune,
    ruling 9052), an absolute path into a clone the machine no longer has (the
    packaged-hook defect), and any other absolute path whose file is gone.
    A bare name is durable by construction: a login shell resolves it at
    hook-run time, which is the whole reason hook_command() falls back to it.
    """
    if "/.cache/" in cmd:
        return False
    if "/" not in cmd:
        return True
    return pathlib.Path(cmd).is_file()


def settings_path():
    """~/.claude/settings.json, or CLAUDE_CONFIG_DIR when it is set. Read at CALL
    time rather than import time so a test HOME is honoured -- a module-level
    constant here made every gate write into the real home directory."""
    return pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR")
                        or (pathlib.Path.home() / ".claude")) / "settings.json"


MCP_ALLOW = "mcp__reveille"
# The upload CLI (ruling 11449): a file's bytes go over plain HTTP, never through
# the model's context, and the ONLY frictionless way to run that HTTP is a named
# binary the settings pre-approve -- a narrow Bash rule resolves before any
# permission prompt or auto-mode classifier. Prefix rule, word boundary: it
# allows `reveille-upload <anything>` and nothing else.
UPLOAD_ALLOW = "Bash(reveille-upload *)"
ALLOW = (MCP_ALLOW, UPLOAD_ALLOW)


def ensure_allow(settings):
    """The installer must GRANT what it REGISTERS. On the operator's Mac the
    first join() after a clean install was refused by permission policy: the
    machine had the MCP registration, the hook, the credential -- it LOOKED
    configured -- and the first real bus call still needed an approval nobody
    was there to give. The explicit allow rule is how a user pre-approves a
    tool in settings.json; its scope is the one server this installer itself
    registers plus the one binary it ships for files, no wider. Returns a step
    line, or None when every rule is already present."""
    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    added = [r for r in ALLOW if r not in allow]
    if not added:
        return None
    allow.extend(added)
    return f"permissions: {', '.join(added)} allowed (the bus tools work on first use)"


def main():
    SETTINGS = settings_path()
    hook = hook_command()
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    perm_line = ensure_allow(settings)
    wrote = perm_line is not None
    stops = settings.setdefault("hooks", {}).setdefault("Stop", [])
    hook_line = None
    for group in stops:
        for h in group.get("hooks", []):
            cmd = h.get("command", "")
            if not cmd.endswith(("agent-stop-hook", "reveille-stop-hook")):
                continue
            # CONVERGE ON CORRECTNESS, NEVER ON PRESENCE. This branch used to
            # return 0 the moment a command merely ENDED IN our name, so a
            # wrong-but-present value became permanent and re-running the
            # installer -- the obvious remedy, and the one the operator
            # actually tried -- CONFIRMED the broken state instead of
            # repairing it (msg 9067). The registration half of `reveille
            # init` learned this already ("already registered, left alone"
            # kept a stale literal-token config through a rotation, cli.py
            # remove-then-add); the hook half did not, and the hook half is
            # the one that decides whether an agent can be WOKEN. That is
            # also why 0.2.77's durable-spelling fix could not reach a single
            # machine that already had the cache path: every such machine
            # takes this branch. A correct entry is still left byte-identical
            # -- idempotence is preserved, it just now means converging
            # rather than detecting.
            if is_durable(cmd):
                hook_line = f"stop hook already installed: {cmd}"
            else:
                h["command"] = hook
                wrote = True
                hook_line = (f"stop hook re-pointed (the old one would not survive "
                             f"a `uv cache prune` or a moved clone): {cmd} -> {hook}")
            break
        if hook_line:
            break
    if hook_line is None:
        stops.append({"hooks": [{"type": "command", "command": hook}]})
        wrote = True
        hook_line = f"stop hook installed -> {SETTINGS} ({hook})"
    if wrote:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    if perm_line:
        print(perm_line)
    print(hook_line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
