"""`reveille init`: one command that makes a machine an agent's machine.

  export REVEILLE_URL=<broker url>
  export REVEILLE_AGENT_ROLE=<the bound name>
  export REVEILLE_TOKEN=<the minted secret>
  uvx --from git+https://github.com/secretzer0/reveille reveille init

FOUR PIECES, ONE TRANSACTION IN SPIRIT (DES-008, architect ruling 8951): the MCP
registration, the Stop hook, the credential file, and the working directory. If
one fails the others are not left standing -- a hook pointing at a bus the agent
cannot reach is worse than no hook, because it looks configured.

WHAT THIS DELIBERATELY IS NOT: a thing the broker can invoke. There is no
callback, no endpoint, no admin path that runs it. The web UI mints a token and
SHOWS this command; a browser button that installed a native agent would be a
host-shell grant, and the whole reason this is four lines a human pastes is that
the grant is then made by a shell. If that ever needs to change, it changes in
the design first.

THE TOKEN COMES FROM THE ENVIRONMENT OR STDIN, never from argv, because an
installer whose documented form is `reveille init <url> <name> <token>` puts a
root-equivalent credential into .bash_history on every machine that runs it.
"""

import argparse
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from . import install


def env_file(home=None):
    return pathlib.Path(home or pathlib.Path.home()) / ".reveille" / "agent.env"


def read_token(args_token, stdin=None):
    """Environment, then stdin, then the flag nobody should use. Returns the token
    or None. The order is the security order, not a convenience order."""
    if os.environ.get("REVEILLE_TOKEN"):
        return os.environ["REVEILLE_TOKEN"].strip()
    stdin = sys.stdin if stdin is None else stdin
    if args_token == "-" or (args_token is None and not stdin.isatty()):
        return stdin.read().strip() or None
    return (args_token or "").strip() or None


def mcp_argv(url, name, token, claude="claude"):
    """The registration command, as argv. Pure, so the shape is gated without a
    claude binary -- it is the same command docker/entrypoint.sh already runs for
    every container, which is why this installer is packaging rather than a new
    mechanism."""
    return [claude, "mcp", "add", "--transport", "http", "--scope", "user",
            "reveille", url.rstrip("/") + "/mcp",
            "--header", f"Authorization: Bearer {token}",
            "--header", f"X-Agent: {name}"]


def already_registered(claude="claude"):
    r = subprocess.run([claude, "mcp", "list"], capture_output=True, text=True)
    return r.returncode == 0 and "reveille" in r.stdout


def verify(url, name, token, timeout=10):
    """Ask the bus whether the credential we just installed works, and return what
    it said. An installer that does not prove it worked has moved the debugging
    to the user (architect ruling 8951)."""
    req = urllib.request.Request(url.rstrip("/") + "/version",
                                 headers={"Authorization": f"Bearer {token}",
                                          "X-Agent": name})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return True, r.read().decode().strip()
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code} -- the broker answered and refused this token"
    except Exception as e:
        return False, f"{e} -- the broker did not answer"


def write_credential(url, name, token, home=None):
    """0600, and the directory too. This file is the credential; a mode that lets
    another account on this machine read it makes the whole boundary decorative."""
    path = env_file(home)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"REVEILLE_URL={url}\nREVEILLE_AGENT_ROLE={name}\n"
                f"REVEILLE_TOKEN={token}\n")
    os.chmod(path, 0o600)          # explicit, in case the file already existed
    return path


def cmd_init(a):
    url = a.url or os.environ.get("REVEILLE_URL", "")
    name = a.name or os.environ.get("REVEILLE_AGENT_ROLE", "")
    token = read_token(a.token)
    missing = [n for n, v in (("REVEILLE_URL", url), ("REVEILLE_AGENT_ROLE", name),
                              ("REVEILLE_TOKEN", token)) if not v]
    if missing:
        print(f"reveille init: missing {', '.join(missing)}.\n"
              f"  export REVEILLE_URL=<broker url>\n"
              f"  export REVEILLE_AGENT_ROLE=<your agent name>\n"
              f"  export REVEILLE_TOKEN=<the minted secret>\n"
              f"then re-run. The token is read from the environment or stdin and "
              f"never from the command line, so it stays out of your shell "
              f"history.", file=sys.stderr)
        return 2

    claude = a.claude or shutil.which("claude") or "claude"
    workdir = pathlib.Path(a.dir or os.getcwd()).resolve()

    # VERIFY BEFORE INSTALLING ANYTHING. The credential is the one thing that can
    # be wrong in a way no amount of correct installation fixes, and finding out
    # first is what keeps a failure from leaving a half-configured machine: at
    # this point there is nothing to undo (ruling 5).
    ok, said = verify(url, name, token)
    if not ok and not a.force:
        print(f"reveille init: REFUSING -- {said}.\n"
              f"  url:   {url}\n  agent: {name}\n"
              f"Nothing was installed. Check the broker url and the token, or "
              f"pass --force to install against a bus you cannot reach right now.",
              file=sys.stderr)
        return 1

    steps = []
    if already_registered(claude):
        steps.append("mcp: already registered (left alone)")
    else:
        r = subprocess.run(mcp_argv(url, name, token, claude),
                           capture_output=True, text=True)
        if r.returncode != 0:
            print(f"reveille init: `claude mcp add` failed at step 1 of 3, so "
                  f"nothing else was installed -- a Stop hook pointing at a bus "
                  f"this machine is not registered with looks configured and is "
                  f"not.\n{(r.stderr or r.stdout).strip()}", file=sys.stderr)
            return 1
        steps.append("mcp: registered")

    hook_rc = install.main()
    if hook_rc != 0:
        print("reveille init: the Stop hook did not install. The MCP registration "
              "above stands -- this machine can reach the bus, but nothing will "
              "keep a waiter armed, so wake it by draining inbox() per turn until "
              "this is fixed.", file=sys.stderr)
        return 1
    steps.append("hook: installed")

    path = write_credential(url, name, token, a.home)
    steps.append(f"credential: {path} (0600)")
    steps.append(f"workdir: {workdir}")

    print("\n".join(steps))
    print(f"\nbus answered: {said}")
    print(f"start working:  cd {workdir} && claude")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(prog="reveille", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="make this machine an agent's machine")
    i.add_argument("url", nargs="?", help="broker url (or $REVEILLE_URL)")
    i.add_argument("name", nargs="?", help="agent name (or $REVEILLE_AGENT_ROLE)")
    i.add_argument("token", nargs="?",
                   help="'-' to read the token from stdin. Prefer $REVEILLE_TOKEN: "
                        "a token in argv lands in your shell history")
    i.add_argument("--dir", help="working directory the agent starts in "
                                 "(default: the current directory)")
    i.add_argument("--claude", help="path to the claude binary")
    i.add_argument("--home", help=argparse.SUPPRESS)   # tests
    i.add_argument("--force", action="store_true",
                   help="install even if the broker did not answer")
    i.set_defaults(fn=cmd_init)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
