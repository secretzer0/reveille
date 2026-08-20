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
import contextlib
import getpass
import socket
import hashlib
import re
import json
import os
import pathlib
import secrets
import signal
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

from reveille import __version__

from . import install, spool
from .devicecode import cli_code


def read_token(args_token, stdin=None):
    """A TOKEN A HUMAN SUPPLIED BEATS THE ONE THE DIRECTORY IS CARRYING.

    This read the environment first, and inside an agent directory that is
    always the wrong answer: Claude Code injects settings.local.json's env into
    every shell it starts, so $REVEILLE_TOKEN is set to the very credential the
    person is running the installer to REPLACE. Piping the new secret in did
    nothing -- the dead one won, was verified, was refused, and the installer
    reported the refusal as though the paste had been wrong (operator, live).
    Stdin and the flag are deliberate acts by someone holding the new secret on
    their screen; the environment is the fallback for a re-run that supplies
    nothing. That is still the security order -- neither path puts a secret in
    shell history -- it is only no longer the order that ignores the human.
    """
    stdin = sys.stdin if stdin is None else stdin
    if args_token == "-" or (args_token is None and not stdin.isatty()):
        try:
            piped = stdin.read().strip()
        except OSError:
            # A stdin that cannot be read is not a token: an implicit pipe is a
            # guess about the caller's shape, so a wrong guess falls through to
            # the environment rather than failing the install.
            piped = ""
        if piped:
            return piped
    elif args_token:
        return args_token.strip()
    return os.environ.get("REVEILLE_TOKEN", "").strip() or None


def register_mcp_local(url, workdir, claude):
    """Register the MCP at LOCAL scope, so nothing per-agent lands in the tree.

    Project scope means <workdir>/.mcp.json -- a FILE IN THE REPO. It carries no
    secret (the headersHelper below is a mechanism name, not a credential), but
    it is still per-agent configuration sitting in a shared, tracked working
    tree, and in a checkout two people share it is one more thing to collide
    over and commit. Local scope stores the same registration in ~/.claude.json
    keyed by this project path: read by Claude Code exactly the same way, never
    in the repo, and needing no enableAllProjectMcpServers approval
    (architect 12167).

    The HEADERS still ride a headersHelper -- reveille-headers, a console script
    this package ships. The first cutover used ${VAR} headers and the acceptance
    run caught the seam: Claude Code expands MCP headers from the process
    environment at connect time, BEFORE project settings env is injected, so the
    session's Bash saw the identity while the MCP headers expanded empty. The
    helper runs with the project directory as cwd, fresh on every connect,
    reading the same settings.local.json the credential lands in.
    """
    spec = json.dumps({
        "type": "http",
        "url": url.rstrip("/") + "/mcp",
        "headersHelper": "reveille-headers",
    })
    # REMOVE FIRST, AND IT IS NOT BELT-AND-BRACES. `claude mcp add-json` REFUSES
    # a name already registered ("MCP server reveille already exists in local
    # config") and carries no --force, so without this init is a ONE-SHOT
    # command: every directory it has already touched refuses it forever. That
    # is exactly the directory that needs it -- waked's own PARKED message says
    # "`reveille init` also works", and on 2026-08-19 it did not, which left a
    # recalled body with no way back (defect 2, chain step 8). The remove is
    # best-effort: a first run has nothing to remove and must not fail on that.
    subprocess.run([claude, "mcp", "remove", "reveille", "--scope", "local"],
                   capture_output=True, text=True, cwd=str(workdir))
    r = subprocess.run([claude, "mcp", "add-json", "--scope", "local", "reveille", spec],
                       capture_output=True, text=True, cwd=str(workdir))
    if r.returncode != 0:
        raise RuntimeError(
            f"`claude mcp add-json --scope local` failed: "
            f"{(r.stderr or r.stdout).strip()}")
    return "~/.claude.json (local scope)"


def tracked_by_git(path):
    """Is this path committed to a git repo? Anything that is not a clean yes --
    no git, no repo, an error -- is a no, because the only thing this answer
    guards is whether to delete somebody's file."""
    try:
        return subprocess.run(
            ["git", "ls-files", "--error-unmatch", str(path)],
            cwd=str(pathlib.Path(path).parent), capture_output=True,
            text=True).returncode == 0
    except OSError:
        return False


def drop_project_mcp_entry(workdir):
    """Remove OUR entry from a <workdir>/.mcp.json an earlier init wrote.

    Migration, idempotent (architect 12167). Two registrations for one server is
    how a body ends up authenticating twice by different rules; the project-scope
    one is the stale shape. Every OTHER server in that file is somebody else's
    and survives untouched -- and a file left holding nothing but an empty
    mcpServers map is removed, because an empty config in a tracked tree is
    litter that outlives the reason for it.
    """
    path = pathlib.Path(workdir) / ".mcp.json"
    if not path.exists():
        return None
    try:
        cfg = json.loads(path.read_text())
    except (ValueError, UnicodeDecodeError):
        # Not ours to repair -- and refusing to touch it is the same discipline
        # the write path had: it may name servers this installer does not own.
        return None
    servers = cfg.get("mcpServers") or {}
    if "reveille" not in servers:
        return None
    servers.pop("reveille")
    if not servers and set(cfg) <= {"mcpServers"}:
        # A TRACKED FILE IS NOT THIS INSTALLER'S TO DELETE. Removing our entry
        # is correctness -- two registrations for one server is how a body
        # authenticates twice by different rules -- but deleting a file git is
        # watching turns an install into a staged deletion the person did not
        # ask for and may not notice until a commit takes it. So: empty it and
        # leave it, and let the removal be their commit.
        if not tracked_by_git(path):
            path.unlink()
            return path
    cfg["mcpServers"] = servers
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


def ignore_the_credential(workdir):
    """Make sure <workdir>/.claude/settings.local.json cannot be committed.

    The credential is written INTO A GIT WORKING TREE, and whether it is ignored
    was, until now, a property of the person's own machine: on this laptop a
    personal ~/.config/git/ignore covered it, and nowhere else did. A fresh
    clone by anyone else leaves a live agent token untracked-but-not-ignored,
    one `git add -A` from a public repo.

    The fix stays inside what we own: a .gitignore in the .claude directory THIS
    INSTALLER CREATES. Not the repo's own .gitignore (a tracked file that is the
    project's, not ours) and NOT .git/info/exclude (the user's git config, and a
    tool that writes there to protect its own mess is fixing the wrong layer --
    operator, 2026-08-19). Self-contained: it ships beside the credential, so a
    clone that gets one gets the other.
    """
    d = pathlib.Path(workdir) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    path = d / ".gitignore"
    # BOTH SECRETS THIS DIRECTORY CAN HOLD, not just the one this function was
    # named for (architect blocking on #151): waked writes the spent credential
    # to .reveille-parked in the same directory, and an ignore file that names
    # only one of two secrets is a published-identity hole with a green install.
    # Appends WHICHEVER is missing -- the old single-name early return meant a
    # dir init had ever touched could never gain a line.
    want = ["settings.local.json", ".reveille-parked"]
    text = path.read_text() if path.exists() else ""
    have = text.split()
    missing = [w for w in want if w not in have]
    if not missing:
        return path, False
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + "".join(w + "\n" for w in missing))
    return path, True


def verify(url, name, token, timeout=10):
    """Ask the bus whether the credential works, and return what it said.

    /presence AND NOT /version, and the difference is the whole value of this
    function: version_http discards its request, resolves no principal, and sits
    beside /health above every authenticated surface -- so asking it proved the
    broker was REACHABLE and proved nothing whatever about the token. A revoked,
    mistyped or foreign credential installed cleanly and failed later, on the
    agent's first turn, which is exactly the debugging this was meant to take off
    the user (architect BLOCKING 1, msg 8966). /presence resolves the bearer
    through _principal, so a bad token is a 401 here and the refusal below is a
    path that can actually be reached.
    """
    req = urllib.request.Request(url.rstrip("/") + "/presence",
                                 headers={"Authorization": f"Bearer {token}",
                                          "X-Agent": name})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode().strip()
            return True, f"{len(json.loads(body).get('agents', []))} agents present"
    except urllib.error.HTTPError as e:
        # REFUSED is not the same fact as UNREACHABLE, and the difference now
        # decides something: a refusal is proof the credential is dead, while
        # silence proves nothing about it at all. False means the broker said
        # no; None means nobody could ask. Both still refuse the install below
        # unless --force, so the answer this adds costs the caller nothing.
        return False, f"HTTP {e.code} -- the broker answered and refused this token"
    except Exception as e:
        return None, f"{e} -- the broker did not answer"


def agent_of_directory(workdir):
    """The agent name <workdir> already belongs to, or "" -- read from the same
    settings.local.json the credential lands in. Unreadable or absent is "",
    because the only thing this answer guards is a refusal."""
    path = pathlib.Path(workdir) / ".claude" / "settings.local.json"
    try:
        return ((json.loads(path.read_text()).get("env") or {})
                .get("REVEILLE_AGENT_ROLE") or "")
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return ""


def mcp_registered(workdir):
    """Is the local-scope registration for this directory already in
    ~/.claude.json? Read-only, shape-tolerant: the file is claude's, not ours,
    and the only thing this answer guards is whether a boot with no claude
    binary may DEGRADE instead of refusing -- so anything unreadable is False,
    which fails toward the hard refusal."""
    try:
        cfg = json.loads((pathlib.Path.home() / ".claude.json").read_text())
        proj = (cfg.get("projects") or {}).get(str(workdir)) or {}
        return "reveille" in (proj.get("mcpServers") or {})
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        return False


