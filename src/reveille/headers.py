"""reveille-headers: the MCP headersHelper for per-directory identity.

A user-scope MCP registration expands its ${VAR} headers from the process
environment at connect time, BEFORE Claude Code injects the project's settings
env -- measured on the first per-directory acceptance run (2026-08-13): the
session's Bash subshell saw $REVEILLE_AGENT_ROLE while the MCP headers had
expanded empty and the broker refused the call. So headers cannot ride ${VAR}
references for a credential that lives in the directory; they ride THIS
command, named as headersHelper in the directory's .mcp.json. Claude Code runs
it with the project directory as cwd, fresh on every connect and reconnect, so
the one place the credential lives -- .claude/settings.local.json's env block
-- is read at the moment the connection needs it, and a rotation is still a
one-file rewrite.

A directory that is not an agent (no file, no env block, malformed JSON)
yields NO headers rather than an error: the session connects anonymously and
the broker refuses it, which is the same inert-outside-an-agent-directory
behavior the Stop hook already has. The helper never prints a partial
identity -- a token without a name or a name without a token is treated as
absent, because half a credential authenticates as nobody and confuses the
refusal it produces.
"""
import json
import pathlib


def gather(root="."):
    """The headers for the agent directory at root, or {} when it is not one."""
    try:
        cfg = json.loads(
            (pathlib.Path(root) / ".claude" / "settings.local.json").read_text())
    except (OSError, ValueError):
        return {}
    env = cfg.get("env", {})
    token = env.get("REVEILLE_TOKEN", "")
    name = env.get("REVEILLE_AGENT_ROLE", "")
    if not (token and name):
        return {}
    return {"Authorization": f"Bearer {token}", "X-Agent": name}


def main():
    print(json.dumps(gather()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
