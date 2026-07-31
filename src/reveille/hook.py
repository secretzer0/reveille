"""`reveille-stop-hook`: run the Stop hook from wherever the package is installed.

THE HOOK USED TO POINT INTO A CLONE. install-hook wrote an absolute path to
scripts/agent-stop-hook, which is fine on a machine that has this repo checked
out and useless on one that does not -- an agent installed with `uv tool
install` got a settings.json naming a file that was never there. That is the
item everything else in the installer waits on, because a hook pointing at
nothing fails silently at exactly the moment it is supposed to keep an agent
reachable.

The hook itself stays bash: it must work when python is broken, when the bus is
down, and when the agent's own environment is half-configured. This is a
launcher for it, so the COMMAND in settings.json is a name on PATH rather than a
path into somebody's checkout.
"""
import os
import pathlib
import sys


def hook_path():
    return pathlib.Path(__file__).resolve().parent / "agent-stop-hook"


def main():
    hook = hook_path()
    if not hook.is_file():
        print(f"reveille-stop-hook: {hook} is missing -- the package is installed "
              f"without its hook, so nothing would keep this agent reachable",
              file=sys.stderr)
        return 1
    # exec, not subprocess: the hook's exit status IS this command's contract with
    # Claude Code, and an extra process between them is one more thing that can
    # swallow it.
    os.execv("/bin/bash", ["/bin/bash", str(hook), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