def write_credential(url, name, token, workdir):
    """THE DIRECTORY IS THE AGENT (operator ruling, 2026-08-13). The credential
    goes in <workdir>/.claude/settings.local.json's env block, which Claude Code
    injects at session start -- so plain `claude` run in that directory IS the
    agent: MCP headers, the Stop hook and the waked it spawns all inherit the
    identity from the session env. One machine holds as many agents as it has
    directories; ~/.reveille/agent.env and the reveille-agent wrapper are gone.

    CONVERGES rather than detects presence (the installer's own idempotence
    ruling): existing REVEILLE_* values are rewritten to correct, every other
    key in the file is preserved. A file that cannot be parsed is refused, not
    clobbered -- it holds settings this installer does not own. 0600, atomic
    replace: this file is the credential; a mode that lets another account read
    it makes the whole boundary decorative."""
    d = pathlib.Path(workdir) / ".claude"
    d.mkdir(parents=True, exist_ok=True)
    path = d / "settings.local.json"
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise RuntimeError(
                f"{path} exists and is not valid JSON ({e}). It holds settings "
                f"this installer does not own, so it is refused rather than "
                f"clobbered -- fix or remove it, then re-run.")
    env = cfg.setdefault("env", {})
    env["REVEILLE_URL"] = url
    env["REVEILLE_AGENT_ROLE"] = name
    env["REVEILLE_TOKEN"] = token
    # SEEDED, NOT CONVERGED, and the split is deliberate: a credential has one
    # correct value so wrong ones are rewritten above, but these are the
    # operator's communication-mode preferences (operator directive,
    # 2026-08-13) -- an agent talks caveman-ultra for token economy and builds
    # ponytail-full for restraint, IF those plugins are installed; the vars are
    # inert otherwise. A value the user tuned by hand IS the correct value, so
    # setdefault never overrides an existing choice, only fills an absence.
    env.setdefault("CAVEMAN_DEFAULT_MODE", "ultra")
    env.setdefault("PONYTAIL_DEFAULT_MODE", "full")
    # The local-scope registration (register_mcp_local) is what carries the identity
    # into MCP, and a project-scope server needs the session's approval;
    # granting it here is the same single-write pre-approval the installer
    # already does for permissions.allow -- without it the first session gets
    # a prompt nobody unattended can answer.
    cfg["enableAllProjectMcpServers"] = True
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)
    os.chmod(path, 0o600)          # explicit, in case the file already existed
    # A FRESH CREDENTIAL SPENDS THE PARKED ONE (architect nit on #151). The
    # sibling .reveille-parked is a spent secret waked remembered so a restart
    # could still claim a return ticket -- for the identity this directory HAD.
    # init just gave it a live credential (possibly a different identity), so a
    # daemon booting here must never prefer that stale secret and poll a
    # foreign claim for fifteen minutes. Same rule as clear_parked-on-attach.
    try:
        os.unlink(d / ".reveille-parked")
    except OSError:
        pass
    return path


def retire_waked(name):
    """Stop the wake daemon still carrying the OLD credential. Returns a line to
    report, or "".

    A RE-KEY MUST REACH THE DAEMON (ruling 12008; measured 2026-08-18). waked
    reads $REVEILLE_TOKEN once, at spawn, and holds it for the life of the
    process -- on that day it held one for four hours and forty-six minutes
    across a body swap and back, kept an ESTABLISHED socket the broker no
    longer counted as a waiter, and left the agent deaf while every file on
    disk said it was configured correctly. Writing the new credential is not
    enough; the process reading the old one has to go.

    By PID from the spool lock, never by pattern: the flock proves that pid is
    the holder, and a pattern match on a process table would sooner or later
    find the command line of whoever is running this. The Stop hook respawns a
    daemon from the fresh session env at the next turn boundary."""
    pid = spool.holder_pid(name)
    if not pid:
        return ""
    try:
        os.kill(pid, 0)            # live? a stale file names a recycled pid
    except OSError:
        return ""
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return f"could not stop the old wake daemon (pid {pid}): {e}"
    return (f"stopped the wake daemon holding the previous credential (pid {pid}) -- "
            f"the Stop hook starts a fresh one on the new one")


# ---- minting from a password (operator ask, 2026-07-31) ---------------------
# The browser flow is: log in, mint a bound token, copy it, paste it into a
# shell. This is the same three steps done by the shell that is already about to
# hold the credential -- which removes the copy-paste and nothing else. It is
# strictly MORE reach than pasting a token: run with a password, this command can
# mint a credential for any agent name the account owns. That is what makes it
# worth having and also why the web UI must still never run it: a browser button
# that did this would be a host-shell grant with a password behind it.


def _post(url, path, payload, cookie=None, timeout=15):
    """POST json, return (status, body-dict, set-cookie). Stdlib only, like the
    rest of this installer -- a JSON POST does not earn a dependency."""
    req = urllib.request.Request(url.rstrip("/") + path,
                                 data=json.dumps(payload).encode(),
                                 headers={"content-type": "application/json"})
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {}), \
                r.headers.get("set-cookie", "")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}"), ""
        except Exception:
            return e.code, {}, ""


def _get(url, path, cookie, timeout=15):
    req = urllib.request.Request(url.rstrip("/") + path, headers={"Cookie": cookie})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode() or "{}")


def _call(url, path, method="GET", cookie=None, timeout=15):
    """(status, body) for the calls whose STATUS is the answer -- the sign-in
    poll replies 202, 200 or 404 and none of those is an error to raise on."""
    req = urllib.request.Request(url.rstrip("/") + path, method=method)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        with contextlib.suppress(Exception):
            return e.code, json.loads(e.read().decode() or "{}")
        return e.code, {}


# ---- DES-022: one sign-in per machine, every agent minted from it ------------------

def auth_file():
    """ONE file per machine, outside every project (DES-022 s3). Resolved on
    every call rather than at import: $HOME is a runtime fact -- a container
    entrypoint sets it, and a test that pinned it at import time would write
    into the developer's own home."""
    return pathlib.Path.home() / ".reveille" / "auth.json"


POLL_EVERY_S = 2
LOGIN_WINDOW_S = 300


def auth_cookie(d):
    """The stored session as a Cookie header. The NAME comes from the broker
    (it is __Host-rev_session on https and rev_session on http), because
    guessing it here would fail exactly on the deployments that matter."""
    return f"{d['cookie']}={d['session']}"


def read_auth(url=""):
    """This machine's sign-in, or {} -- and {} for a session belonging to a
    DIFFERENT broker: one file holds one, and using it elsewhere would be a
    401 dressed up as a configuration."""
    try:
        d = json.loads(auth_file().read_text())
    except (OSError, ValueError):
        return {}
    if not (d.get("session") and d.get("cookie")):
        return {}
    if url and d.get("url", "").rstrip("/") != url.rstrip("/"):
        return {}
    return d


def write_auth(d):
    """0600 in a 0700 directory (DES-022 s6): the file holds a session that can
    mint agents, which is the same power as a signed-in browser tab."""
    path = auth_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)                     # a pre-existing file keeps its mode
    path.write_text(json.dumps(d, indent=1) + "\n")
    return d


def device_login(url, open_browser=True, sleep=time.sleep, window=LOGIN_WINDOW_S):
    """Print ONE link, wait for it to be used (DES-022 s3). No loopback
    listener: that needs the browser on this machine, which rules out ssh,
    containers and the phone in the reader's hand. The state is 256 bits and
    single-use, so polling it is worth nothing to anyone who did not make it."""
    state = secrets.token_urlsafe(32)
    code, body = _call(url, f"/auth/cli/{state}", "POST")
    if code == 404:
        raise RuntimeError(
            f"{url} does not offer CLI sign-in (needs broker 0.2.187+) -- mint the "
            f"token in the web UI, Settings -> Tokens, and pass it to `reveille init`")
    if code != 200:
        raise RuntimeError(f"{url} refused to start a sign-in ({code}): "
                           f"{body.get('error') or body}")
    link = f"{url.rstrip('/')}/auth/cli?cli={state}"
    # THE CODE IS WHAT MAKES THE LINK SAFE TO CLICK (architect 12183). A link
    # alone can be mailed to somebody else, and their browser would sign THEM in
    # under the sender's state -- a 14-day minting session, collected by whoever
    # sent it, with nothing on the victim's screen looking wrong. The page shows
    # this code; an attacker cannot put it on the victim's terminal because they
    # never see that terminal. So it is printed here, first, and named as the
    # thing to compare.
    print(f"\nSign in here -- any browser, any device:\n\n    {link}\n\n"
          f"    code: {cli_code(state)}\n\n"
          f"The page will show that code. If it shows a different one, or you did "
          f"not\nexpect this, do not continue -- somebody else asked for it.\n")
    if open_browser:
        with contextlib.suppress(Exception):
            webbrowser.open(link)
    print("waiting for that link", end="", flush=True)
    deadline = time.monotonic() + window
    while time.monotonic() < deadline:
        sleep(POLL_EVERY_S)
        code, body = _call(url, f"/auth/cli/{state}")
        if code == 200 and body.get("session"):
            print(f"\nsigned in as {body['user']}")
            return dict(body, url=url.rstrip("/"))
        if code == 404:
            break
        print(".", end="", flush=True)
    print()
    raise RuntimeError("that link was not used in time -- run `reveille login` again")


def sign_in(url, no_prompt=False, open_browser=True):
    """A fresh sign-in for this machine, by whichever way in the broker has."""
    doors = json.loads(urllib.request.urlopen(
        url.rstrip("/") + "/auth/doors", timeout=15).read().decode())
    if doors.get("doors"):
        # A LINK IS A PROMPT (it waits for a human to click it), so --no-prompt
        # refuses it rather than parking a script on a poll for five minutes at
        # a terminal nobody is watching.
        if no_prompt:
            raise RuntimeError(f"no sign-in stored for {url}; run `reveille login "
                               f"{url}` and click the link, then re-run this")
        return device_login(url, open_browser=open_browser)
    # NO DOORS MEANS THE PASSWORD DOOR IS OPEN (the broker's own rule: the two
    # are exclusive, daemon._password_closed). A password is something this
    # terminal can take directly, so it does -- sending someone to a browser to
    # type one, then polling for the result, would be a round trip that exists
    # only because the OTHER kind of door needs one.
    if no_prompt:
        raise RuntimeError(f"{url} signs in by password; run `reveille login` where "
                           f"it can prompt, or pass a token")
    user = os.environ.get("REVEILLE_USER") or ask("your broker username")
    name, secret = login(url, user, read_password(user)).split("=", 1)
    return {"url": url.rstrip("/"), "user": user, "session": secret, "cookie": name,
            "expires_ns": 0}


