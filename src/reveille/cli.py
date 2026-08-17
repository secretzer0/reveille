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
import re
import json
import os
import pathlib
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

from . import install


def read_token(args_token, stdin=None):
    """Environment, then stdin, then the flag nobody should use. Returns the token
    or None. The order is the security order, not a convenience order."""
    if os.environ.get("REVEILLE_TOKEN"):
        return os.environ["REVEILLE_TOKEN"].strip()
    stdin = sys.stdin if stdin is None else stdin
    if args_token == "-" or (args_token is None and not stdin.isatty()):
        return stdin.read().strip() or None
    return (args_token or "").strip() or None


def write_mcp_json(url, workdir):
    """The registration lives in the DIRECTORY too, as <workdir>/.mcp.json.

    The first cutover registered user-scope with ${VAR} headers and put the
    values in the directory's settings env block -- and the acceptance run
    caught the seam: Claude Code expands MCP headers from the process
    environment at connect time, BEFORE project settings env is injected, so
    the session's Bash saw the identity while the MCP headers expanded empty.
    Headers therefore ride a headersHelper -- reveille-headers, a console
    script this package ships -- which Claude Code runs with the project
    directory as cwd, fresh on every connect, reading the same
    settings.local.json the credential lands in. One file holds the secret;
    this one only names the mechanism, carries nothing sensitive, and is safe
    to commit. Same converge discipline as the credential write: rewrite the
    reveille entry to correct, preserve every other server, refuse a file
    that cannot be parsed."""
    path = pathlib.Path(workdir) / ".mcp.json"
    cfg = {}
    if path.exists():
        try:
            cfg = json.loads(path.read_text())
        except (ValueError, UnicodeDecodeError) as e:
            raise RuntimeError(
                f"{path} exists and is not valid JSON ({e}). It may name MCP "
                f"servers this installer does not own, so it is refused rather "
                f"than clobbered -- fix or remove it, then re-run.")
    cfg.setdefault("mcpServers", {})["reveille"] = {
        "type": "http",
        "url": url.rstrip("/") + "/mcp",
        "headersHelper": "reveille-headers",
    }
    path.write_text(json.dumps(cfg, indent=2) + "\n")
    return path


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
        return False, f"HTTP {e.code} -- the broker answered and refused this token"
    except Exception as e:
        return False, f"{e} -- the broker did not answer"


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
    # The directory's .mcp.json (write_mcp_json) is what carries the identity
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
    return path


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


def login(url, user, password):
    """One web session for the installer's few calls; the cookie, or a
    RuntimeError naming why the broker refused."""
    code, body, cookie = _post(url, "/login", {"name": user, "password": password})
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
            seen.setdefault(name, set()).update(
                r if isinstance(r, str) else r.get("name", "") for r in t.get("rooms") or [])
    return sorted((name, sorted(rooms)) for name, rooms in seen.items())


def ask_agent(agents, stdin=None):
    """Which agent this directory becomes: one of yours by number (this machine
    takes over its identity -- the token is rotated, the old body goes dead), or
    a NEW name typed in. Returns (name, is_new)."""
    if not agents:
        return "", True
    print("\nYour agents (pick one to make THIS directory its native body, "
          "or type a new name):")
    for i, (name, rooms) in enumerate(agents, 1):
        print(f"  {i}. {name:<28} {', '.join(rooms) or '(no rooms)'}")
    while True:
        pick = ask("agent (number, or a new name)", "1", stdin)
        if pick.isdigit() and 1 <= int(pick) <= len(agents):
            return agents[int(pick) - 1][0], False
        if any(pick == n for n, _ in agents):
            return pick, False
        if NAME_OK(pick):
            return pick, True
        print("  names are letters, digits, _ and -, starting with a letter or digit")


