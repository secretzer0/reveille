#!/usr/bin/env python3
"""`reveille-install-hook`: put the wake-waiter Stop hook into ~/.claude/settings.json.

User scope, so it covers every session on this machine. Idempotent: a settings
file that already names this hook is reported and left alone, which is what lets
`reveille init` re-run without asking whether it has run before.

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
def settings_path():
    """~/.claude/settings.json, or CLAUDE_CONFIG_DIR when it is set. Read at CALL
    time rather than import time so a test HOME is honoured -- a module-level
    constant here made every gate write into the real home directory."""
    return pathlib.Path(os.environ.get("CLAUDE_CONFIG_DIR")
                        or (pathlib.Path.home() / ".claude")) / "settings.json"


def main():
    SETTINGS = settings_path()
    hook = hook_command()
    settings = json.loads(SETTINGS.read_text()) if SETTINGS.exists() else {}
    stops = settings.setdefault("hooks", {}).setdefault("Stop", [])
    for group in stops:
        for h in group.get("hooks", []):
            if h.get("command", "").endswith(("agent-stop-hook", "reveille-stop-hook")):
                print(f"stop hook already installed: {h['command']}")
                return 0
    stops.append({"hooks": [{"type": "command", "command": hook}]})
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")
    print(f"stop hook installed -> {SETTINGS} ({hook})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