def session_cookie(url, no_prompt=False, open_browser=True):
    """(Cookie header, whose it is) for this machine -- the stored sign-in if the
    broker still honours it, else a fresh one, saved for the next agent here.

    THE 401 IS THE RE-AUTH TRIGGER (DES-022 s4): a session revoked from the
    Sessions view means the next init prints the link again, rather than failing
    with something the reader has to translate into an action."""
    d = read_auth(url)
    if d and _call(url, "/tokens", cookie=auth_cookie(d))[0] == 200:
        return auth_cookie(d), d.get("user", "")
    d = write_auth(sign_in(url, no_prompt=no_prompt, open_browser=open_browser))
    return auth_cookie(d), d.get("user", "")


def login(url, user, password):
    """One web session for the installer's few calls; the cookie, or a
    RuntimeError naming why the broker refused."""
    code, body, cookie = _post(url, "/login", {"name": user, "password": password})
    if code == 410:
        # DES-018 slice 2: this broker has doors and the password door is shut,
        # so there is no password for this installer to send. Name the door that
        # IS open rather than repeating a refusal the reader cannot act on.
        raise RuntimeError(
            f"password sign-in is closed on {url} -- there is nothing to send. Run "
            f"`reveille login {url}`, click the link, and then `reveille init {url} "
            f"<name>` mints from that sign-in (no --login, no paste)")
    if code != 200 or not cookie:
        raise RuntimeError(f"login failed ({code}): {body.get('error') or body}")
    return cookie.split(";", 1)[0]


def my_agents(url, cookie):
    """The account's agents that hold a live token, with their rooms, from
    GET /tokens -- the picker's inventory (operator ask, 2026-08-17: "a list of
    my agents that exist, so I can pick one and be its native version"). An
    agent whose every token was revoked is still a live identity the broker
    will bind to; it just is not offered here, and typing its name still works."""
    seen = {}
    for t in _get(url, "/tokens", cookie).get("tokens", []):
        name = t.get("agent_name")
        if name:
            # THE PAYLOAD IS {room_id: room_name} (store.rooms_for_token), so the
            # VALUES are the names. Iterating it produced room IDs -- which the
            # picker printed at people as if they were names, and which made the
            # room-carrying mint refuse a live agent with "carries no rooms you
            # can reach" while naming the id it had just failed to match
            # (measured 2026-08-19, red-shirt-01). The stub that gated this
            # returned a list of {id, name} dicts: a fake shape no route serves.
            seen.setdefault(name, set()).update((t.get("rooms") or {}).values())
    return sorted((name, sorted(rooms)) for name, rooms in seen.items())


def ask_agent(agents, stdin=None):
    """Which agent this directory becomes: one of yours by number (this machine
    takes over its identity -- the token is rotated, the old body goes dead), or
    a NEW name typed in. Returns (name, is_new).

    NO DEFAULT, AND ONE EXPLICIT YES (ruling 11246): attaching a directory to a
    LIVE identity kills whatever body holds it now, and the password prompt used
    to be the human stop before that happened; with the session already open,
    this prompt IS the stop. So Enter picks nothing, and an existing agent is
    confirmed by name -- piped or empty input never rotates a token."""
    if not agents:
        return "", True
    print("\nYour agents (pick one to make THIS directory its native body, "
          "or type a new name):")
    for i, (name, rooms) in enumerate(agents, 1):
        print(f"  {i}. {name:<28} {', '.join(rooms) or '(no rooms)'}")
    while True:
        pick = ask("agent (number, or a new name)", "", stdin)
        if not pick:
            raise RuntimeError("no agent chosen; nothing was minted")
        held = agents[int(pick) - 1][0] if pick.isdigit() and 1 <= int(pick) <= len(agents) \
            else pick if any(pick == n for n, _ in agents) else ""
        if held:
            if ask(f"take over {held!r}? Its current token is superseded and the machine "
                   f"holding it goes dead on its next call. [y/N]", "N", stdin).lower() \
                    .startswith("y"):
                return held, False
            continue
        if NAME_OK(pick):
            return pick, True
        print("  names are letters, digits, _ and -, starting with a letter or digit")


def mint_token(url, user, password, agent, rooms=None, tier="state", pick=None,
               create=False, confirm_create=None, cookie=None, keep_session=False):
    """Log in (unless a session cookie is handed in), mint a token BOUND to
    `agent`, attach rooms. Returns (secret, rooms-attached, note, pending) or
    raises RuntimeError with what to fix. `pending` is the broker's word for "an
    existing body still holds this identity and keeps it until this credential
    joins" -- the caller needs it, because what a pending mint may disturb
    locally is nothing at all (ruling 12320 R5).

    Binding supersedes the account's previous token for that name -- the broker's
    existing one-identity-one-live-credential rule, not something this adds. So
    re-running this rotates the credential rather than accumulating four live
    ones for a single agent, which is a state the operator has found in their
    own Tokens tab before.
    """
    cookie = cookie or login(url, user, password)

    payload = _get(url, "/rooms", cookie)
    text, ordered, n_mine = room_listing(payload)
    if not ordered:
        raise RuntimeError(
            f"{user} can reach no rooms, so a token minted here would reach "
            f"nothing. Create or join a room first.")
    if rooms:
        want_ids = choose_rooms(rooms, ordered, n_mine)
    elif pick:
        print(f"\nRooms this agent can be assigned to:\n{text}")
        want_ids = choose_rooms(pick(), ordered, n_mine)
    else:
        # A BOUND RE-MINT CARRIES THE IDENTITY'S ROOMS, NEVER THE OWNER'S
        # (defect, measured live 2026-08-19). This used to default to every
        # room the OWNER could reach, so materialising red-shirt-01 -- an agent
        # in one room -- silently handed its new body three, including rooms
        # its old body had deliberately left. An owner's reach is what they may
        # grant; it is not a statement about where this agent belongs. Widening
        # a scope is a deliberate act and it has a flag: --rooms.
        by_name = {r["name"]: r["id"] for r in ordered}
        held = dict(my_agents(url, cookie)).get(agent) or []
        want_ids = [by_name[n] for n in held if n in by_name]
        if not want_ids:
            raise RuntimeError(
                f"{agent!r} carries no rooms you can reach, so a token minted "
                f"here would reach nothing. Name the rooms with --rooms"
                + (f" (it is in: {', '.join(held)})." if held else "."))
    by_id = {r["id"]: r["name"] for r in ordered}
    want = [by_id[i] for i in want_ids]

    # ONE call: the mint attaches its rooms in the same broker transaction, or
    # nothing exists afterwards (ruling 9010). The two-step version POSTed its
    # attach to a PATCH-only route -- impossible on any box, discovered by the
    # operator's real install, invisible to a stub that accepts any method --
    # and its failure mode was a minted token reaching nothing, with a message
    # telling a human to go and revoke it. That state is unrepresentable now,
    # so the message is gone rather than improved.
    code, tok, _ = _post(url, "/tokens",
                         {"agent_name": agent, "label": f"native {agent}",
                          "mem_tier": tier, "create": create,
                          "rooms": want_ids}, cookie)
    if code == 400 and tok.get("error") == "unknown_agent":
        # THE GUARD AGAINST SILENT FORKS (ruling 10896): the broker refuses to
        # mint a NEW identity unless creation is declared, and shows the
        # owner's live agents so a near-miss ('architect' typed where
        # 'reveille-architect' exists) is caught at the prompt instead of
        # becoming a fork that every health check reports as fine. The wizard
        # asks; a script gets the refusal and passes --create on purpose.
        live = tok.get("live_agents") or []
        if confirm_create:
            print(f"\nNo live agent of yours is named {agent!r}."
                  + (" Your live agents:\n  " + "\n  ".join(live) if live
                     else " You have no live agents yet."))
            if confirm_create(agent):
                code, tok, _ = _post(url, "/tokens",
                                     {"agent_name": agent,
                                      "label": f"native {agent}",
                                      "mem_tier": tier, "create": True,
                                      "rooms": want_ids}, cookie)
            else:
                raise RuntimeError(
                    f"declined creating a new agent {agent!r}; nothing was "
                    f"minted. Re-run with one of your live agent names to "
                    f"attach this machine to it.")
        else:
            raise RuntimeError(
                f"{tok.get('detail')} Your live agents: "
                f"{', '.join(live) if live else '(none)'}. Re-run with "
                f"--create to deliberately create a new agent.")
    if code != 200 or not tok.get("secret"):
        # detail carries the WHY and, for a held name, both remedies; the bare
        # error word alone ("name_held") refuses without telling.
        raise RuntimeError(
            f"mint failed ({code}): {tok.get('detail') or tok.get('error') or tok}")
    attached = want
    # CLOSE THE SESSION. It was minted for three calls and there is no reason for
    # it to outlive them; leaving it valid means the installer left a live
    # session behind on every machine it ever ran on (architect, msg 8987).
    # Best effort: the mint succeeded and a logout that fails must not fail the
    # install, since the credential the caller needs is already in hand.
    # NOT THE MACHINE'S OWN SIGN-IN (DES-022): that one is deliberately durable
    # -- it exists so the NEXT agent here mints without asking again -- and
    # ending it would make one init per sign-in, which is the round trip this
    # whole design removes. Only a session this call created is closed by it.
    if not keep_session:
        with contextlib.suppress(Exception):
            _post(url, "/logout", {}, cookie)

    note = ""
    if tok.get("superseded"):
        note = f" (superseded {len(tok['superseded'])} previous token(s) for {agent})"
    # SAY THAT AN EARLIER MOVE WAS RETRACTED. A person re-running init after a
    # failed materialisation is holding a link or a container that will now be
    # refused, and the only place they can learn that is here.
    n = len(tok.get("discarded_pending") or ())
    if n:
        note += (f" (discarded {n} unclaimed move(s) for {agent}: whatever was "
                 f"minted for them cannot arrive)")
    return tok["secret"], attached, note, bool(tok.get("pending"))


def room_listing(payload):
    """(display text, ordered room list) from GET /rooms. Yours first, then
    public rooms as "owner -> name" -- per-owner room names are only unambiguous
    with the owner shown (the operator's own format, msg 9022)."""
    mine = list(payload.get("owned", [])) + list(payload.get("member", []))
    pub = list(payload.get("public", []))
    lines, ordered = [], []
    for r in mine:
        ordered.append(r)
        lines.append(f"  {len(ordered)}. {r['name']}")
    if pub:
        lines.append("  -----")
        for r in pub:
            ordered.append(r)
            lines.append(f"  {len(ordered)}. {r.get('owner_name') or '?'} -> {r['name']}")
    return "\n".join(lines), ordered, len(mine)