def mint_token(url, user, password, agent, rooms=None, tier="state", pick=None,
               create=False, confirm_create=None, cookie=None):
    """Log in (unless a session cookie is handed in), mint a token BOUND to
    `agent`, attach rooms. Returns (secret, rooms-attached, note) or raises
    RuntimeError with what to fix.

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
        want_ids = [r["id"] for r in ordered[:n_mine]]
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
    with contextlib.suppress(Exception):
        _post(url, "/logout", {}, cookie)

    note = ""
    if tok.get("superseded"):
        note = f" (superseded {len(tok['superseded'])} previous token(s) for {agent})"
    return tok["secret"], attached, note


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


def starter_claude_md(workdir, name, agent_type):
    """Seed a CLAUDE.md naming the role and the boot ritual, unless one exists.

    Otherwise "type" is a label that changes nothing, which is worse than not
    asking: the operator would answer a question whose answer had no effect. It
    never overwrites -- a directory that already has a CLAUDE.md has an opinion,
    and this is an installer rather than an editor.
    """
    path = pathlib.Path(workdir) / "CLAUDE.md"
    if path.exists():
        return None
    path.write_text(
        f"# {name}\n\n"
        f"You are the fleet's **{agent_type}**.\n\n"
        f"## Bus\n"
        f"Identity and credential come from the environment, never hardcoded:\n"
        f"$REVEILLE_AGENT_ROLE is your bus name, $REVEILLE_TOKEN your credential.\n"
        f"Your token does NOT name a room -- the broker maps it to your rooms\n"
        f"server-side.\n\n"
        f"Startup: `join()`, then `lessons()`, then `brief(role=\"...\")`. That is\n"
        f"the knowledge floor: rules the fleet already paid for, plus the doctrine,\n"
        f"contracts and decisions ranked to what you do.\n\n"
        f"Per ring: `inbox()`, `ack()` everything, act only if owed, delete the\n"
        f"spool files you handled, then re-arm `wake-watch $REVEILLE_AGENT_ROLE`.\n"
        f"Nothing owed -> silence is a valid turn.\n\n"
        f"Full reference: `usage()`.\n")
    return path


def cmd_init(a):
    url = a.url or os.environ.get("REVEILLE_URL", "")
    name = a.name or os.environ.get("REVEILLE_AGENT_ROLE", "")
    agent_type = a.type or ""
    # INTERACTIVE UNLESS TOLD OTHERWISE. Nothing here is required in advance; an
    # answer supplied as a flag or an env var skips its own prompt and nothing
    # else. --no-prompt is for a script that would rather fail than block.
    wizard = sys.stdin.isatty() and not a.no_prompt
    cookie = None
    if wizard and not (url and name and (a.token or os.environ.get("REVEILLE_TOKEN"))):
        print("reveille: setting this machine up as an agent.\n")
        url = url or ask("broker url", DEFAULT_URL)
        a.login = a.login or not (a.token or os.environ.get("REVEILLE_TOKEN"))
        if not name and a.login:
            # LOG IN FIRST, THEN PICK: the account's own agents are the menu, so
            # becoming the native body of an existing agent is a number, not a
            # name remembered exactly (operator ask, 2026-08-17). One password
            # prompt: the session is handed on to the mint below.
            print("Log in as YOURSELF -- the web account that owns (or will own) "
                  "the agent.")
            a.user = a.user or os.environ.get("REVEILLE_USER") or input("your broker username: ")
            try:
                cookie = login(url, a.user, read_password(a.user))
                agents = my_agents(url, cookie)
            except (RuntimeError, urllib.error.URLError, OSError) as e:
                print(f"reveille init: REFUSING -- {e}\nNothing was installed.",
                      file=sys.stderr)
                return 1
            name, is_new = ask_agent(agents)
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
    minted = ""
    if a.login:
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
        if not user and not cookie:
            print(f"Creating the agent '{name}'.")
            print("Now log in as YOURSELF -- the web account that will OWN it. "
                  "This is not the agent's name.")
            user = input("your broker username: ")
        if not url or not name:
            print("reveille init --login: needs REVEILLE_URL and an agent name "
                  "(REVEILLE_AGENT_ROLE or the second argument).", file=sys.stderr)
            return 2
        # SAY WHAT MINTING DOES TO AN EXISTING NAME BEFORE ASKING FOR A PASSWORD.
        # Binding supersedes this account's previous token for that name, so
        # re-running on a second machine MOVES the agent rather than cloning it
        # -- the first machine goes dead on its next call. That is the design
        # (one identity, one live credential) and it is the kind of thing a
        # person should read before it happens rather than after.
        if wizard:
            print(f"\nMinting a token bound to '{name}'. If that name already has "
                  f"one, it is superseded:\n  the machine holding it stops working "
                  f"on its next call. Two machines means two names.")
        try:
            token, attached, minted = mint_token(
                url, user, None if cookie else read_password(user), name, a.rooms, a.tier,
                cookie=cookie,
                pick=(lambda: ask("rooms (numbers/names, Enter = yours)", ""))
                     if wizard else None,
                create=a.create,
                confirm_create=(lambda n: ask(
                    f"create NEW agent {n!r}? [y/N]", "N").lower().startswith("y"))
                    if wizard else None)
        except RuntimeError as e:
            print(f"reveille init: REFUSING -- {e}\nNothing was installed.",
                  file=sys.stderr)
            return 1
        minted = f"minted a token bound to {name}, rooms: {', '.join(attached)}{minted}"
    else:
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
    persisted = ensure_on_path()
    if persisted:
        steps.append(persisted)
    if minted:
        steps.append(minted)
    # THE REGISTRATION LIVES IN THE DIRECTORY. A stale user-scope registration
    # is converged AWAY, never left: its ${VAR} headers expand from the process
    # env at connect time, before project settings env exists, so it either
    # authenticates as whatever the shell happened to export or as nobody --
    # both wrong, and the second one silently. The remove is idempotent and
    # cheap; the .mcp.json write below is the registration.
    subprocess.run([claude, "mcp", "remove", "--scope", "user", "reveille"],
                   capture_output=True, text=True)
    try:
        mcp_path = write_mcp_json(url, workdir)
    except RuntimeError as e:
        print(f"reveille init: REFUSING at step 1 of 3 -- {e}\n"
              f"Nothing else was installed: a Stop hook beside a directory "
              f"whose MCP registration did not land looks configured and is "
              f"not.", file=sys.stderr)
        return 1
    steps.append(f"mcp: {mcp_path} (project scope; headersHelper reads the "
                 f"credential from settings.local.json at connect time)")

    hook_rc = install.main()
    if hook_rc != 0:
        print("reveille init: the Stop hook did not install. The MCP registration "
              "above stands -- this machine can reach the bus, but nothing will "
              "keep a waiter armed, so wake it by draining inbox() per turn until "
              "this is fixed.", file=sys.stderr)
        return 1
    steps.append("hook: installed")

    try:
        path = write_credential(url, name, token, workdir)
    except RuntimeError as e:
        print(f"reveille init: REFUSING at the credential step -- {e}\n"
              f"The MCP registration and Stop hook above stand; re-run once the "
              f"file is fixed and init converges the rest.", file=sys.stderr)
        return 1
    steps.append(f"credential: {path} (0600) -- this directory IS the agent")
    if agent_type:
        seeded = starter_claude_md(workdir, name, agent_type)
        steps.append(f"role: {agent_type}" + (f", CLAUDE.md written to {seeded}"
                                              if seeded else
                                              " (CLAUDE.md already here, left alone)"))

    print("\n".join(steps))
    print(f"\nbus answered: {said}")
    print(f"start working:  cd {workdir} && claude")
    print("  The credential lives in that directory's .claude/settings.local.json, "
          "so any session started THERE carries this identity -- and a session "
          "started elsewhere carries none: its Stop hook stays inert and nothing "
          "wakes it. One directory, one agent; run init in another directory to "
          "make another agent.")
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
                   help="log in and mint the token instead of pasting one. Reads "
                        "the password from $REVEILLE_PASSWORD or a prompt, never "
                        "from a flag")
    i.add_argument("--user", help="broker username for --login (or $REVEILLE_USER)")
    i.add_argument("--rooms", help="comma-separated room names for --login "
                                   "(default: every room your account is in)")
    i.add_argument("--tier", default="state",
                   help="memory tier for the minted token (default: state, which "
                        "is least privilege -- everything else lands as a draft)")
    i.add_argument("--type", help="agent type (architect, senior-dev, ui-ux, "
                                  "devops, ...) -- seeds a starter CLAUDE.md")
    i.add_argument("--create", action="store_true",
                   help="with --login: deliberately create a NEW agent when the "
                        "name has no live identity. Without it, an unknown name "
                        "is refused (the wizard asks instead) -- attaching to an "
                        "existing agent never needs this")
    i.add_argument("--no-prompt", action="store_true",
                   help="never ask: fail on anything not supplied. For scripts "
                        "that would rather stop than block")
    i.add_argument("--force", action="store_true",
                   help="install even if the broker did not answer")
    i.set_defaults(fn=cmd_init)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
