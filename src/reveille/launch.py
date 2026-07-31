"""`agent`: start a Claude Code session bound to a bus identity.

The console-script half of what used to be scripts/agent -- repo-only, which is
why an agent installed without a clone had no way to start a bound session. It
sources ~/.reveille/agent.env, exports the three variables, and execs claude.

WITHOUT THIS, AN INSTALLED AGENT IS DEAF. `reveille init` writes the credential
file and nothing reads it; a plain `claude` session has no
$REVEILLE_AGENT_ROLE, so the Stop hook fails open and stays inert, no waiter is
armed, and the agent can send on the bus while never being woken. It looks
installed and answers only when a human happens to run a turn.
"""
import os
import pathlib
import sys


def script_path():
    return pathlib.Path(__file__).resolve().parent / "agent-launch"


def main():
    p = script_path()
    if not p.is_file():
        print(f"agent: {p} is missing -- this package was installed without its "
              f"launcher, and a session started without it is not woken by "
              f"anything", file=sys.stderr)
        return 1
    os.execv("/bin/bash", ["/bin/bash", str(p), *sys.argv[1:]])


if __name__ == "__main__":
    sys.exit(main())