def choose_rooms(answer, ordered, n_mine):
    """Comma-separated numbers or names -> room ids. Empty = YOUR rooms only --
    not every public room on the broker: the first real run attached a stranger's
    room by default, and a grant that broad should be a choice, not a silence."""
    answer = (answer or "").strip()
    if not answer:
        return [r["id"] for r in ordered[:n_mine]]
    picked = []
    by_name = {r["name"]: r for r in ordered}
    for part in answer.split(","):
        part = part.strip()
        if part.isdigit() and 1 <= int(part) <= len(ordered):
            picked.append(ordered[int(part) - 1]["id"])
        elif part in by_name:
            picked.append(by_name[part]["id"])
        else:
            raise RuntimeError(f"no such room: {part!r}")
    return picked


def ensure_on_path():
    """A uvx run is EPHEMERAL: its console scripts live in a cache that can be
    garbage-collected, so nothing this run installed survives it -- the closing
    `reveille-agent <name>` was command-not-found on the operator's first real
    run, and worse, the Stop hook had captured the CACHE path, which dies at the
    next uv cache prune. And a bare which() cannot see the problem from inside
    the problem: uvx puts its ephemeral bin FIRST on PATH, so during `uvx ...
    reveille init` the agent binary is always "present" and the presence check
    skipped the persist on every machine -- the operator's Mac hit the exact
    command-not-found this function exists to prevent. Presence is not
    durability (install.py learned this at msg 9067); ask whether the copy
    would survive a cache prune. The probe binary is reveille-waked -- the
    console script whose durability decides whether an agent can be woken at
    all. Returns a step line, or None when already persistent."""
    w = shutil.which("reveille-waked")
    if w and install.is_durable(w):
        return None
    uv = shutil.which("uv") or "uv"
    r = subprocess.run([uv, "tool", "install", "--force", "--from", GIT_SOURCE,
                        "reveille"], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"uv tool install failed: {(r.stderr or r.stdout).strip()}")
    # this process's PATH may predate ~/.local/bin; the hook installer resolves
    # commands with which(), so it must see the durable copies
    local_bin = os.path.expanduser("~/.local/bin")
    step = f"persisted: uv tool install reveille -> {local_bin} (uvx runs are ephemeral)"
    if local_bin not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = local_bin + os.pathsep + os.environ.get("PATH", "")
        # uv prints its own PATH warning, but capture_output above swallows it
        step += (" -- ~/.local/bin is not on your shell PATH: run "
                 "`uv tool update-shell` once, then open a new shell")
    return step


def read_password(user, prompt=None):
    """Environment, then a TTY prompt. Never a flag: a password in argv is a
    password in .bash_history, and this one mints credentials."""
    if os.environ.get("REVEILLE_PASSWORD"):
        return os.environ["REVEILLE_PASSWORD"]
    return getpass.getpass(prompt or f"password for {user}: ")


# ---- the wizard (operator ask, 2026-07-31) ----------------------------------
# EVERY PROMPT HAS A DEFAULT AND SAYS WHOSE ANSWER IT WANTS. The installer used
# to require two exported variables before it would run, which is a scripted
# install wearing an interactive one's clothes -- and every question the operator
# asked about it came from having to know what those variables MEANT before they
# could type anything. A flag or an environment variable still skips its prompt,
# so scripting is unchanged.

DEFAULT_URL = "https://reveille.mythos.org"
GIT_SOURCE = "git+https://github.com/secretzer0/reveille"

# The broker's own rule, mirrored so a bad name is refused at the prompt rather
# than after a password has been typed.
NAME_OK = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}$").match

# The fleet's convention rather than a schema: these are the roles this project
# actually runs. "other" exists because a list of types that cannot be escaped
# is a list that starts lying the first time somebody needs a fifth.
AGENT_TYPES = [
    ("architect", "reveille-architect", "designs, rules, and issues verdicts"),
    ("senior-dev", "reveille-senior-dev", "implements slices and ships them green"),
    ("ui-ux", "reveille-senior-ui-ux", "the web UI and everything a human sees"),
    ("devops", "reveille-devops", "deploys, hosts, and the machines themselves"),
    ("other", "", "a name you choose"),
]


def ask(prompt, default="", stdin=None):
    """One prompt, one default shown in brackets. Enter accepts it."""
    stdin = sys.stdin if stdin is None else stdin
    if not stdin.isatty():
        return default          # piped input: take the default rather than block
    shown = f"{prompt} [{default}]: " if default else f"{prompt}: "
    return input(shown).strip() or default


def ask_type(stdin=None):
    """The agent's type, as a numbered menu. Returns (type, suggested name)."""
    print("\nWhat kind of agent is this?")
    for i, (t, name, what) in enumerate(AGENT_TYPES, 1):
        print(f"  {i}. {t:<12} {what}")
    while True:
        pick = ask("choose", "2", stdin)
        if pick.isdigit() and 1 <= int(pick) <= len(AGENT_TYPES):
            t, suggested, _ = AGENT_TYPES[int(pick) - 1]
            return t, suggested
        if pick in {t for t, _, _ in AGENT_TYPES}:
            return pick, next(n for t, n, _ in AGENT_TYPES if t == pick)
        print(f"  pick 1-{len(AGENT_TYPES)}, or the type's name")


# THE DOCTRINE BLOCK IS MANAGED, AND ONLY THE BLOCK (operator 11879). A native
# agent's CLAUDE.md is the only thing that tells it to call lessons(), brief()
# and inbox() -- the hive is PULLED, never pushed -- and red-shirt proved what
# happens without one: it joined 0.2.170 with a bus connection, a Stop hook and
# no idea who a broadcast wakes. The seed used to be written
# only on the wizard path, which the web-mint-then-paste install (the only way
# to install a native agent now that the password door is closed) never takes.
#
# So: the block is delimited and REWRITTEN in place on every init, and
# everything outside the markers is the agent's own and is never touched. A
# directory with no CLAUDE.md gets one containing the block; a CLAUDE.md
# written by a human gets the block appended once and updated thereafter. The
# markers carry the version that wrote them, so a later boot can tell whether
# what it is reading is current.
# THE BEGIN MARKER IS A SIGNATURE, not just a fence (operator, 2026-08-19). It
# carries the version that wrote the block AND a sha256 of the block's own body,
# which lets a later boot tell three states apart instead of two:
#   file-hash == marker-hash == expected   the block is current; do nothing
#   file-hash == marker-hash != expected   doctrine MOVED ON; replace, and the
#                                          log can name both versions
#   file-hash != marker-hash               somebody EDITED INSIDE THE MARKERS;
#                                          replace, and say so
# The third case is the one a version string alone cannot see, and it is exactly
# how a stale doctrine stays alive -- a person tweaks one line, the version still
# reads current, and every later boot agrees with the edit instead of correcting
# it. Everything OUTSIDE the markers is still never touched.
DOCTRINE_BEGIN_PREFIX = "<!-- reveille:begin"
DOCTRINE_END = "<!-- reveille:end -->"
_MARKER_RE = re.compile(
    r"<!-- reveille:begin(?:\s+v=(?P<v>\S+))?(?:\s+sha256=(?P<sha>[0-9a-f]+))?[^>]*-->")


def body_hash(body):
    """sha256 of the block body, short. Pure, so a gate can recompute it."""
    return hashlib.sha256(body.encode()).hexdigest()[:16]


def doctrine_begin(version, sha):
    return (f"{DOCTRINE_BEGIN_PREFIX} v={version} sha256={sha} -- managed by "
            f"`reveille init`; edit OUTSIDE these markers -->")


def doctrine_block(name, agent_type, version=__version__):
    """The managed section, verbatim. Pure, so a gate can read it."""
    body = doctrine_body(name, agent_type)
    return f"{doctrine_begin(version, body_hash(body))}\n{body}{DOCTRINE_END}\n"


def doctrine_body(name, agent_type):
    """Everything BETWEEN the markers. Hashed, so it is its own identity."""
    role = f"You are the fleet's **{agent_type}**.\n\n" if agent_type else ""
    return (
        f"# {name}\n\n"
        f"{role}"
        f"## Bus\n"
        f"BUS DOCTRINE: write ULTRA-TERSE -- fragments, no articles or filler,\n"
        f"ids/numbers/names exact, code and errors quoted verbatim. Write for AGENTS,\n"
        f"never for the ear: humans hear the writer's persona expansion; the raw text\n"
        f"stays the record.\n\n"
        f"Identity and credential come from the environment, never hardcoded:\n"
        f"$REVEILLE_AGENT_ROLE is your bus name, $REVEILLE_TOKEN your credential.\n"
        f"Your token does NOT name a room -- the broker maps it to your rooms\n"
        f"server-side.\n\n"
        f"Startup: `join()`, then `lessons()`, then `brief(role=\"...\")`. That is\n"
        f"the knowledge floor: rules the fleet already paid for, plus the doctrine,\n"
        f"contracts and decisions ranked to what you do.\n\n"
        f"Arm the watcher ONCE per session, with the Monitor tool:\n"
        f"command=\"wake-watch --follow $REVEILLE_AGENT_ROLE\", persistent=true. It\n"
        f"never exits and every line it prints is one ring. Where Monitor is not\n"
        f"available, fall back to Bash run_in_background=true:\n"
        f"`wake-watch $REVEILLE_AGENT_ROLE`, one ring per arm, re-armed after each.\n"
        f"Armed means the HARNESS is watching it: `wake-watch ... &` inside a Bash\n"
        f"call is an orphan that satisfies every check and rings nobody.\n\n"
        f"Per ring: `inbox()`, `ack()` everything, act only if owed, delete the\n"
        f"spool files you handled. Nothing owed -> silence is a valid turn.\n\n"
        f"On a `reason=\"swap-pending\"` ring: a new credential was minted for your\n"
        f"identity and is waiting to arrive. You are STILL the live body -- nothing\n"
        f"has been taken from you and nothing will be until it joins. Three acts, in\n"
        f"THIS ORDER, and the order is the whole point: the far side FETCHES before\n"
        f"it joins, and the moment it joins your credential is spent. So the note --\n"
        f"the one thing only you can write -- goes SECOND, not last.\n"
        f"1. COMMIT AND PUSH. Files do NOT travel -- only the identity does. Commit\n"
        f"   everything uncommitted to `wip/$REVEILLE_AGENT_ROLE/<utc-ts>` and push\n"
        f"   it. NEVER onto main, NEVER a force-push: this branch exists so the far\n"
        f"   side can fetch it, not so it can overwrite anything.\n"
        f"2. WRITE THE NOTE, IMMEDIATELY. Room is 8192 characters; aim for 2048.\n"
        f"   Over the soft line the write STILL LANDS and the result carries a\n"
        f"   condense nudge -- going over costs nothing but advice, so never let\n"
        f"   fear of a refusal shorten the note in a window seconds wide.\n"
        f"   `memory_add(kind=\"state\", ...)` with FIVE FIELDS: task, the wip BRANCH\n"
        f"   and SHA, next step, open threads, what is undone. If you could NOT push,\n"
        f"   say so exactly: \"unpushed at <host>:<path>\", so the new body knows the\n"
        f"   work is stranded rather than assuming it came.\n"
        f"3. VERIFY THE PUSH and post the five fields to the room. Verification is\n"
        f"   last because it is the only step that can wait: if the swap lands\n"
        f"   mid-note your credential keeps these two writes for five minutes and\n"
        f"   NOTHING else -- so spend that grace on the note, never on a read.\n"
        f"The new body FETCHES that branch before it does anything else. Then carry\n"
        f"on: if the swap never arrives, nothing about your situation changed.\n\n"
        f"On a `reason=\"recalled\"` or `reason=\"not-arrived\"` ring: the credential\n"
        f"in THIS directory is a successor that has not landed. `join()` -- that call\n"
        f"IS the arrival, it commits the swap, and until it happens the identity is\n"
        f"still the other body and nothing else here will work.\n\n"
        f"THE BODY IN WAITING (rulings 12445/12526): join() refused and no ring\n"
        f"explains it -> read your own spool first (~/.reveille/spool; a ring's\n"
        f"`reason` is the system speaking, but reason=idle-nudge says nothing),\n"
        f"then act on the refusal: `reveille knock`, `reveille init`, or stay\n"
        f"idle. Do NOT reconstruct your state from anything else -- not files,\n"
        f"not logs, not git history. Idle is a valid life.\n\n"
        f"WHO HEARS WHAT: a unicast (`to=\"<name>\"`) WAKES that agent. Your\n"
        f"REPLY-broadcast on a thread rings that thread's agent authors -- unless\n"
        f"they already read, and never past 40 agent messages in the ROOM with no\n"
        f"human speaking in it (then nothing rings until a human does). Your PARENTLESS\n"
        f"broadcast (`to=\"*\"`, no reply_to) does not wake anyone -- it is read\n"
        f"on each recipient's next turn. A HUMAN's broadcast rings the room. So:\n"
        f"needed now -> unicast the one who owes it. Broadcast only when a shared\n"
        f"contract changed or you block several peers.\n\n"
        f"Full reference: `usage()`.\n"
        )


def sync_claude_md(workdir, name, agent_type, version=__version__):
    """Write or refresh the managed doctrine block in CLAUDE.local.md.

    Returns (path, what) where what is 'created' | 'updated' | 'repaired' |
    'appended' | 'unchanged'.

    CLAUDE.local.md, NOT CLAUDE.md (architect 12167). Claude Code loads both at
    session start, but CLAUDE.md is the PROJECT's file -- tracked, shared, and
    written by whoever owns the repo. This block is PER-AGENT: it carries the
    agent's own name and role, so in a shared checkout two people's agents would
    overwrite each other's block in a tracked file and commit the fight. The
    .local.md variant is the documented per-developer, untracked home, which is
    exactly what a per-agent block is.

    NEVER an overwrite of somebody's file: outside the markers, every byte the
    directory already had survives, in place. Between them, this owns the text --
    which is what makes a later boot able to correct a doctrine that has moved on
    without asking a human to merge prose by hand.
    """
    path = pathlib.Path(workdir) / "CLAUDE.local.md"
    body = doctrine_body(name, agent_type)
    block = doctrine_block(name, agent_type, version)
    if not path.exists():
        path.write_text(block)
        return path, "created"
    text = path.read_text()
    m = _MARKER_RE.search(text)
    j = text.find(DOCTRINE_END)
    if m is None or j == -1 or j < m.start():
        sep = "" if text.endswith("\n\n") else ("\n" if text.endswith("\n") else "\n\n")
        path.write_text(text + sep + block)
        return path, "appended"
    # WHERE THE BLOCK ENDS, MEASURED (architect nit on #123): the end marker plus
    # its newline IF there is one. Assuming the newline eats the first byte after
    # the marker in a hand-edited file -- and this whole design exists to promise
    # that nothing outside the markers is ever touched.
    end = j + len(DOCTRINE_END)
    if text[end:end + 1] == "\n":
        end += 1
    found_body = text[m.end():j]
    if found_body.startswith("\n"):
        found_body = found_body[1:]

    # THE THREE CASES (operator, 2026-08-19). The marker CLAIMS a hash; the bytes
    # HAVE a hash; and there is the hash we would write. Comparing all three is
    # what separates "doctrine moved on" from "someone edited inside the markers"
    # -- and only the second one is silent under a version-only check.
    claimed = (m.group("sha") or "")
    actual = body_hash(found_body)
    expected = body_hash(body)
    if actual != claimed:
        path.write_text(text[:m.start()] + block + text[end:])
        return path, "repaired"
    if actual == expected and (m.group("v") or "") == version:
        return path, "unchanged"
    path.write_text(text[:m.start()] + block + text[end:])
    return path, "updated"


def lift_doctrine_from_claude_md(workdir):
    """Remove a doctrine block an EARLIER init wrote into CLAUDE.md.

    Migration, idempotent (architect 12167). Before this version the block lived
    in the project's own tracked CLAUDE.md; leaving it there would mean two
    blocks claiming to be the doctrine, and the stale one is in the file a
    reviewer is more likely to read. Only ever removes text BETWEEN our own
    markers -- a CLAUDE.md that never had one is not touched, and neither is a
    single byte outside them.
    """
    path = pathlib.Path(workdir) / "CLAUDE.md"
    if not path.exists():
        return None
    text = path.read_text()
    m = _MARKER_RE.search(text)
    j = text.find(DOCTRINE_END)
    if m is None or j == -1 or j < m.start():
        return None
    end = j + len(DOCTRINE_END)
    if text[end:end + 1] == "\n":
        end += 1
    remainder = (text[:m.start()] + text[end:]).rstrip("\n")
    path.write_text(remainder + "\n" if remainder else "")
    return path


def warn_if_committable(workdir, path):
    """Say so when a per-agent file could be committed. Returns the warning, or "".

    THE HONEST HALF OF THE IGNORE STORY. `.claude/.gitignore` covers the
    credential because the credential lives in a directory this installer
    creates. CLAUDE.local.md lives at the PROJECT ROOT, where a .gitignore of
    ours cannot reach it -- the only files that could are the repo's own
    .gitignore (the project's, not ours) and .git/info/exclude (the user's git
    config, and a tool that writes there to protect its own mess is fixing the
    wrong layer -- operator, 2026-08-19).

    So this does not act. It tells the truth and names the one line that fixes
    it. A per-agent doctrine block committed to a shared repo is noise and a
    small identity leak, not a credential leak -- worth a warning, not worth
    reaching into somebody's git config.
    """
    rel = pathlib.Path(path).name
    r = subprocess.run(["git", "check-ignore", "-q", str(path)],
                       cwd=str(workdir), capture_output=True, text=True)
    if r.returncode == 0:
        return ""
    inside = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"],
                            cwd=str(workdir), capture_output=True, text=True)
    if inside.returncode != 0 or inside.stdout.strip() != "true":
        return ""
    return (f"warning: {rel} is per-agent and this directory is a git work "
            f"tree, so it can be committed. Add `{rel}` to that repo's "
            f".gitignore if it is shared.")


def cmd_init(a):
    url = a.url or os.environ.get("REVEILLE_URL", "")
    name = a.name or os.environ.get("REVEILLE_AGENT_ROLE", "")
    agent_type = a.type or ""
    # INTERACTIVE UNLESS TOLD OTHERWISE. Nothing here is required in advance; an
    # answer supplied as a flag or an env var skips its own prompt and nothing
    # else. --no-prompt is for a script that would rather fail than block.
    wizard = sys.stdin.isatty() and not a.no_prompt
    cookie = None
    # A CREDENTIAL IN THE ENVIRONMENT IS A CANDIDATE, NOT A CONFIGURATION.
    # Claude Code injects this directory's own settings.local.json env into
    # every shell it starts, so inside an agent directory $REVEILLE_TOKEN is
    # ALWAYS set -- to the credential the person is running the installer to
    # replace. Taking it as "already configured" skipped the login wizard,
    # re-wrote the dead token over itself and left the file's mtime moving
    # while its contents never changed: the operator minted five credentials
    # in a row, each superseding the last, and the directory kept the first.
    # So it is asked about first, and a token the BROKER REFUSES is treated as
    # absent -- the wizard offers a login, --no-prompt says why it stopped.
    # Only a refusal counts: silence from an unreachable broker must not throw
    # away a credential that is probably fine, which is what --force is for.
    # Resolved ONCE, here, because stdin cannot be read twice -- and resolved
    # before the wizard so the wizard can see whether there is anything usable.
    token = read_token(a.token)
    checked = None
    if (token and token == os.environ.get("REVEILLE_TOKEN", "").strip()
            and url and name):
        ok, said = verify(url, name, token)
        checked = (token, ok, said)          # asked ONCE; the gate below reuses it
        if ok is False:
            # STDERR, WITH ITS SIBLINGS (ruling 12944 R-B b1). A diagnostic,
            # and every REFUSING sentence around it already goes to stderr; on
            # stdout it was block-buffered under the entrypoint's 2>&1 capture
            # and flushed LAST, so the true verdict trailed "no sign-in
            # stored" and a first-line quote handed the reader the wrong
            # remedy. Fixed at the source -- no env var to remember at the
            # next call site.
            print(f"reveille: this directory's credential no longer works "
                  f"({said})."
                  + (" --force: kept anyway, unverified -- a body that was moved "
                     "off parks on exactly this secret and trades it for a live "
                     "one at the return ticket."
                     if a.force else
                     " It will not be reinstalled -- mint a new one."),
                  file=sys.stderr)
    usable = bool(token) and not (checked and checked[1] is False)
    # A REFUSED CREDENTIAL IS A FACT TO RECORD, NOT A REASON TO REPLACE
    # (architect R2, ruling 12851). init replaces a credential only when it has
    # NONE or a human asked (--login / the wizard) -- so --force with a token in
    # hand keeps that token and falls through to the verify gate below, which
    # already installs under force and refuses without it.
    #
    # DELIBERATELY NOT FOLDED INTO `usable`: the wizard must go on reading a
    # refused token as absent, or a person re-running the installer to replace a
    # dead credential would be handed back the dead one. This flag is read by
    # the mint gate and nothing else.
    #
    # MEASURED 2026-08-20 (rev-tmelhiser-red-shirt-01): a container moved to
    # another machine boots holding the SUPERSEDED secret; the broker answers
    # 401; init diverted to the mint path, which needs a human sign-in no
    # container has; `set -e` in docker/entrypoint.sh turned that into exit 1,
    # so waked never spawned and the DES-012 s14 return ticket could never be
    # claimed. --force is the entrypoint's own fallback and it could not work.
    keep_refused = bool(token) and a.force
    if wizard and not (url and name and usable):
        print("reveille: setting this machine up as an agent.\n")
        url = url or ask("broker url", DEFAULT_URL)
        if not name:
            # SIGN IN FIRST, THEN PICK: the account's own agents are the menu, so
            # becoming the native body of an existing agent is a number, not a
            # name remembered exactly (operator ask, 2026-08-17). ONE sign-in per
            # machine (DES-022): the stored session answers here if it still
            # works, and the link is printed only when it does not -- the session
            # is handed on to the mint below either way.
            print("Signing in as YOURSELF -- the web account that owns (or will "
                  "own) the agent.")
            try:
                if a.login:
                    a.user = (a.user or os.environ.get("REVEILLE_USER")
                              or ask("your broker username"))
                    cookie = login(url, a.user, read_password(a.user))
                else:
                    cookie, a.user = session_cookie(url)
                name, is_new = ask_agent(my_agents(url, cookie))
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                print(f"reveille init: REFUSING -- {e}\nNothing was installed.",
                      file=sys.stderr)
                return 1
            if is_new:
                agent_type, suggested = ask_type()
                name = ask("agent name", name or suggested)
                while not NAME_OK(name):
                    print("  names are letters, digits, _ and -, starting with a "
                          "letter or digit")
                    name = ask("agent name", suggested)
        elif not name:
            agent_type, suggested = ask_type()
            name = ask("agent name", suggested)
            while not NAME_OK(name):
                print("  names are letters, digits, _ and -, starting with a "
                      "letter or digit")
                name = ask("agent name", suggested)
    # Bound here so the deferred mint below reads them whether or not this block
    # runs -- `will_mint` is the only thing that decides it does.
    will_mint, user, keep, cookie = False, None, False, cookie
    minted_pending = False
    installed_new_credential = False
    if a.login or (not usable and not keep_refused):
        # MINT FIRST, then fall into exactly the same path as a pasted token.
        # One installer, not two: everything after this point cannot tell where
        # the credential came from, so there is one flow to get right.
        # WHOSE USERNAME: the operator read "broker username" and reasonably asked
        # whether it meant the agent they were creating. Two identities are in
        # play and only one has a password -- the prompt now says which, because
        # a person answering it is holding the only screen that could tell them
        # apart, and getting it wrong creates an agent named after them that
        # posts in the room under their own name.
        user = a.user or os.environ.get("REVEILLE_USER")
        if not url or not name:
            print("reveille init: needs REVEILLE_URL and an agent name "
                  "(REVEILLE_AGENT_ROLE or the second argument).", file=sys.stderr)
            return 2
        # A NEW IDENTITY WITH NO ROOMS NAMED IS A THROWAWAY (DES-022 s4, ruled
        # 12165). --create already says "yes, a new agent"; --rooms says which
        # bus it is for, and defaulting it to every room the owner is in makes a
        # typo reach everything. The wizard asks instead, so it is exempt.
        if a.create and not a.rooms and not wizard:
            print("reveille init --create: --rooms is required when creating a new "
                  "agent -- name the rooms it is for.", file=sys.stderr)
            return 2
        # A SESSION THIS RUN DID NOT CREATE OUTLIVES IT. --login's is the
        # installer's own and is closed after the mint; the machine's sign-in
        # is not, whether it was loaded here or by the wizard above.
        keep = not a.login
        if not cookie:
            try:
                if a.login:
                    # The password door, for a broker that still has one open.
                    if not user:
                        print(f"Creating the agent '{name}'.")
                        print("Now log in as YOURSELF -- the web account that will "
                              "OWN it. This is not the agent's name.")
                        user = input("your broker username: ")
                    cookie = login(url, user, read_password(user))
                else:
                    # DES-022 s4: THIS MACHINE'S SIGN-IN mints it. Missing,
                    # expired or revoked -> the link is printed here, inline, and
                    # the init carries on where it left off. That is the whole
                    # re-auth path, and there is no other.
                    cookie, user = session_cookie(url, no_prompt=a.no_prompt)
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                print(f"reveille init: REFUSING -- {e}\nNothing was installed.",
                      file=sys.stderr)
                return 1
        # THE SIGN-IN HAPPENS HERE; THE MINT DOES NOT. Everything above this
        # point either asks the person something or reads a session off disk --
        # none of it creates a credential on the broker, so a refusal below
        # still leaves nothing behind. The mint itself runs at the END of the
        # install (see `will_mint`), because it is the one act with a remote
        # consequence and it must not happen before the local steps that can
        # refuse.
        will_mint = True
    missing = [n for n, v in (("REVEILLE_URL", url), ("REVEILLE_AGENT_ROLE", name),
                              # not the token when one is about to be minted --
                              # demanding it here is what sent readers to the web
                              # UI for something the CLI now does (DES-022 s4)
                              ("REVEILLE_TOKEN", token if not will_mint else "-"))
               if not v]
    if missing:
        print(f"reveille init: missing {', '.join(missing)}.\n"
              f"  export REVEILLE_URL=<broker url>\n"
              f"  export REVEILLE_AGENT_ROLE=<your agent name>\n"
              f"  export REVEILLE_TOKEN=<the minted secret>\n"
              f"then re-run. The token is read from the environment or stdin and "
              f"never from the command line, so it stays out of your shell "
              f"history.", file=sys.stderr)
        return 2

    # RESOLVED ONCE, NEVER THE LITERAL (ruling 12401). The old fallback
    # `or "claude"` existed only to turn a missing binary into a
    # FileNotFoundError traceback -- at whichever of three call sites happened
    # to run first (the user-scope remove here, both runs in
    # register_mcp_local). Measured 2026-08-19: an interrupted claude
    # self-update deleted the binary, and every docker start of that container
    # died on the traceback instead of a sentence.
    claude = shutil.which(a.claude) if a.claude else shutil.which("claude")
    workdir = pathlib.Path(a.dir or os.getcwd()).resolve()

    # ONE DIRECTORY, ONE AGENT (measured 2026-08-19, and it cost an identity).
    # `reveille init <broker> native-reveille-devops` was run with no --dir from
    # a shell sitting in red-shirt-01's directory. It wrote devops' credential
    # over red-shirt's settings.local.json, so the NEXT session started there
    # read the file, believed it was devops, and called join() -- which is the
    # arrival. One agent therefore ARRIVED as another, superseding devops' real
    # body mid-turn and destroying the handover note that body was writing. Two
    # agents then spent an hour disagreeing about where devops lived.
    # The directory IS the agent, so a directory that already names one is a
    # fact, not a default to overwrite: name both sides and refuse. --force is
    # the deliberate override, because REPLACING an agent's body with another
    # is a real thing to want -- it just must never be what a wrong cwd does.
    # Before the mint (D4), so a refusal here leaves no credential behind.
    held = agent_of_directory(workdir)
    if held and name and held != name and not a.force:
        print(f"reveille init: REFUSING -- this directory is {held!r}; you are "
              f"installing {name!r}.\n"
              f"  {workdir}\n"
              f"The directory IS the agent: whatever is installed here is what a "
              f"session started here becomes, and its first join() ARRIVES as "
              f"that identity. Installing over {held!r} would hand this body to "
              f"{name!r} and displace {held!r} wherever it is now.\n"
              f"If you meant another directory, pass --dir. If you really mean "
              f"to replace {held!r} here, pass --force. Nothing was installed.",
              file=sys.stderr)
        return 1

    # VERIFY BEFORE INSTALLING ANYTHING. The credential is the one thing that can
    # be wrong in a way no amount of correct installation fixes, and finding out
    # first is what keeps a failure from leaving a half-configured machine: at
    # this point there is nothing to undo (ruling 5).
    # The environment's credential was already asked about above -- once is the
    # whole point, and a second probe of the same secret is a second chance for
    # a flaky network to answer differently about a token nothing has changed.
    # A CREDENTIAL ABOUT TO BE MINTED HAS NOTHING TO VERIFY: there is no token
    # yet, and the mint below either succeeds or refuses on its own terms.
    ok, said = (True, "") if will_mint else (
        checked[1:] if (checked and checked[0] == token) else verify(url, name, token))
    if not ok and not a.force:
        print(f"reveille init: REFUSING -- {said}.\n"
              f"  url:   {url}\n  agent: {name}\n"
              f"Nothing was installed. Check the broker url and the token, or "
              f"pass --force to install against a bus you cannot reach right now.",
              file=sys.stderr)
        return 1

    # A MISSING BINARY IS A SENTENCE, NEVER A TRACEBACK (ruling 12401). Two
    # worlds, told apart by what already stands in the directory: one that is
    # ALREADY CONFIGURED (this agent's credential and this directory's
    # registration both present) continues DEGRADED -- the claude-dependent
    # steps are skipped, everything local is still converged, and the exit is
    # 0 so a container entrypoint under set -e boots instead of crash-looping.
    # A first boot with nothing configured refuses hard, by name, before any
    # local step: there is nothing to degrade into.
    degraded = ""
    if not claude:
        if agent_of_directory(workdir) == name and mcp_registered(workdir):
            degraded = ("the claude binary was not found on PATH; this "
                        "directory's registration and credential already "
                        "stand, so the claude-dependent steps were skipped")
        else:
            print(f"reveille init: REFUSING -- the `claude` binary was not "
                  f"found on PATH{f' or at {a.claude}' if a.claude else ''}, "
                  f"and this directory is not already configured for "
                  f"{name!r}.\n"
                  f"The MCP registration is written by `claude mcp add-json`, "
                  f"so without the binary it cannot land, and a credential "
                  f"beside a registration that never landed looks configured "
                  f"and is not. Install claude (or pass --claude PATH), then "
                  f"re-run. Nothing was installed.", file=sys.stderr)
            return 1

    steps = []
    persisted = ensure_on_path()
    if persisted:
        steps.append(persisted)
    if degraded:
        steps.append(f"DEGRADED: {degraded}")
        # ON STDERR, BECAUSE THAT IS WHAT THE ENTRYPOINT CAPTURES (architect
        # blocking on #153): `init_said=$(reveille init ... 2>&1 >/dev/null)`
        # keeps stderr and discards stdout, so a degraded sentence on stdout
        # sets nothing and the row shows running for a body that cannot take a
        # turn. The steps line above stays for the human reading stdout; a
        # warning belongs on stderr anyway.
        print(f"reveille init: DEGRADED -- {degraded}", file=sys.stderr)
    else:
        # THE REGISTRATION LIVES IN THE DIRECTORY. A stale user-scope
        # registration is converged AWAY, never left: its ${VAR} headers expand
        # from the process env at connect time, before project settings env
        # exists, so it either authenticates as whatever the shell happened to
        # export or as nobody -- both wrong, and the second one silently. The
        # remove is idempotent and cheap; the local-scope add-json below is the
        # registration.
        subprocess.run([claude, "mcp", "remove", "--scope", "user", "reveille"],
                       capture_output=True, text=True)
        try:
            mcp_where = register_mcp_local(url, workdir, claude)
        except RuntimeError as e:
            print(f"reveille init: REFUSING at step 1 of 3 -- {e}\n"
                  f"Nothing else was installed: a Stop hook beside a directory "
                  f"whose MCP registration did not land looks configured and is "
                  f"not.", file=sys.stderr)
            return 1
        steps.append(f"mcp: {mcp_where}; headersHelper reads the credential "
                     f"from settings.local.json at connect time")
    # NOTHING PER-AGENT STAYS IN THE TREE. An earlier init put the registration
    # in <dir>/.mcp.json; two registrations for one server is how a body ends up
    # authenticating twice by different rules, so the old one is lifted here
    # rather than left for someone to notice.
    dropped = drop_project_mcp_entry(workdir)
    if dropped:
        steps.append(f"migrated: removed the reveille entry from {dropped}")

    hook_rc = install.main()
    if hook_rc != 0:
        print("reveille init: the Stop hook did not install. The MCP registration "
              "above stands -- this machine can reach the bus, but nothing will "
              "keep a waiter armed, so wake it by draining inbox() per turn until "
              "this is fixed.", file=sys.stderr)
        return 1
    steps.append("hook: installed")

    # THE MINT IS THE LAST ACT WITH A REMOTE CONSEQUENCE (ruling #126, regressed
    # in 0.2.186, re-ruled 12271). It used to run ~50 lines earlier, before the
    # MCP registration and the hook -- both of which can refuse. On 2026-08-19 a
    # refused `claude mcp add-json` printed "Nothing else was installed" while a
    # credential ALREADY EXISTED on the broker; and because a bound mint
    # supersedes the name's previous token, it also rang the working body with a
    # swap-pending, costing that agent a full handover cycle -- a push, a state
    # memory and a bus post -- for an install that never happened.
    # A refusal has to be true about the BROKER, not just about this disk. So
    # everything that can refuse locally has already run by here, and the only
    # steps after this one write files whose failure the message names.
    if will_mint:
        # SAY WHAT MINTING DOES TO AN EXISTING NAME. Binding supersedes this
        # account's previous token for that name, so re-running on a second
        # machine MOVES the agent rather than cloning it -- the first machine
        # goes dead on its next call. That is the design (one identity, one live
        # credential) and a person should read it before it happens, not after.
        if wizard:
            print(f"\nMinting a token bound to '{name}'. If that name already has "
                  f"one, it is superseded:\n  the machine holding it stops working "
                  f"on its next call. Two machines means two names.")
        try:
            token, attached, note, minted_pending = mint_token(
                url, user, None, name, a.rooms, a.tier,
                cookie=cookie, keep_session=keep,
                pick=(lambda: ask("rooms (numbers/names, Enter = yours)", ""))
                     if wizard else None,
                create=a.create,
                confirm_create=(lambda n: ask(
                    f"create NEW agent {n!r}? [y/N]", "N").lower().startswith("y"))
                    if wizard else None)
        except RuntimeError as e:
            print(f"reveille init: REFUSING -- {e}\nThe MCP registration and Stop "
                  f"hook above stand and NO credential was minted; re-run to "
                  f"finish.", file=sys.stderr)
            return 1
        steps.append(f"minted a token bound to {name}, "
                     f"rooms: {', '.join(attached)}{note}"
                     + (" -- PENDING: the body holding this identity keeps it "
                        "until a turn here calls join()" if minted_pending else ""))
        # A non-pending mint superseded the name's previous tokens IN THIS
        # CALL -- whatever secret a running daemon read at spawn is dead now.
        # A pending mint took nothing yet (12320 R5): the swap commits at
        # join(), so nothing is dead until then.
        installed_new_credential = not minted_pending

    try:
        path = write_credential(url, name, token, workdir)
    except RuntimeError as e:
        print(f"reveille init: REFUSING at the credential step -- {e}\n"
              f"The MCP registration and Stop hook above stand; re-run once the "
              f"file is fixed and init converges the rest.", file=sys.stderr)
        return 1
    steps.append(f"credential: {path} (0600) -- this directory IS the agent")
    ign, wrote = ignore_the_credential(workdir)
    if wrote:
        steps.append(f"ignored: {ign} -- the credential cannot be committed from here")
    warn = warn_if_committable(workdir, pathlib.Path(workdir) / "CLAUDE.local.md")
    if warn:
        steps.append(warn)
    lifted = lift_doctrine_from_claude_md(workdir)
    if lifted:
        steps.append(f"migrated: lifted the doctrine block out of {lifted} "
                     f"(it belongs in CLAUDE.local.md, which is not tracked)")
    # THE DAEMON GOES ONLY WHEN THE SECRET IT READ AT SPAWN IS NOW DEAD --
    # that is what 12008 meant, and the predicate says it directly (ruling
    # 13094). An init that installed no NEW credential retires no daemon:
    # nothing it wrote made the running daemon's secret stale, so a SIGTERM
    # here is not a re-key, it is a murder. Measured 2026-08-20 on the 0.2.25
    # container shape: the hoisted boot daemon (12882) took the flock seconds
    # before init's keep-refused --force path reached this line, retire_waked
    # killed it, the set-e supervisor died with it, and the body was deaf --
    # forever, for the parked population the daemon exists to serve. The
    # pending-mint case (12320 R5) and every kept-credential case fall out of
    # the one predicate; do not re-split them.
    retired = retire_waked(name) if installed_new_credential else ""
    if retired:
        steps.append(f"wake: {retired}")
    elif minted_pending:
        steps.append("wake: left the running daemon alone -- it holds the live "
                     "credential until this one arrives")
    else:
        steps.append("wake: left the running daemon alone -- nothing was "
                     "re-keyed here")
    # ALWAYS, wizard or not (operator 11879 + red-shirt, 2026-08-18): the paste
    # path skips every prompt, and an agent with no CLAUDE.md has no boot ritual
    # -- it comes up connected and doctrine-less, which is what red-shirt did.
    doc, what = sync_claude_md(workdir, name, agent_type)
    steps.append(f"doctrine: {doc} ({what}" +
                 (f"; role {agent_type}" if agent_type else "") +
                 ") -- the reveille block is managed, everything outside it is yours")

    print("\n".join(steps))
    print(f"\nbus answered: {said}")
    print(f"start working:  cd {workdir} && claude")
    print("  The credential lives in that directory's .claude/settings.local.json, "
          "so any session started THERE carries this identity -- and a session "
          "started elsewhere carries none: its Stop hook stays inert and nothing "
          "wakes it. One directory, one agent; run init in another directory to "
          "make another agent.")
    return 0


def directory_env(workdir):
    """The REVEILLE_* env block of the directory's settings.local.json, or {}.
    The knock runs OUTSIDE a session as easily as inside one, so it reads the
    same file the installer writes rather than trusting the process env alone;
    the process env fills any gap (a session already carries the identity)."""
    path = pathlib.Path(workdir) / ".claude" / "settings.local.json"
    try:
        env = json.loads(path.read_text()).get("env") or {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, AttributeError):
        env = {}
    out = {}
    for k in ("REVEILLE_URL", "REVEILLE_AGENT_ROLE", "REVEILLE_TOKEN"):
        out[k] = (env.get(k) or os.environ.get(k, "")).strip()
    return out


def post_knock(url, token, machine=None, timeout=10):
    """POST /recalls/request with the dead credential as the bearer. The
    broker's refusal is the only diagnostic, so an HTTP error surfaces the
    broker's own sentence, never a guess about it. `machine` is this run's
    own user@host:path (12626) -- the CLI is the one party standing in the
    directory, so it is the one that can say WHERE the knock is from; the
    owner's dialog shows it so they know which machine they are answering."""
    body = json.dumps({"machine": machine} if machine else {}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/recalls/request",
                                 data=body, method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            said = body.get("detail") or body.get("error") or str(e)
        except Exception:
            said = str(e)
        raise RuntimeError(said)


def cmd_knock(a):
    """THE CLEAN BODY MAY ASK TO BE BEAMED; IT MAY NEVER BEAM ITSELF (DES-012
    s18). Presents THIS directory's credential -- the dead one join() just
    refused -- and asks the owner to send the identity here. Records one row
    on the owner's rail and nothing else; their answer lands here on its own,
    through the same return ticket a parked daemon already claims."""
    # THE WORD MOVED, THE ACT DID NOT (12676/12682): hail is the human-facing
    # word, knock the shipped identifier -- one function, two spellings, and
    # the old one points at the new one instead of silently renaming.
    word = getattr(a, "word", "knock")
    if word == "knock":
        print("reveille knock: the word is now `reveille hail` -- same act, "
              "same answer; knock keeps working.", file=sys.stderr)
    where = os.path.abspath(a.dir or os.getcwd())
    env = directory_env(where)
    url, token = env["REVEILLE_URL"], env["REVEILLE_TOKEN"]
    if not url or not token:
        print(f"reveille {word}: this directory holds no credential to present "
              f"-- nothing to {word} with. Run it in the agent's directory.",
              file=sys.stderr)
        return 1
    # user@host:path -- so the owner's dialog can name WHICH machine is asking
    # (12626; two directories on one laptop cost the operator that decision).
    machine = f"{getpass.getuser()}@{socket.gethostname()}:{where}"
    try:
        out = post_knock(url, token, machine=machine)
    except RuntimeError as e:
        said = str(e)
        # THE DISAMBIGUATION (#158 review), specific fact BEFORE the shared
        # doctrine: the generic refusal tells every story-less body to
        # knock-init-or-idle, and this IS the knock -- without this line the
        # tool answers an instruction to run itself. Not redundant with the
        # broker text: the broker's sentence is correct and the CONTEXT is
        # what makes it a loop. Only the story-less case; a credential the
        # broker recognises gets its own sentence undecorated.
        if said.startswith("bad token"):
            print("reveille knock: knocking cannot help here -- this "
                  "credential has no story with the broker.", file=sys.stderr)
        print(f"reveille knock: the broker refused it -- {said}", file=sys.stderr)
        return 1
    reason = {"superseded": "this machine was the identity's body",
              "expired-unclaimed": "the move minted for this machine never "
                                   "arrived"}.get(out.get("reason"), out.get("reason"))
    print(f"{word}ed: asked the owner to send {out.get('agent')!r} back here "
          f"({reason}). Their answer lands on its own -- keep the daemon "
          f"running or take a turn here later. Nothing else to do; idle is a "
          f"valid life.")
    return 0


def post_claim(url, token, timeout=10):
    """POST /recalls/claim with the dead credential as the bearer -- the same
    exchange the parked daemon polls, one shot. 204 answers None: no ticket
    standing is the ordinary case, never a fault. An HTTP error surfaces the
    broker's own sentence, never a guess about it."""
    req = urllib.request.Request(url.rstrip("/") + "/recalls/claim",
                                 data=b"{}", method="POST",
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            if r.status == 204:
                return None
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode())
            said = body.get("detail") or body.get("error") or str(e)
        except Exception:
            said = str(e)
        raise RuntimeError(said)


def cmd_claim(a):
    """THE CLAIM MUST NOT DEPEND ON A DAEMON (ruling 12644). When the identity
    is live in another directory on this host, that body's waked holds the one
    per-agent slot -- the asking directory has no daemon to take an answered
    window, and every ticket keyed to it dies unclaimed. This verb is the
    existing claim with a human hand instead of a daemon: same route, same
    hash-keyed ticket, same five-minute window, no new authority.

    Deliberately explicit, never folded into join(): the MCP server holds its
    token from spawn, so a self-claim would succeed at the broker and still
    leave the session refused -- success reported, nothing changed for the
    caller. This verb says what it did and what remains: restart the session."""
    where = os.path.abspath(a.dir or os.getcwd())
    env = directory_env(where)
    url, token = env["REVEILLE_URL"], env["REVEILLE_TOKEN"]
    if not url or not token:
        print("reveille claim: this directory holds no credential to present "
              "-- nothing to claim with. Run it in the agent's directory.",
              file=sys.stderr)
        return 1
    try:
        out = post_claim(url, token)
    except RuntimeError as e:
        print(f"reveille claim: the broker refused it -- {e}", file=sys.stderr)
        return 1
    if not out:
        print("reveille claim: no ticket is standing for this directory's "
              "credential -- nothing claimed, nothing changed. The owner opens "
              "the window first (answer the hail on their rail); run this "
              "again inside its five minutes.", file=sys.stderr)
        return 1
    agent = out.get("agent_name") or env["REVEILLE_AGENT_ROLE"]
    secret = out.get("secret", "")
    try:
        write_credential(url, agent, secret, where)
    except (RuntimeError, OSError) as e:
        # The ticket is already spent at the broker; pretending otherwise is
        # the failure shape this verb exists to end. The secret stays off the
        # screen -- the remedy is a fresh window, which costs one click.
        print(f"reveille claim: claimed at the broker, but the credential "
              f"could not be written here -- {e}. Fix that, then ask the "
              f"owner to open the window again and re-run this.",
              file=sys.stderr)
        return 1
    # Same arrival semantics as the daemon's claim path: the credential is not
    # the arrival, join() is -- park the ring so the next armed session fires.
    spool.write_ring(agent, json.dumps(
        {"wake": True, "reason": "recalled",
         "detail": "this body holds a credential that has not landed -- "
                   "join() IS the arrival and commits the swap"}))
    print(f"claimed: a live credential for {agent} is written to "
          f"{where}/.claude/settings.local.json. RESTART the session there -- "
          f"a running one holds its token from spawn and cannot use this; "
          f"{agent} lands when a fresh session takes a turn and joins.")
    return 0


def cmd_login(a):
    """Sign this MACHINE in. One session, one file, every agent here minted
    from it (DES-022 s2)."""
    url = (a.url or read_auth().get("url") or os.environ.get("REVEILLE_URL", "")
           or (ask("broker url", DEFAULT_URL) if sys.stdin.isatty() and not a.no_prompt
               else ""))
    if not url:
        print("reveille login: which broker? Pass the url or set REVEILLE_URL.",
              file=sys.stderr)
        return 2
    try:
        d = write_auth(sign_in(url, no_prompt=a.no_prompt,
                               open_browser=not a.no_browser))
    except (RuntimeError, urllib.error.URLError, OSError) as e:
        print(f"reveille login: {e}", file=sys.stderr)
        return 1
    print(f"signed in as {d['user']} on {d['url']} -- {auth_file()} (0600).\n"
          f"`reveille init {d['url']} <agent>` now mints without asking again.")
    return 0


def cmd_logout(a):
    """End the machine's sign-in AND remove the file. Both, always: a deleted
    file with a live session leaves a credential the reader thinks is gone, and
    a revoked session with the file still there is a 401 at the next init."""
    d = read_auth()
    if not d:
        print(f"not signed in ({auth_file()} holds nothing).")
        return 0
    with contextlib.suppress(Exception):
        _post(d["url"], "/logout", {}, auth_cookie(d))
    auth_file().unlink(missing_ok=True)
    print(f"signed out of {d['url']} -- {auth_file()} removed.")
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
    i.add_argument("--dir", help="the agent's directory -- the credential lands in "
                                 "its .claude/settings.local.json and sessions "
                                 "started there carry the identity (default: the "
                                 "current directory)")
    i.add_argument("--claude", help="path to the claude binary")
    i.add_argument("--login", action="store_true",
                   help="mint through the PASSWORD door, for a broker that still "
                        "has one open. Reads the password from $REVEILLE_PASSWORD "
                        "or a prompt, never from a flag. Everywhere else the "
                        "machine's own sign-in mints (`reveille login`), which is "
                        "what happens by default when no token is supplied")
    i.add_argument("--user", help="broker username for --login (or $REVEILLE_USER)")
    i.add_argument("--rooms", help="comma-separated room names for the minted "
                                   "token. REQUIRED with --create; without it, "
                                   "defaults to every room your account is in")
    i.add_argument("--tier", default="state",
                   help="memory tier for the minted token (default: state, which "
                        "is least privilege -- everything else lands as a draft)")
    i.add_argument("--type", help="agent type (architect, senior-dev, ui-ux, "
                                  "devops, ...) -- seeds a starter CLAUDE.md")
    i.add_argument("--create", action="store_true",
                   help="deliberately create a NEW agent when the "
                        "name has no live identity. Without it, an unknown name "
                        "is refused (the wizard asks instead) -- attaching to an "
                        "existing agent never needs this")
    i.add_argument("--no-prompt", action="store_true",
                   help="never ask: fail on anything not supplied. For scripts "
                        "that would rather stop than block")
    i.add_argument("--force", action="store_true",
                   help="install even if the broker did not answer, or over a "
                        "directory that already belongs to a different agent")
    i.set_defaults(fn=cmd_init)
    kn = sub.add_parser("knock", help="ask the owner to send this directory's "
                                      "identity back -- the choice a refused "
                                      "join() offers a body in waiting")
    kn.add_argument("--dir", help="the agent's directory (default: the current one)")
    kn.set_defaults(fn=cmd_knock, word="knock")
    hl = sub.add_parser("hail", help="hail the ship: ask the owner to beam this "
                                     "directory's identity back down -- the word "
                                     "for what `reveille knock` does (12676)")
    hl.add_argument("--dir", help="the agent's directory (default: the current one)")
    hl.set_defaults(fn=cmd_knock, word="hail")
    cl = sub.add_parser("claim", help="take a standing return ticket by hand, "
                                      "with this directory's credential -- for "
                                      "when no daemon here can (the identity "
                                      "is live elsewhere on this host)")
    cl.add_argument("--dir", help="the agent's directory (default: the current one)")
    cl.set_defaults(fn=cmd_claim)
    lg = sub.add_parser("login", help="sign this machine in -- one link, one click, "
                                      "and every agent here mints from it")
    lg.add_argument("url", nargs="?", help="broker url (default: the stored one, "
                                           "then $REVEILLE_URL)")
    lg.add_argument("--no-browser", action="store_true",
                    help="print the link, do not open a browser (ssh, containers)")
    lg.add_argument("--no-prompt", action="store_true",
                    help="never ask: fail on anything not supplied")
    lg.set_defaults(fn=cmd_login)
    lo = sub.add_parser("logout", help="end this machine's sign-in and remove it "
                                       "from disk")
    lo.set_defaults(fn=cmd_logout)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
