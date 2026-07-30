#!/usr/bin/env python3
"""reveille-launch (DES-002 T2): the ONLY thing that touches docker.

The broker never gains docker awareness (G4); this is a separate host-side process
that owns the docker socket and reads the bus like any other client -- no new broker
surface. Provisioning takes the bound token as INPUT (operator mints in /ui, pipes it
here); the launcher holds NO standing broker credential and launcher.db NEVER persists
a token or a gate secret (R1) -- env dies with the container and re-provision prompts
again.

Tenancy (DES-005 P0): every container belongs to a (user, agent) pair --
rev-<user>-<agent>, so two users may both run a 'senior-dev'. Persistent state
is PER AGENT: data/<user>/<agent>/{claude,repos} bind-mounts to ~/.claude and
~/repos; two agents of one user share NOTHING on disk (want isolation? create
another agent). Renaming an agent would be a move of that root -- there is no
rename; destroy+create. Quotas (cpu/mem/pids + container cap) come from
QUOTA_DEFAULTS overlaid with the user's `quota` overrides. Restart policy is
`no`: reboot and crash both leave a container down, so 'running' always means
somebody meant it.

  reveille-launch new <user> <agent> <repo-url> [--broker URL] [--network N]
                                        [--image IMG] [--timeout 90] [--no-wait]
      Provision one agent container. Token read from stdin (piped) or prompted --
      NEVER argv (argv leaks; the wake-127 lesson is fleet law). Succeeds when the
      broker's presence shows the agent live+connected (health-by-presence, 3.2.5).
  reveille-launch ls
  reveille-launch stop <user> <agent> | start <user> <agent>
  reveille-launch destroy <user> <agent> [--purge]   --purge drops the data root
  reveille-launch quota <user> [--cpus N] [--mem 8g] [--pids N] [--max-containers N]
      Show (bare) or override (flags) one user's quotas.
  reveille-launch profile <user> [--agent A] [--claude-token] [--github-token]
                                 [--repo-url URL] [--claude-mode token|home-login]
                                 [--clear-claude] [--clear-github]
      Show (masked) or set stored credentials (DES-005 P2): claude setup-token
      or API key (told apart by prefix), github token, default repo URL --
      per-user globals, --agent for a per-agent override. Values arrive on
      stdin/prompt, land 0600 in data/<user>/profile.json (a sibling of the
      agent dirs, so no container mounts it), and are echoed by nothing.
      claude_mode=home-login: no token env at all; agents copy the user's
      `login` credential at boot (NOT zero-touch -- log in first).
  reveille-launch login <user>
      Interactive `claude /login` in the user's login home
      (data/<user>/claude-auth) -- ONE login shared by ALL that user's
      home-login agents as a boot-time COPY into each agent's own ~/.claude.
      Re-run anytime to switch subscription accounts, then RESTART agents to
      move them; agent homes stay otherwise 100% unique.
  reveille-launch grant <user> <agent> <grantee> [--mode viewer|driver] [--ttl 86400]
      Mint a per-grant URL token (docker exec attach-gate mint -- the secret never
      leaves the container) and record the grant. The token is PRINTED ONCE, never
      stored (4.5.2): re-issue is re-mint, never retrieval.
  reveille-launch grants [user] [agent]              list grant records
  reveille-launch revoke <user> <agent> <grant-id>   kill d-/v-<id> now; audit exact
  reveille-launch flip <user> <agent> on|off         multi-driver toggle (4.3)
  reveille-launch debug <user> <agent>           interactive --rm shell in the
      agent's EXACT environment (same image/mounts/credential env); run claude
      by hand to watch billing and login behave. Entrypoint bash by default.
  reveille-launch pin [--path ~/.reveille/launcher-src]
      Create or fast-forward the tree `serve` is SPAWNED from. Never a
      developer's checkout: spawning from one makes `git checkout` a deployment.
  reveille-launch sweep [--idle-hours 24]        ONE tick, now (the recurring
      one runs inside serve -- see --sweep-seconds there).
      The tick 4.6 assigns the launcher: kill d-*/v-* sessions whose grant is
      expired/revoked/mode-mismatched, harvest the gate's ATTACH lines, derive
      DETACH lines from sessions observed gone (observation time, stated as such),
      and STOP (never destroy) containers idle past the window -- no attached
      client, no session activity, no waiting ring (DES-005 7.1).
  reveille-launch serve [--host 127.0.0.1] [--port 8766] [--auth-url URL]
      The browser's path to the launcher (DES-005 P1). Every request's session
      cookie is forwarded to the BROKER's /me; the resolved name is the only
      user the request can touch -- no user parameter exists on the wire, so
      cross-user access is unrepresentable. Provision takes the bound broker
      token in the request body: that frame and the docker-run child env only,
      echoed nowhere, stored nowhere (P2 owns credential profiles).
  reveille-launch join-here <role> [--broker URL]
      Bootstrap THIS user's terminal for one agent identity (DES-003 2.4): env
      fragment (0600 -- the ONLY file the token ever touches; stdin, never argv),
      MCP registration (headers are env templates, no token in config), Stop
      hook, PATH links for wake/wake-watch/reveille-waked, spool dir. Same
      checklist container provisioning satisfies -- one list, two callers.

launcher.db: $REVEILLE_LAUNCH_DB or ~/.reveille/launcher.db. Container + grant
records only, never a secret and never a minted token. Audit log: audit.log next
to the db ($REVEILLE_LAUNCH_AUDIT overrides).

SERVING IT (DES-006 U2). `serve` refuses to start when the docker socket is
unreachable, takes a flock so one instance serves one data root, prints the
resolved data root and db, and binds 127.0.0.1 -- the proxy is the only way in.
Supervision is the Stop hook's blind flock-guarded spawn (no systemd), and it
spawns the launcher ONLY where an operator has declared where state lives:

    ~/.reveille/launcher.env       # sourced by the hook; no file, no spawn
      REVEILLE_LAUNCH_DATA=/srv/reveille/data
      REVEILLE_LAUNCH_DB=/srv/reveille/launcher.db

That file is the deliberate declaration: without it the data root would follow
whoever happened to start the process, and every agent's home would move the
day someone else ran the command (msg 8499).
"""
import argparse
import asyncio
import contextlib
import html
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import threading
import time
import typing
import urllib.error
import urllib.parse
import urllib.request

# The AGENT reaches the broker by container DNS on a shared docker network (4.2: the
# container has a PRIVATE interface, never the host's -- host networking would bind
# ttyd to every LAN interface and collide every container's 7681 on one namespace).
DEFAULT_BROKER = os.environ.get("REVEILLE_LAUNCH_BROKER", "http://reveille-server:8765")
# The LAUNCHER's own health poll runs on the host, so it reaches the broker's PUBLISHED
# port -- the same broker, a different route (reveille-server publishes 8765, 4.2).
DEFAULT_HEALTH = os.environ.get("REVEILLE_LAUNCH_HEALTH", "http://127.0.0.1:8765")
DEFAULT_NETWORK = os.environ.get("REVEILLE_LAUNCH_NETWORK", "reveille")
DEFAULT_IMAGE = os.environ.get("REVEILLE_AGENT_IMAGE", "reveille-agent:0.2.8")
# The image's agent uid/gid (docker/Dockerfile ARG UID default -- keep in
# lockstep; a future image change is one grep for AGENT_UID). Bind-mounted
# homes must belong to THIS uid, not to whoever ran the launcher: the two
# coincide only when the operator's uid happens to be 1000 (msg 8475), and the
# P1 daemon's will not.
AGENT_UID = 1000
AGENT_GID = 1000
# Per-agent persistent state (DES-005 sec 4, a7d389b): data/<user>/<agent>/{claude,repos}.
# The user segment exists for ownership and deletion ONLY -- two agents of one
# user share NOTHING on disk.
DEFAULT_DATA = os.environ.get(
    "REVEILLE_LAUNCH_DATA", os.path.expanduser("~/.reveille/data"))
ROLE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{1,63}\Z")

# Suggestions only -- the field takes any string, because a hardcoded list ages
# the day a model ships and an agent must never be blocked on this file being
# current. Blank means the account default, which is the right answer for most.
MODEL_SUGGESTIONS = ("claude-fable-5", "claude-opus-5", "claude-sonnet-5",
                     "claude-haiku-4-5-20251001")

# Tenancy defaults (DES-005 sec 6, sized for an agent that BUILDS): every value
# per-user overridable via the user_quotas table (`quota` subcommand).
# disk_gb is RECORDED and surfaced but not yet enforced.
# ponytail: disk enforcement needs fs project quotas or a storage-opt-capable
# driver; add at P4 hardening if the host fs cooperates.
QUOTA_DEFAULTS = {"cpus": 2.0, "mem": "8g", "disk_gb": 50, "pids": 512,
                  "max_containers": 5}

# Secrets ride the ENVIRONMENT of the docker-run child (docker reads `-e NAME` from the
# launcher's env), so their VALUES never appear in argv. This is the whole no-argv
# discipline in one list: names here, values in env, never the two together on a
# command line a `ps` can read.
ENV_PASSTHROUGH_SECRET = ("REVEILLE_TOKEN", "REVEILLE_GATE_SECRET")

# DES-003 2.4: ONE checklist, two callers. Container provisioning satisfies each
# step inside the image/entrypoint (noted per step); `join-here` executes the
# host-side equivalent. A new step added here without both halves is a visible
# gap in join-here's printed walk, not a silent drift.
PROVISION_CHECKLIST = (
    ("env", "identity + credential + broker URL in the environment"),
    #        container: docker -e names (docker_run_argv); host: env fragment 0600
    ("register", "MCP server registered (reveille alias, HTTP transport)"),
    #        container: entrypoint `claude mcp add`; host: the same command
    ("hook", "Stop hook installed (waked supervisor + wake-watch gate)"),
    #        container: baked into the image's claude home; host: install-hook
    ("path", "wake, wake-watch, reveille-waked on PATH"),
    #        container: uv tool install in the image; host: ~/.local/bin symlinks
    ("spool", "spool directory exists"),
    #        container: entrypoint mkdir; host: mkdir here
)


class LaunchError(Exception):
    """One operator-facing failure. The CLI turns it into die(); the HTTP API
    (P1) turns it into a 400 body -- same message, two front doors."""


def die(msg, code=2) -> typing.NoReturn:
    print(f"reveille-launch: {msg}", file=sys.stderr)
    raise SystemExit(code)


def container_name(user, agent):
    """rev-<user>-<agent> (DES-005 sec 6): namespaced so two users may both run
    a 'senior-dev'."""
    return f"rev-{user}-{agent}"


def data_root(user, agent, base=None):
    """This AGENT's home on the host. Nothing else ever mounts it: two agents of
    one user are as separate as two users (sec 4). Renaming an agent would be a
    MOVE of this path -- there is deliberately no rename; destroy+create."""
    return os.path.join(base or DEFAULT_DATA, user, agent)


def user_auth_root(user, base=None):
    """The per-USER login home (operator requirement, 2026-07-30): the ONE
    place a human performs `claude /login`, shared into every agent of that
    user as a boot-time COPY of the credentials file -- never as a shared
    mount. Agent homes stay 100% unique (identity confusion between agents
    sharing state is exactly what the hive exists to prevent); the login is
    the only thing they have in common, so re-logging-in here and restarting
    the agents moves the whole fleet to the newly logged-in account."""
    return os.path.join(base or DEFAULT_DATA, user, "claude-auth")


def docker_run_argv(user, agent, image, network, quotas,
                    boot_cmd=None, data_base=None, extra_env=(),
                    auth_mount=None):
    """The docker-run command as argv. `-e NAME` entries pass values BY NAME from the
    child's env, so no secret is ever a token in this list -- the test asserts exactly
    that. quotas is a resolved QUOTA_DEFAULTS-shaped dict. `--restart no` is explicit
    although it is docker's default: reboot and crash both leave the container down,
    so 'running' always means somebody meant it (sec 7.1). auth_mount (home-login
    mode) is the user's login home, mounted READ-ONLY at /run/reveille-auth; the
    entrypoint copies the credentials file into the agent's own home at every
    boot, so a restart picks up whatever account the user last logged in. Pure:
    no env read, no side effects."""
    root = data_root(user, agent, base=data_base)
    argv = [
        "docker", "run", "-d",
        "--name", container_name(user, agent),
        "--label", f"reveille.user={user}",
        "--label", f"reveille.agent={agent}",
        "--restart", "no",
        "--network", network,
        "--memory", quotas["mem"],
        "--cpus", str(quotas["cpus"]),
        "--pids-limit", str(quotas["pids"]),
        "-v", f"{os.path.join(root, 'claude')}:/home/agent/.claude",
        "-v", f"{os.path.join(root, 'repos')}:/home/agent/repos",
        "-e", "REVEILLE_AGENT_ROLE",
        "-e", "REVEILLE_URL",
        "-e", "REVEILLE_REPO_URL",
    ]
    if auth_mount:
        argv += ["-v", f"{auth_mount}:/run/reveille-auth:ro"]
    for name in ENV_PASSTHROUGH_SECRET:
        argv += ["-e", name]
    # There is deliberately NO ambient-Anthropic fallback here. This function
    # used to forward the launcher's own ANTHROPIC_API_KEY when the profile had
    # no claude token -- which made an env var nobody declared into a SILENT
    # BILLING-MODEL SWITCH: an operator who had ever exported an API key in the
    # shell that started the launcher got agents billing per token instead of
    # riding their subscription, invisibly, discoverable only on an invoice
    # (architect ruling, msg 8617). A billing credential is CHOSEN -- stored in
    # the profile, where the prefix names its kind -- never inherited.
    for name in extra_env:
        # P2 profile credentials: NAMES here, values in the child env -- the
        # same no-argv discipline as ENV_PASSTHROUGH_SECRET.
        argv += ["-e", name]
    argv.append(image)
    if boot_cmd:
        import shlex
        argv += shlex.split(boot_cmd)
    return argv


def resolve_quotas(row):
    """QUOTA_DEFAULTS overlaid with a user's override row (sqlite Row or dict or
    None); NULL columns keep the default. Pure."""
    q = dict(QUOTA_DEFAULTS)
    if row is not None:
        for k in QUOTA_DEFAULTS:
            v = row[k] if not isinstance(row, dict) else row.get(k)
            if v is not None:
                q[k] = v
    return q


def is_idle(attached, last_activity_ns, last_ring_ns, now_ns, window_ns):
    """The 24h-idle-stop decision (sec 7.1), pure. Idle = no attached tmux
    client AND no session activity AND no wake ring inside the window. An agent
    working autonomously overnight shows session activity (its panes update on
    every bus turn) and is never reclaimed -- that case is the point."""
    if window_ns <= 0 or attached:
        return False
    newest = max(last_activity_ns or 0, last_ring_ns or 0)
    return now_ns - newest >= window_ns


def health_from_presence(agents, role):
    """True iff SOME presence entry for the role is both live (recent heartbeat) and
    connected (real-time reachable: a wake waiter attached). A role can appear once per
    room, so we want any room where it is fully up, not just the first entry. Pure so
    the poll logic is testable without a broker."""
    return any(a.get("name") == role and a.get("live") and a.get("connected")
               for a in agents)


# ---- broker (read like any client; no new broker surface) --------------------------

def _presence(broker_url, role, token):
    req = urllib.request.Request(
        broker_url.rstrip("/") + "/presence",
        headers={"Authorization": f"Bearer {token}", "X-Agent": role})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.load(r).get("agents", [])


def wait_healthy(broker_url, role, token, timeout, poll=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if health_from_presence(_presence(broker_url, role, token), role):
                return True
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            pass
        time.sleep(poll)
    return False


# ---- launcher.db (container records; NEVER a secret) -------------------------------

def _migrate_launcher_db(conn):
    """Pre-P0 launcher.db keyed everything by bare role. Clean cutover to
    (user, agent): old rows become user='operator' -- they were all the
    operator's -- and the volume column dies with the named volumes."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(containers)")}
    if "role" not in cols:
        return
    conn.executescript("""
        ALTER TABLE containers RENAME TO c_old;
        ALTER TABLE grants RENAME TO g_old;
        ALTER TABLE sessions_seen RENAME TO s_old;
    """)
    _launcher_tables(conn)
    conn.execute("INSERT INTO containers(user, agent, repo_url, container, image, "
                 "broker_url, created_ns) SELECT 'operator', role, repo_url, "
                 "'rev-operator-'||role, image, broker_url, created_ns FROM c_old")
    conn.execute("INSERT INTO grants(id, user, agent, grantee, mode, issued_ns, "
                 "expiry_ns, revoked_ns) SELECT id, 'operator', role, grantee, "
                 "mode, issued_ns, expiry_ns, revoked_ns FROM g_old")
    conn.execute("INSERT INTO sessions_seen(user, agent, session, first_seen_ns) "
                 "SELECT 'operator', role, session, first_seen_ns FROM s_old")
    conn.executescript("DROP TABLE c_old; DROP TABLE g_old; DROP TABLE s_old;")
    conn.commit()


def _launcher_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS containers("
        "user TEXT NOT NULL, agent TEXT NOT NULL, repo_url TEXT, container TEXT, "
        "image TEXT, broker_url TEXT, created_ns INTEGER, "
        "PRIMARY KEY(user, agent))")
    # Grant records (4.5.2): metadata ONLY -- the minted token is signable solely
    # with the container's gate secret, which this process never persists. A db
    # that could reproduce a live token would be the standing-credential store
    # 3.1 forbids.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS grants("
        "id TEXT PRIMARY KEY, user TEXT, agent TEXT, grantee TEXT, mode TEXT, "
        "issued_ns INTEGER, expiry_ns INTEGER, revoked_ns INTEGER)")
    # What the sweep saw last tick, so a session OBSERVED GONE yields a DETACH
    # line (4.5.2: observation time, never a fabricated event time).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions_seen("
        "user TEXT, agent TEXT, session TEXT, first_seen_ns INTEGER, "
        "PRIMARY KEY(user, agent, session))")
    # Per-user quota overrides (DES-005 sec 6): NULL = QUOTA_DEFAULTS value.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS user_quotas("
        "user TEXT PRIMARY KEY, cpus REAL, mem TEXT, disk_gb INTEGER, "
        "pids INTEGER, max_containers INTEGER)")
    # WHO OWNS A NAME (ruling 8660). An agent name that carries messages,
    # memories and a state note is an IDENTITY; only its owner may resurrect it.
    # This table is DELIBERATELY not `containers`: a container is ephemeral by
    # design and destroy deletes its row, so ownership stored there dies with the
    # thing it outlives. Ownership is a fact about the PAST that nothing surviving
    # a destroy can re-derive -- the one category that must be written down.
    # Keyed on agent ALONE: a name has exactly one owner. FIRST provisioner, not
    # most recent, or the rule is only "whoever last touched it".
    conn.execute(
        "CREATE TABLE IF NOT EXISTS agent_owners("
        "agent TEXT PRIMARY KEY, user TEXT NOT NULL, "
        "first_provisioned_ns INTEGER NOT NULL, "
        "released_ns INTEGER, released_by TEXT)")
    # Backfill from containers ONCE, because a still-provisioned agent's owner is
    # derivable TODAY and unrecoverable the moment it is destroyed. This is the
    # only place the two tables ever touch, and it runs before the first destroy
    # that would take the fact with it. Already-erased names are past saving --
    # their container rows are gone, which is exactly the gap being closed.
    conn.execute(
        "INSERT OR IGNORE INTO agent_owners(agent, user, first_provisioned_ns) "
        "SELECT agent, user, created_ns FROM containers")
    conn.commit()   # open holds no write lock: a second opener must not block


def _db(path=None):
    path = path or os.environ.get(
        "REVEILLE_LAUNCH_DB", os.path.expanduser("~/.reveille/launcher.db"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='containers'").fetchone():
        _migrate_launcher_db(conn)
    _launcher_tables(conn)
    return conn


def _quotas_for(conn, user):
    return resolve_quotas(conn.execute(
        "SELECT * FROM user_quotas WHERE user=?", (user,)).fetchone())


def claim_agent_name(conn, user, agent, now_ns=None):
    """Record who owns this name, once, at provision time (ruling 8660).

    RECORDING IS URGENT, ENFORCING IS NOT: this writes the fact and returns the
    owner; it refuses nothing today, because the operator is the only user and
    the enforcement wants the UI half. But every agent provisioned before the
    enforcement exists has an ownership fact that is UNRECOVERABLE if it is not
    written at the time, so it is written at the time.

    First provisioner wins and re-provisioning does not move ownership. A
    RELEASED name is claimable again -- release is the key to this lock, and
    shipping the lock without it would leak every name a deleted account held."""
    now = time.time_ns() if now_ns is None else now_ns
    row = conn.execute("SELECT * FROM agent_owners WHERE agent=?",
                       (agent,)).fetchone()
    if row is None or row["released_ns"] is not None:
        conn.execute(
            "INSERT OR REPLACE INTO agent_owners"
            "(agent, user, first_provisioned_ns, released_ns, released_by) "
            "VALUES(?,?,?,NULL,NULL)", (agent, user, now))
        conn.commit()
        return {"agent": agent, "user": user, "first_provisioned_ns": now}
    return {"agent": agent, "user": row["user"],
            "first_provisioned_ns": row["first_provisioned_ns"]}


def release_agent_name(conn, agent, by, now_ns=None):
    """Free a name for someone else to claim. Returns the prior owner, or None
    if the name was unowned or already released. Audited by the caller: an
    ownership transfer nobody can point at afterwards is not a transfer."""
    now = time.time_ns() if now_ns is None else now_ns
    row = conn.execute("SELECT * FROM agent_owners WHERE agent=?",
                       (agent,)).fetchone()
    if row is None or row["released_ns"] is not None:
        return None
    conn.execute("UPDATE agent_owners SET released_ns=?, released_by=? "
                 "WHERE agent=?", (now, by, agent))
    conn.commit()
    return row["user"]


def _record(conn, user, agent, repo_url, image, broker_url):
    conn.execute(
        "INSERT OR REPLACE INTO containers"
        "(user, agent, repo_url, container, image, broker_url, created_ns) "
        "VALUES(?,?,?,?,?,?,?)",
        (user, agent, repo_url, container_name(user, agent), image,
         broker_url, time.time_ns()))
    conn.commit()


# ---- audit (4.5.2): one line per attach/detach/revoke -- who, container, mode,
# timestamp. ATTACH lines are the GATE's (it alone holds the verified grant id
# before exec) and are harvested on the sweep tick with their original
# timestamps. DETACH is what the LAUNCHER observed -- the line says so, because
# an inferred-and-backdated detach is a log that lies. REVOKE/KILL are launcher
# actions, stamped at the moment they act, exact.

def audit_line(ts_iso, verb, **fields):
    """Pure formatter: greppable `<ts> <VERB> k=v ...` line."""
    kv = " ".join(f"{k}={v}" for k, v in fields.items())
    return f"{ts_iso} {verb} {kv}".rstrip()


def _audit_path():
    db = os.environ.get(
        "REVEILLE_LAUNCH_DB", os.path.expanduser("~/.reveille/launcher.db"))
    return os.environ.get(
        "REVEILLE_LAUNCH_AUDIT", os.path.join(os.path.dirname(db), "audit.log"))


def _audit(verb, ts_iso=None, **fields):
    ts_iso = ts_iso or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    path = _audit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a") as f:
        f.write(audit_line(ts_iso, verb, **fields) + "\n")


# ---- sweep decisions (pure, so the tick's judgement is testable without docker) -----

def sweep_actions(grants, live, seen, now_ns):
    """grants: {id: {mode, expiry_ns, revoked_ns}}; live: {session: attached?}
    this tick; seen: session-name set from last tick. Returns (kills, detaches):
    kills as (session, reason) for live sessions whose grant no longer justifies
    them -- expiry that only gates the doorway is not expiry (4.6) -- detaches
    as sessions observed gone. A killed session is OUR action (exact KILL line),
    never also a DETACH."""
    kills = []
    for s in sorted(live):
        mode = "driver" if s.startswith("d-") else "viewer"
        g = grants.get(s[2:])
        if g is None:
            kills.append((s, "no-grant"))
        elif g["revoked_ns"]:
            # The signed token outlives a revoke (offline verify has no
            # revocation list); a re-attach after revoke lives at most one tick.
            kills.append((s, "revoked"))
        elif now_ns >= g["expiry_ns"]:
            kills.append((s, "expired"))
        elif g["mode"] != mode:
            kills.append((s, "mode-mismatch"))
        elif not live[s] and s in seen:
            # destroy-unattached reaps on detach, so a session unattached for a
            # FULL tick is an orphan from a failed attach (client died between
            # new-session and attach-session). Left alone it holds driver
            # exclusivity until the grant expires.
            kills.append((s, "orphan"))
    return kills, sorted(set(seen) - set(live))


# ---- helpers -----------------------------------------------------------------------

def read_secret(prompt):
    """Token from a pipe (stdin not a tty) or an interactive prompt -- never argv."""
    if not sys.stdin.isatty():
        tok = sys.stdin.readline().strip()
        if tok:
            return tok
    import getpass
    return getpass.getpass(f"{prompt}: ").strip()


def _docker(*args, check=True, capture=False):
    # capture=True takes stderr too: probe-style calls (kill-session on a name
    # that may not exist, network create on an existing one) are expected to
    # fail quietly, not leak daemon noise into the CLI's output.
    return subprocess.run(
        ["docker", *args], check=check,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None, text=True)


def _exists(name):
    return _docker("inspect", name, check=False, capture=True).returncode == 0


def docker_probe_error(rc, stderr):
    """The startup docker probe's verdict, pure so the refusal is testable
    without a broken docker (msg 8499): rc 0 is fine, anything else refuses.
    A launcher that cannot reach the socket must not serve -- a health
    endpoint returning 200 while provisioning is structurally impossible is
    the 'looks fine, does nothing' shape we keep paying for. Returns the
    operator-facing message, or None when the socket is reachable."""
    if rc == 0:
        return None
    err = (stderr or "").strip()
    hint = ("the launcher's user is not in the docker group -- add it "
            "(usermod -aG docker <user>) and start a new login session"
            if "permission denied" in err.lower() else
            "is the docker daemon running, and is DOCKER_HOST correct?")
    return (f"reveille-launch: cannot reach the docker socket ({hint}).\n"
            f"docker said: {err or f'exit {rc}'}")


def _require_docker():
    """Refuse to serve without the socket. `docker version` is the cheapest
    call that actually round-trips to the daemon (`docker --version` does
    not -- it answers from the client alone and would pass on a host with no
    daemon at all)."""
    p = _docker("version", "--format", "{{.Server.Version}}",
                check=False, capture=True)
    msg = docker_probe_error(p.returncode, p.stderr)
    if msg:
        raise SystemExit(msg)
    return (p.stdout or "").strip()


def _singleton(lock_path):
    """One serving launcher per data root, the waiter's discipline (DES-003
    2.3): the flock IS the guard, so a supervisor may spawn blindly and the
    loser exits itself. The fd stays open for the process's life on purpose;
    closing it would drop the lock."""
    import fcntl
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    fd = open(lock_path, "w")   # noqa: SIM115 -- held for the process lifetime
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise SystemExit(
            f"reveille-launch: another launcher already holds {lock_path} "
            "-- this one exits (the running instance keeps serving)")
    return fd


# ---- credential profiles (DES-005 P2) ----------------------------------------------
# Per-user globals + per-agent overrides for the CLAUDE credential, the GITHUB
# token, and the default repo URL. Stored launcher-side at
# data/<user>/profile.json -- 0600, and a SIBLING of the agent dirs, so no
# container ever mounts it. The broker db never sees it; no API response ever
# echoes a value (masked to set/absent). A container still receives its own
# user's credentials via env (sec 7.1: the user's own credential doing the
# user's own work -- stated, not hidden).

def profile_path(user, base=None):
    return os.path.join(base or DEFAULT_DATA, user, "profile.json")


def load_profile(user, base=None):
    try:
        with open(profile_path(user, base)) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def save_profile(user, prof, base=None):
    """0600 at open (never chmod-after), user root 0700 first -- the same
    discipline as join-here's env fragment."""
    path = profile_path(user, base)
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(path), 0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(prof, f)


CLAUDE_MODES = ("token", "home-login")


def merge_profile(prof, updates, agent=None):
    """Apply updates (None value = clear) to the globals, or to one agent's
    override block. Pure; returns the new profile. claude_mode is validated
    HERE because both the CLI and the HTTP PUT flow through this function --
    a mode is a CHOICE (msg 8629), and an unknown choice must be refused at
    write time, not discovered as a silent default at provision time."""
    prof = json.loads(json.dumps(prof))   # deep copy, no surprises
    target = prof
    if agent is not None:
        target = prof.setdefault("agents", {}).setdefault(agent, {})
    if updates.get("claude_mode") and updates["claude_mode"] not in CLAUDE_MODES:
        raise LaunchError(f"claude_mode must be one of {CLAUDE_MODES}, "
                          f"not {updates['claude_mode']!r}")
    for k in ("claude_token", "github_token", "repo_url", "claude_mode"):
        if k in updates:
            if updates[k]:
                target[k] = updates[k]
            else:
                target.pop(k, None)
    return prof


def resolve_credentials(prof, agent, repo_url_req=""):
    """Per-agent override > user global; an explicit repo_url on the request
    outranks both (it is the most specific statement of intent). Pure."""
    a = (prof.get("agents") or {}).get(agent) or {}

    def pick(k):
        return a.get(k) or prof.get(k) or None

    return {"claude_token": pick("claude_token"),
            "github_token": pick("github_token"),
            "claude_mode": pick("claude_mode") or "token",
            "repo_url": repo_url_req or pick("repo_url") or ""}


def credential_kind(token):
    """The BILLING KIND of a claude credential, for responses and audit lines:
    the kind is reportable, the value never is. Empty token = no claude
    credential at all (a probe container that never runs claude). Pure."""
    if not token:
        return "none"
    return ("api-key" if token.startswith("sk-ant-api") else "subscription-token")


def credential_env(creds):
    """(env_names, env_values, kind) a container or debug shell receives for
    the resolved credentials. Pure. The ONE place the claude_mode choice takes
    effect (msg 8629): in home-login mode NO claude env var is passed at all,
    even when a token is also stored -- claude's own precedence would let the
    env var shadow the file credential the operator logged in, which is the
    silent-billing-switch defect class wearing the fix as a disguise. The
    credential in that mode is the file `claude /login` wrote into the agent's
    persisted home; if it is absent, the agent boots to a visible login
    prompt, which is honest. kind is the reportable word for audit lines and
    responses: home-login | api-key | subscription-token | none."""
    names, env = [], {}
    if creds.get("claude_mode") == "home-login":
        kind = "home-login"
    else:
        kind = credential_kind(creds["claude_token"])
        if creds["claude_token"]:
            names.append(claude_env_name(creds["claude_token"]))
            env[names[-1]] = creds["claude_token"]
    if creds["github_token"]:
        names.append("GITHUB_TOKEN")
        env["GITHUB_TOKEN"] = creds["github_token"]
    return names, env, kind


def claude_env_name(token):
    """API keys and setup-token OAuth tokens share one profile field, told
    apart by prefix (sec 3): sk-ant-api... is a key, anything else rides
    CLAUDE_CODE_OAUTH_TOKEN. Pure."""
    return ("ANTHROPIC_API_KEY" if token.startswith("sk-ant-api")
            else "CLAUDE_CODE_OAUTH_TOKEN")


# ~/.claude.json is CONTAINER-LOCAL (the bind mounts cover ~/.claude and
# ~/repos, not the file beside them), so a fresh debug container boots claude
# into the first-run wizard: theme picker, then a LOGIN flow -- and completing
# that login would write new credentials into the agent's SHARED mounted home.
# The managed entrypoint (docker/entrypoint.sh) seeds these same keys for the
# same reason; this is that seed, minus settings.json (which lives in the
# persisted home and is already there). setdefault-merge, then exec bash: an
# operator re-entering a container they configured keeps their choices.
_SEED_BASE = """\
import json, os, pathlib
p = pathlib.Path.home() / ".claude.json"
try:
    d = json.loads(p.read_text())
except Exception:
    d = {}
d.setdefault("hasCompletedOnboarding", True)
d.setdefault("theme", "dark")
d.setdefault("autoMode", True)
d.setdefault("autoModeOptInDismissed", True)
pr = d.setdefault("projects", {})
for path in ("/home/agent/repos", "/home/agent"):
    pr.setdefault(path, {}).setdefault("hasTrustDialogAccepted", True)
p.write_text(json.dumps(d, indent=2))
"""
_DEBUG_SEED = _SEED_BASE + 'os.execlp("bash", "bash")\n'


def debug_argv(user, agent, image, network, extra_env=(), entrypoint="bash",
               data_base=None):
    """`docker run --rm -ti` mirroring what provision gives the agent -- same
    image, same network, same two mounts, same credential env BY NAME -- but
    interactive, foreground, self-removing, and named apart from the managed
    container. This exists so an operator can sit INSIDE the exact environment
    an agent gets and watch claude behave (billing, login, model) instead of
    inferring it from the outside: reveille-launch debug <user> <agent>.
    Entrypoint defaults to bash ON PURPOSE -- the debugging question is "what
    does claude do in this env", so the operator runs claude by hand; the full
    entrypoint would also demand a bus token and spawn the waiter, which is
    plumbing noise around a billing question. The default bash arrives AFTER
    the _DEBUG_SEED first-run seed (see above): without it claude opens the
    wizard whose login step can rewrite the agent's shared credentials.
    An explicit --entrypoint runs raw. Pure: no env read."""
    root = data_root(user, agent, base=data_base)
    argv = ["docker", "run", "--rm", "-ti",
            "--name", f"{container_name(user, agent)}-debug",
            "--network", network,
            "-v", f"{os.path.join(root, 'claude')}:/home/agent/.claude",
            "-v", f"{os.path.join(root, 'repos')}:/home/agent/repos"]
    if entrypoint == "bash":
        tail = [image, "-c", _DEBUG_SEED]
        argv += ["--entrypoint", "python3"]
    else:
        tail = [image]
        argv += ["--entrypoint", entrypoint]
    for name in extra_env:
        argv += ["-e", name]     # names only -- values ride the child env
    argv += tail
    return argv


def _refuse_if_running(user, agent, force, doing):
    managed = container_name(user, agent)
    if _docker("inspect", "-f", "{{.State.Running}}", managed,
               check=False, capture=True).stdout.strip() == "true":
        # Same ~/.claude, two claudes: shared sqlite state and stepped-on locks.
        # REFUSE rather than warn-and-proceed. A warning printed immediately
        # before exec'ing into a tty is read after the damage, and what is at
        # risk here is the agent's persisted ~/.claude -- the thing the destroy
        # modal and the retire/erase split exist to protect. Every other guard
        # in this file refuses (deploy-preflight, server-image, the
        # credential-less boot); a debug tool aimed at a billing investigation
        # is not the place to make an exception, because the operator running it
        # is by definition not yet sure what is going on. For `login` the
        # refusal is doubly binding (msg 8629): a login written while the agent
        # runs is a credential swap under a running process.
        if not force:
            raise LaunchError(
                f"{managed} is RUNNING and shares ~/.claude with this shell. "
                f"Two claudes on one home step on each other's sqlite state. "
                f"Stop the agent first (reveille-launch stop {user} {agent}), "
                f"then {doing}; or pass --force if you genuinely want both at "
                f"once.")
        print(f"WARNING: {managed} is RUNNING and shares this home -- "
              f"--force given, proceeding.", file=sys.stderr)


def cmd_debug(a):
    creds = resolve_credentials(load_profile(a.user), a.agent, "")
    names, cred_env, kind = credential_env(creds)
    env = dict(os.environ, **cred_env)
    _refuse_if_running(a.user, a.agent, a.force, "debug")
    home_note = ("  claude auths from the login COPIED into the agent home at "
                 "boot (home-login mode); no token env is passed -- if it asks "
                 f"to log in, run `reveille-launch login {a.user}` and restart "
                 "the agent.\n" if kind == "home-login" else "")
    print(f"debug shell into {a.user}/{a.agent}: image {a.image}, "
          f"credential: {kind}\n{home_note}"
          f"  inside, try:  claude /status   (whose account, which billing)\n"
          f"                claude /usage    (subscription limit bars -- absent "
          f"means not on a subscription seat)\n"
          f"  exits clean: --rm, nothing to clean up; the agent home persists.",
          file=sys.stderr)
    argv = debug_argv(a.user, a.agent, a.image, a.network,
                      extra_env=names, entrypoint=a.entrypoint)
    os.execvpe("docker", argv, env)   # foreground: the tty is the point


def login_argv(user, image, network, data_base=None):
    """The per-USER login shell as argv: the user's login home mounted as
    ~/.claude, seeded past the first-run wizard, landing in bash. NO agent
    mounts, NO credential env -- this container exists so `claude /login` can
    write the ONE credential file every agent of this user copies at boot.
    Pure: no env read."""
    return ["docker", "run", "--rm", "-ti",
            "--name", f"rev-{user}-login",
            "--network", network,
            "-v", f"{user_auth_root(user, data_base)}:/home/agent/.claude",
            "--entrypoint", "python3", image, "-c", _DEBUG_SEED]


# ---- browser-mediated login (operator, msg 8642; rulings 8644) ---------------
# The SAME login container the terminal path opens, driven headless: claude
# /login runs in tmux, the container ITSELF advances the method picker to
# choice 1 (subscription) -- the picker must never reach a human, because its
# choice 2 is per-token Console billing and a hurried click through a picker
# we surfaced would rebuild the billing incident with a nicer front end. The
# launcher then only READS the pane (the URL is shown to the human, never
# followed by us) and relays ONE pasted authorization code back. Completion is
# OBSERVED: .credentials.json appears in the login home, read fresh -- never
# the container exiting, never the human claiming.
_LOGIN_BOOT = r"""
python3 -c "$SEED"
tmux new-session -d -s login -x 220 -y 50 "claude /login"
for i in $(seq 1 120); do
  if tmux capture-pane -t login -p 2>/dev/null | grep -q "Select login method"; then
    tmux send-keys -t login 1 Enter
    break
  fi
  sleep 0.5
done
while tmux has-session -t login 2>/dev/null; do sleep 1; done
"""

# From the REAL pane, captured in-container 2026-07-30 (msg 8643): the flow
# prints the authorize URL and then waits at "Paste code here if prompted".
_LOGIN_URL_RE = re.compile(r"https://[^\s]*claude\.com/[^\s]+")
# The code is OPAQUE: never parsed beyond "printable, sane length, no control
# characters" -- enough to refuse key sequences, never enough to learn it.
_LOGIN_CODE_RE = re.compile(r"^[A-Za-z0-9_\-#%.~]{4,256}$")


def login_container_name(user):
    return f"rev-{user}-login"


def login_bg_argv(user, image, network, data_base=None):
    """The DETACHED login container (browser path): same mounts and same
    no-credential-env rule as login_argv, but it holds a tmux the launcher can
    read, and its boot script advances the picker itself. Pure."""
    return ["docker", "run", "-d",
            "--name", login_container_name(user),
            "--network", network,
            "-v", f"{user_auth_root(user, data_base)}:/home/agent/.claude",
            "-e", "SEED",   # the first-run seed rides env BY NAME, like every secretish value
            "--entrypoint", "sh", image, "-c", _LOGIN_BOOT]


def parse_login_pane(text):
    """The pending login's stage, READ from its pane. Pure. The pane is the
    truth (ruling 8644): expiry and failure surface here, not from exit codes.
    -> {stage: starting|picker|awaiting-code|failed, url: str|None}"""
    url = None
    m = _LOGIN_URL_RE.search(text.replace("\n", ""))   # tmux wraps the long URL
    if m:
        url = m.group(0)
    if "Paste code here" in text or url:
        return {"stage": "awaiting-code", "url": url}
    if "Select login method" in text:
        return {"stage": "picker", "url": None}
    if "Login" in text or "Welcome" in text or not text.strip():
        return {"stage": "starting", "url": None}
    return {"stage": "failed", "url": None}


def ensure_login_home(user, image):
    """Create the user's login home with the OWNERSHIP fix, and say whether a
    login already exists. Shared by the terminal and browser paths -- one
    mechanism, two doors.

    THIRD INSTANCE of the data-root ownership defect (msg 8475, and again in
    the chown container itself). This dir is created by the LAUNCHER's uid and
    then mounted as ~/.claude in a container running as the image's agent uid;
    if they differ, `claude /login` cannot write .credentials.json and the one
    command this whole mode depends on fails. It works on this host only
    because the operator happens to be uid 1000, which is exactly the
    coincidence that hid it the first two times. provision_agent already does
    this for the agent home; the login home needs it for the same reason."""
    root = user_auth_root(user)
    os.makedirs(os.path.dirname(root), mode=0o700, exist_ok=True)
    os.chmod(os.path.dirname(root), 0o700)
    os.makedirs(root, mode=0o700, exist_ok=True)
    _own_agent_dirs(root, image, subdirs=())   # the login home IS ~/.claude
    return os.path.isfile(os.path.join(root, ".credentials.json"))


def cmd_login(a):
    """The per-USER interactive login for home-login mode (operator
    requirement, 2026-07-30): ONE login shared by ALL of the user's agents as
    a boot-time copy, so with several subscription accounts the user re-logs
    in here when one is exhausted and restarts the agents -- the whole fleet
    moves to the new account. Re-login while agents run is safe and expected:
    each agent holds its own COPY and picks up the new login on its next
    restart, never mid-session. The launcher never holds this credential; it
    lives in the user's login home, written by claude itself."""
    had = ensure_login_home(a.user, a.image)
    print(f"login shell for user {a.user} ({'RE-login: replaces the current '
          'account for every agent' if had else 'first login'}): image "
          f"{a.image}, no credential env, no agent home touched.\n"
          f"  inside, run:  claude /login   (complete the browser flow with "
          f"the account whose plan should pay)\n"
          f"  then exit. Agents in claude_mode=home-login copy this login at "
          f"boot -- RESTART them to move them to the new account; running "
          f"agents keep their current copy until then.",
          file=sys.stderr)
    os.execvpe("docker", login_argv(a.user, a.image, a.network),
               dict(os.environ))


def masked_profile(prof):
    """What the API may say about a profile: WHICH fields are set, never what
    they hold. repo_url is not a secret and passes through. Pure."""
    def mask(d):
        out = {}
        for k in ("claude_token", "github_token"):
            out[k] = "set" if d.get(k) else "absent"
        if d.get("repo_url"):
            out["repo_url"] = d["repo_url"]
        if d.get("claude_mode"):   # a choice, not a secret
            out["claude_mode"] = d["claude_mode"]
        return out
    body = mask(prof)
    body["agents"] = {name: mask(o) for name, o in
                      (prof.get("agents") or {}).items()}
    return body


PROFILE_NOTES = (
    "Credentials are stored 0600 in your profile file on the launcher host and "
    "injected only into your own containers' environment -- your agents can "
    "read your own tokens, the same as on your laptop. Claude spend and rate "
    "limits are your subscription's own; N agents share them. Rotation is "
    "user-side: run `claude setup-token` again and paste the new value. "
    "Whether rotating revokes the previous token is not documented; do not "
    "assume it does. claude_mode=home-login uses no token at all: the agent "
    "auths from the file `claude /login` wrote into its own home "
    "(reveille-launch login <user> <agent>, once, agent stopped). That mode "
    "is NOT zero-touch: a freshly provisioned agent sits at a login prompt "
    "until you log it in.")


def own_dirs_argv(root, image, subdirs=("claude", "repos")):
    """The chown container's argv, pure so the uid-critical shape is unit-
    testable anywhere (msg 8479: our one smoke host is uid 1000, where BOTH
    ownership defects were invisible -- the argv test is where the accident
    cannot hide). --user 0:0 is load-bearing: the image sets USER agent and
    --entrypoint does not change the user, so without it the chown runs as
    uid 1000 and cannot take ownership of dirs a root launcher created.

    subdirs=() owns the ROOT ITSELF -- the USER LOGIN home, which has no
    claude/repos beneath it because it IS the ~/.claude a login container
    mounts. Chowning names that do not exist made chown exit 1, so the
    ownership fix crashed the very command it was added to protect, on both
    the terminal and browser doors, for any user whose login home was fresh."""
    targets = [f"/own/{d}" for d in subdirs] or ["/own"]
    return ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "chown",
            "-v", f"{root}:/own", image,
            "-R", f"{AGENT_UID}:{AGENT_GID}", *targets]


def _own_agent_dirs(root, image, subdirs=("claude", "repos")):
    """Hand the agent dirs to the image's uid (msg 8475). A plain os.chown
    needs CAP_CHOWN the launcher's own uid may not have; its actual privilege
    is the docker socket, so the chown rides a throwaway container of the very
    image about to use the dirs. -R heals pre-existing wrong-uid files from a
    launcher that ran before this fix, not just fresh mkdirs."""
    subprocess.run(own_dirs_argv(root, image, subdirs), check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def _ensure_network(net, broker_url):
    """Create the shared network if missing and pull the broker onto it, so the agent
    can resolve it by DNS. Both are idempotent -- create/connect on an existing
    network/membership is a harmless non-zero we swallow. host mode owns no network."""
    if net == "host":
        return
    _docker("network", "create", net, check=False, capture=True)
    host = urllib.parse.urlparse(broker_url).hostname
    if host:  # a container name resolves; an IP or external host just no-ops here
        _docker("network", "connect", net, host, check=False, capture=True)


# ---- subcommands -------------------------------------------------------------------

def provision_agent(conn, user, agent, repo_url, token, *, image=DEFAULT_IMAGE,
                    network=DEFAULT_NETWORK, broker=DEFAULT_BROKER,
                    boot_cmd=None, replace=False, role_prompt=None, model=None):
    """The one provisioning path (CLI and HTTP share it). Validates, enforces
    the per-user cap, lays the per-agent data root, runs the container. The
    token exists only in this frame and the docker-run child's env -- never
    argv, never any store. Raises LaunchError; returns the container name."""
    for label, val in (("user", user), ("agent", agent)):
        if not ROLE_RE.match(val or ""):
            raise LaunchError(f"bad {label} {val!r}: lowercase alnum + dash, "
                              f"2-64 chars")
    name = container_name(user, agent)
    # Re-provision is routine (3.5) but must never be UNPROMPTED: an accidental
    # re-provision of a live agent would destroy its session mid-conversation.
    # Refuse unless replace is explicit; the data root survives either way.
    if _exists(name) and not replace:
        raise LaunchError(f"{name} already exists -- destroy first, or pass "
                          f"replace to re-provision (its data root is kept)")
    quotas = _quotas_for(conn, user)
    # The per-user container cap (sec 6). The one being replaced does not count
    # against itself.
    others = conn.execute(
        "SELECT count(*) FROM containers WHERE user=? AND agent!=?",
        (user, agent)).fetchone()[0]
    if others >= quotas["max_containers"]:
        raise LaunchError(
            f"{user} is at their container cap ({quotas['max_containers']}); "
            f"destroy one or raise it with `quota {user} --max-containers N`")
    if not token:
        raise LaunchError("no token given")

    # P2: the user's stored credentials, override > global > request repo_url.
    creds = resolve_credentials(load_profile(user), agent, repo_url)
    cred_names, cred_env, kind = credential_env(creds)
    # No claude credential + the default boot (claude itself) = an agent that
    # will hang at a login prompt nobody is watching -- or worse, find some
    # ambient credential and bill it. REFUSE and name the fix. A custom
    # boot_cmd (agent-probe, gates) runs no claude and needs no credential:
    # the requirement follows the thing that consumes it. home-login mode has
    # the same requirement one level up: its credential is the file in the
    # USER's login home, checkable right here -- refuse-not-create, with the
    # one command that fixes it named.
    if kind == "none" and not boot_cmd:
        raise LaunchError(
            f"no claude credential for {user}/{agent}: save one in the profile "
            f"(claude setup-token output), or choose claude_mode=home-login "
            f"and log in once (reveille-launch login {user}) -- an agent must "
            f"never inherit whatever credential is lying around in the "
            f"launcher's environment")
    auth_mount = None
    if kind == "home-login":
        auth_mount = user_auth_root(user)
        if not os.path.isfile(os.path.join(auth_mount, ".credentials.json")) \
                and not boot_cmd:
            raise LaunchError(
                f"claude_mode=home-login but {user} has no login on file: run "
                f"`reveille-launch login {user}` first (one interactive "
                f"`claude /login`; every agent copies it at boot)")
    env = dict(
        os.environ,
        REVEILLE_AGENT_ROLE=agent,
        REVEILLE_URL=broker,
        REVEILLE_REPO_URL=creds["repo_url"],
        REVEILLE_TOKEN=token,
        # Per-container gate secret (T1 4.3), minted HERE at provision, injected by
        # name, never stored -- dies with the container, re-provision mints a new one.
        REVEILLE_GATE_SECRET=secrets.token_hex(32),
        **cred_env,
    )
    extra_env = list(cred_names)
    if role_prompt:
        # Sec 5: the role's text lands in the container via env; the entrypoint
        # writes it into ~/.claude/CLAUDE.md (marker-guarded, once).
        extra_env.append("REVEILLE_ROLE_PROMPT")
        env["REVEILLE_ROLE_PROMPT"] = role_prompt
    if model:
        # The user's model choice for THIS agent. Env, not a config file: it is
        # per-container and must not be baked into the persisted home, where it
        # would outlive the choice and quietly override the next one.
        extra_env.append("ANTHROPIC_MODEL")
        env["ANTHROPIC_MODEL"] = model

    # The agent's home, nothing else's (sec 4). The USER root is 0700 so no other
    # host user browses it; the agent dirs under it belong to the AGENT.
    root = data_root(user, agent)
    user_root = os.path.dirname(root)
    os.makedirs(user_root, mode=0o700, exist_ok=True)
    os.chmod(user_root, 0o700)
    for sub in ("claude", "repos"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    _own_agent_dirs(root, image)

    _ensure_network(network, broker)
    if replace:
        _docker("rm", "-f", name, check=False, capture=True)
    argv = docker_run_argv(user, agent, image, network, quotas,
                           boot_cmd=boot_cmd, extra_env=extra_env,
                           auth_mount=auth_mount)
    subprocess.run(argv, env=env, check=True, stdout=subprocess.DEVNULL)
    _record(conn, user, agent, creds["repo_url"], image, broker)
    owner = claim_agent_name(conn, user, agent)
    # The KIND on the audit line and in every response: "provisioned on api-key
    # billing" is one word that turns an invoice surprise into a paste-time fact.
    _audit("PROVISION", user=user, agent=agent, image=image, credential=kind,
           name_owner=owner["user"])
    return name, kind


def destroy_agent(conn, user, agent, purge=False):
    """Shared destroy path. Grants die with the container (4.5) -- the gate
    secret they were signed against is gone, so the records are history, not
    authority. The data root survives unless purge.

    agent_owners is UNTOUCHED, on purpose and by ruling 8660: the name outlives
    the container, and who owned it is the one fact no surviving state can
    re-derive afterwards. Freeing a name is release_agent_name(), explicit and
    audited -- never a side effect of throwing a container away."""
    _docker("rm", "-f", container_name(user, agent), check=False, capture=True)
    if purge:
        import shutil
        shutil.rmtree(data_root(user, agent), ignore_errors=True)
    conn.execute("DELETE FROM containers WHERE user=? AND agent=?", (user, agent))
    conn.execute("DELETE FROM grants WHERE user=? AND agent=?", (user, agent))
    conn.execute("DELETE FROM sessions_seen WHERE user=? AND agent=?",
                 (user, agent))
    conn.commit()


def mint_grant(conn, user, agent, grantee, mode, ttl):
    """Shared grant-mint path. The token is RETURNED once, never stored
    (4.5.2): re-issue is re-mint, never retrieval."""
    _known_agent(conn, user, agent)
    name = container_name(user, agent)
    gid = secrets.token_hex(4)
    res = _docker("exec", name, "attach-gate", "mint", mode, str(ttl), gid,
                  check=False, capture=True)
    token = (res.stdout or "").strip()
    if res.returncode != 0 or not token.startswith("v1."):
        raise LaunchError(f"mint failed in {name} -- is it running?")
    now = time.time_ns()
    conn.execute(
        "INSERT INTO grants(id, user, agent, grantee, mode, issued_ns, expiry_ns, "
        "revoked_ns) VALUES(?,?,?,?,?,?,?,NULL)",
        (gid, user, agent, grantee, mode, now, now + ttl * 10**9))
    conn.commit()
    # RELATIVE, and that is the fix (DES-006 2.3): this used to be the
    # container's docker-network address, which resolves on the host and
    # NOWHERE else -- so every grant handed to a remote human was born broken.
    # A path resolves against whatever origin the recipient opened, which is
    # the proxy, which is reachable from wherever they are.
    return {"id": gid, "mode": mode, "grantee": grantee,
            "expiry_ns": now + ttl * 10**9,
            "attach_url": f"/attach/{agent}/?arg={token}"}


def container_addr(user, agent, port=7681):
    """Where THIS agent's ttyd lives, resolved by the launcher from its own
    records -- never from anything a client sent (DES-006 2.3: never an open
    proxy). Returns "ip:port", or None when the container is not running."""
    name = container_name(user, agent)
    ip = _docker("inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 name, check=False, capture=True)
    host = (ip.stdout or "").strip()
    return f"{host}:{port}" if host else None


def revoke_grant(conn, user, agent, grant_id, actor):
    row = conn.execute("SELECT * FROM grants WHERE id=? AND user=? AND agent=?",
                       (grant_id, user, agent)).fetchone()
    if row is None:
        raise LaunchError(f"no grant {grant_id} on {user}/{agent}")
    # Kill first, record after: the <1s promise is about the client dropping.
    _kill_grant_sessions(user, agent, grant_id)
    conn.execute("UPDATE grants SET revoked_ns=? WHERE id=?",
                 (time.time_ns(), grant_id))
    # The killed session must not also read as an observed DETACH next tick.
    conn.execute("DELETE FROM sessions_seen WHERE user=? AND agent=? AND "
                 "session IN (?,?)",
                 (user, agent, f"d-{grant_id}", f"v-{grant_id}"))
    conn.commit()
    _audit("REVOKE", user=user, agent=agent, grant=grant_id, mode=row["mode"],
           grantee=row["grantee"], actor=actor)
    return dict(row)


def cmd_new(a):
    conn = _db()
    try:
        token = read_secret(f"bound broker token for {a.agent}")
        name, kind = provision_agent(conn, a.user, a.agent, a.repo_url, token,
                                     image=a.image, network=a.network,
                                     broker=a.broker, boot_cmd=a.boot_cmd,
                                     replace=a.replace)
    except LaunchError as e:
        conn.close()
        die(str(e))
    conn.close()
    if a.no_wait:
        print(f"provisioned {name} on network {a.network} "
              f"(credential: {kind}; health wait skipped)")
        return 0
    print(f"provisioned {name} on network {a.network} (credential: {kind}); "
          f"waiting for presence live+connected (timeout {a.timeout}s)...")
    if wait_healthy(a.health_url, a.agent, token, a.timeout):
        print(f"OK: {a.agent} is live+connected (broker {a.broker})")
        return 0
    print(f"UNHEALTHY: {a.agent} never reached live+connected. Inspect:\n"
          f"  docker logs --tail 20 {name}", file=sys.stderr)
    return 1


def cmd_ls(a):
    conn = _db()
    rows = conn.execute("SELECT * FROM containers ORDER BY user, agent").fetchall()
    conn.close()
    if not rows:
        print("no containers provisioned")
        return 0
    for r in rows:
        st = _docker("inspect", "-f", "{{.State.Status}}", r["container"],
                     check=False, capture=True)
        status = (st.stdout or "").strip() or "absent"
        print(f"{r['user']:14s} {r['agent']:16s} {status:10s} "
              f"{r['image']:22s} {r['repo_url']}")
    return 0


def cmd_stop(a):
    _docker("stop", container_name(a.user, a.agent))
    return 0


def cmd_start(a):
    _docker("start", container_name(a.user, a.agent))
    return 0


def cmd_destroy(a):
    conn = _db()
    destroy_agent(conn, a.user, a.agent, purge=a.purge)
    conn.close()
    root = data_root(a.user, a.agent)
    if a.purge:
        print(f"destroyed {a.user}/{a.agent} and purged {root}")
    else:
        print(f"destroyed {a.user}/{a.agent}; kept {root} (--purge to drop -- "
              f"recreate picks up everything the agent learned)")
    return 0


def cmd_owner(a):
    """Who owns an agent NAME -- and, with --release, the key to that lock.

    Ownership is not enforced yet (ruling 8660: recording is urgent, enforcing
    is not), so today this reads the record the enforcement will read. Release
    exists from day one anyway: an unenforced-forever name held by a deleted
    account is a leak, and adding the key after the lock is how locks stay
    unopenable."""
    conn = _db()
    try:
        if a.release:
            prior = release_agent_name(conn, a.agent, a.by)
            if prior is None:
                print(f"{a.agent}: unowned or already released -- nothing to do")
                return 1
            _audit("RELEASE-NAME", agent=a.agent, prior_owner=prior, by=a.by)
            print(f"released {a.agent} (was {prior}'s); anyone may claim it now")
            return 0
        row = conn.execute("SELECT * FROM agent_owners WHERE agent=?",
                           (a.agent,)).fetchone()
        if row is None:
            print(f"{a.agent}: no owner recorded -- provisioned before this "
                  f"table existed, or never provisioned")
            return 1
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                             time.gmtime(row["first_provisioned_ns"] / 1e9))
        if row["released_ns"] is not None:
            print(f"{a.agent}: RELEASED by {row['released_by']} "
                  f"(was {row['user']}'s, first provisioned {when})")
        else:
            print(f"{a.agent}: {row['user']} (first provisioned {when})")
        return 0
    finally:
        conn.close()


def cmd_profile(a):
    """Show (masked) or set one user's stored credentials. Token values come
    from stdin/prompt via read_secret -- never argv; --clear-* drops a field."""
    prof = load_profile(a.user)
    updates = {}
    if a.claude_token:
        updates["claude_token"] = read_secret(f"claude credential for {a.user}")
    if a.github_token:
        updates["github_token"] = read_secret(f"github token for {a.user}")
    if a.repo_url is not None:
        updates["repo_url"] = a.repo_url
    if a.claude_mode:
        updates["claude_mode"] = a.claude_mode
    if a.clear_claude:
        updates["claude_token"] = ""
    if a.clear_github:
        updates["github_token"] = ""
    if updates:
        save_profile(a.user, merge_profile(prof, updates, agent=a.agent))
    print(json.dumps(masked_profile(load_profile(a.user)), indent=2))
    return 0


def cmd_quota(a):
    """Show or override one user's quotas. Bare `quota <user>` prints the
    resolved values; any flag writes an override (NULL columns stay default)."""
    conn = _db()
    sets = {k: getattr(a, k) for k in
            ("cpus", "mem", "disk_gb", "pids", "max_containers")
            if getattr(a, k) is not None}
    if sets:
        conn.execute("INSERT OR IGNORE INTO user_quotas(user) VALUES(?)", (a.user,))
        for k, v in sets.items():
            conn.execute(f"UPDATE user_quotas SET {k}=? WHERE user=?", (v, a.user))
        conn.commit()
    q = _quotas_for(conn, a.user)
    used = conn.execute("SELECT count(*) FROM containers WHERE user=?",
                        (a.user,)).fetchone()[0]
    conn.close()
    print(f"{a.user}: cpus={q['cpus']} mem={q['mem']} disk_gb={q['disk_gb']} "
          f"(recorded, not yet enforced) pids={q['pids']} "
          f"containers={used}/{q['max_containers']}")
    return 0


def _known_agent(conn, user, agent):
    # LaunchError, not die: mint_grant runs under both front doors (CLI + HTTP).
    if conn.execute("SELECT 1 FROM containers WHERE user=? AND agent=?",
                    (user, agent)).fetchone() is None:
        raise LaunchError(f"unknown agent {user}/{agent} -- provision it first")


def cmd_grant(a):
    conn = _db()
    try:
        # Mint INSIDE the container: the gate secret never leaves it, re-issue
        # is re-mint (4.5.2). The launcher only ever holds the token long
        # enough to print it once.
        out = mint_grant(conn, a.user, a.agent, a.grantee, a.mode, a.ttl)
    except LaunchError as e:
        conn.close()
        die(str(e))
    conn.close()
    exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(out["expiry_ns"] / 1e9))
    print(f"grant {out['id']}: {a.mode} for {a.grantee} on {a.user}/{a.agent}, "
          f"expires {exp}")
    # A PATH, not an address: it resolves against the reveille origin the
    # recipient opens (the proxy), which is reachable from anywhere they are --
    # the container-network address it used to print never was.
    print(f"attach: {out['attach_url']}  (on your reveille origin)")
    print("token shown once, never stored; lost token = revoke + re-grant")
    return 0


def cmd_grants(a):
    conn = _db()
    q, args = "SELECT * FROM grants", []
    conds = []
    if a.user:
        conds.append("user=?")
        args.append(a.user)
    if a.agent:
        conds.append("agent=?")
        args.append(a.agent)
    if conds:
        q += " WHERE " + " AND ".join(conds)
    rows = conn.execute(q + " ORDER BY issued_ns", args).fetchall()
    conn.close()
    if not rows:
        print("no grants")
        return 0
    now = time.time_ns()
    for r in rows:
        state = ("revoked" if r["revoked_ns"]
                 else "expired" if now >= r["expiry_ns"] else "active")
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(r["expiry_ns"] / 1e9))
        print(f"{r['id']}  {r['user']:12s} {r['agent']:14s} {r['grantee']:16s} "
              f"{r['mode']:6s} {state:8s} expires {exp}")
    return 0


def _kill_grant_sessions(user, agent, grant_id):
    for prefix in ("d", "v"):  # both: a flip may have left the other-mode name
        _docker("exec", container_name(user, agent),
                "tmux", "kill-session", "-t", f"{prefix}-{grant_id}",
                check=False, capture=True)


def cmd_revoke(a):
    conn = _db()
    try:
        row = revoke_grant(conn, a.user, a.agent, a.grant_id,
                           actor="launcher-cli")
    except LaunchError as e:
        conn.close()
        die(str(e))
    conn.close()
    print(f"revoked {a.grant_id} ({row['mode']} for {row['grantee']} on "
          f"{a.user}/{a.agent}); its token stays signed until expiry -- the "
          f"sweep kills any re-attach")
    return 0


def cmd_flip(a):
    conn = _db()
    try:
        _known_agent(conn, a.user, a.agent)
    except LaunchError as e:
        die(str(e))
    finally:
        conn.close()
    name = container_name(a.user, a.agent)
    # Runtime toggle rides a marker file the gate checks per-attach: ttyd's
    # children only ever see create-time env, so env alone cannot flip live.
    shell = ("touch ~/.multi-driver" if a.state == "on"
             else "rm -f ~/.multi-driver")
    res = _docker("exec", name, "sh", "-c", shell, check=False, capture=True)
    if res.returncode != 0:
        die(f"flip failed in {name} -- is it running?")
    _audit("FLIP", user=a.user, agent=a.agent, multi_driver=a.state,
           actor="launcher-cli")
    print(f"{a.user}/{a.agent}: multi-driver {a.state}")
    return 0


def _live_grant_sessions(user, agent):
    res = _docker("exec", container_name(user, agent),
                  "tmux", "list-sessions", "-F",
                  "#{session_name} #{session_attached}",
                  check=False, capture=True)
    # No tmux server / stopped container reads as no sessions -- the sweep only
    # reasons about what it can OBSERVE.
    live = {}
    for line in (res.stdout or "").splitlines():
        parts = line.split()
        if len(parts) == 2 and re.match(r"\A[dv]-[0-9a-f]+\Z", parts[0]):
            live[parts[0]] = parts[1] != "0"
    return live


def _idle_probe(user, agent):
    """One exec, three observations (sec 7.1): any attached tmux client, the
    newest session activity, the newest spool entry (a ring that arrived but has
    not fired yet). Epoch seconds; a stopped container or absent tmux reads as
    (False, 0, 0) and the caller skips it -- already-stopped needs no stop."""
    res = _docker("exec", container_name(user, agent), "sh", "-c",
                  "tmux list-clients -F x 2>/dev/null | wc -l;"
                  " tmux list-sessions -F '#{session_activity}' 2>/dev/null"
                  " | sort -rn | head -1;"
                  " find ~/.reveille/spool -name '*.ring' -printf '%T@\\n'"
                  " 2>/dev/null | sort -rn | head -1 | cut -d. -f1",
                  check=False, capture=True)
    if res.returncode != 0:
        return False, 0, 0
    lines = (res.stdout or "").splitlines() + ["", "", ""]
    def n(s):
        return int(s.strip()) if s.strip().isdigit() else 0
    return n(lines[0]) > 0, n(lines[1]), n(lines[2])


def _harvest_gate_audit(user, agent, grants_by_id):
    """Pull the gate's ATTACH lines (read-and-truncate) into the host log,
    keeping the gate's own timestamps and resolving grantee from the record."""
    # 4.6.1: move-then-read, never read-then-truncate -- a gate append between
    # cat and truncate would be dropped, and a boundary log with a known drop
    # window is not a boundary. rename is atomic within the fs; an append that
    # loses the race lands in a fresh ~/.attach-audit and survives to the next
    # tick. The $$-suffix keeps a crashed harvest's remainder readable (glob),
    # at worst re-reading it -- a duplicate line over a dropped one, always.
    res = _docker("exec", container_name(user, agent), "sh", "-c",
                  "[ -f ~/.attach-audit ] && mv ~/.attach-audit ~/.attach-audit.h.$$;"
                  " cat ~/.attach-audit.h.* 2>/dev/null; rm -f ~/.attach-audit.h.*",
                  check=False, capture=True)
    for line in (res.stdout or "").splitlines():
        parts = line.split()  # <ts> ATTACH <mode> <id>
        if len(parts) != 4 or parts[1] != "ATTACH":
            continue
        ts, _, mode, gid = parts
        g = grants_by_id.get(gid)
        _audit("ATTACH", ts_iso=ts, user=user, agent=agent, grant=gid, mode=mode,
               grantee=(g["grantee"] if g else "unknown"), src="gate")


def _sweep_once(conn, idle_window_ns=0):
    now = time.time_ns()
    for c in conn.execute("SELECT user, agent FROM containers").fetchall():
        user, agent = c["user"], c["agent"]
        # Expired grant ROWS are kept on purpose. The row is the only thing that
        # answers "whose session was that" for an audit line, and the gate's
        # ATTACH lines are harvested a tick LATE -- delete on expiry and the
        # harvest that follows writes grantee=unknown, turning the audit trail
        # into a record of anonymous attaches. The row holds no secret (4.5.2:
        # metadata only, never anything a token could be reproduced from) and is
        # ~100 bytes per attach. Expiry is enforced by killing the SESSION, which
        # is the thing that grants access; the row is history.
        grants = {r["id"]: r for r in conn.execute(
            "SELECT * FROM grants WHERE user=? AND agent=?",
            (user, agent)).fetchall()}
        _harvest_gate_audit(user, agent, grants)
        live = _live_grant_sessions(user, agent)
        seen = {r["session"] for r in conn.execute(
            "SELECT session FROM sessions_seen WHERE user=? AND agent=?",
            (user, agent)).fetchall()}
        kills, detaches = sweep_actions(grants, live, seen, now)
        for session, reason in kills:
            _docker("exec", container_name(user, agent),
                    "tmux", "kill-session", "-t", session,
                    check=False, capture=True)
            gid = session[2:]
            g = grants.get(gid)
            _audit("KILL", user=user, agent=agent, grant=gid, session=session,
                   reason=reason, grantee=(g["grantee"] if g else "unknown"))
        for session in detaches:
            gid = session[2:]
            g = grants.get(gid)
            _audit("DETACH", user=user, agent=agent, grant=gid,
                   mode=("driver" if session.startswith("d-") else "viewer"),
                   grantee=(g["grantee"] if g else "unknown"),
                   observed="sweep-tick")  # observation time, not event time
        killed = {s for s, _ in kills}
        conn.execute("DELETE FROM sessions_seen WHERE user=? AND agent=?",
                     (user, agent))
        conn.executemany(
            "INSERT INTO sessions_seen(user, agent, session, first_seen_ns) "
            "VALUES(?,?,?,?)",
            [(user, agent, s, now) for s in set(live) - killed])
        # 24h idle STOP, never destroy (sec 7.1): data is on bind mounts, so a
        # restart is one `start` and loses nothing. A stopped container probes
        # as (False, 0, 0) with an exec error and is skipped above the is_idle
        # window by construction -- but skip it explicitly: stopping the
        # stopped is noise.
        if idle_window_ns > 0:
            st = _docker("inspect", "-f", "{{.State.Running}}",
                         container_name(user, agent), check=False, capture=True)
            if (st.stdout or "").strip() == "true":
                attached, act_s, ring_s = _idle_probe(user, agent)
                if is_idle(attached, act_s * 10**9, ring_s * 10**9, now,
                           idle_window_ns):
                    _docker("stop", container_name(user, agent),
                            check=False, capture=True)
                    _audit("IDLESTOP", user=user, agent=agent,
                           window_s=idle_window_ns // 10**9)
    conn.commit()


def cmd_sweep(a):
    """ONE tick, then exit -- for an operator who wants the sweep to happen now.
    The recurring one is not here: it runs inside serve (see _sweep_forever).
    A loop mode on a CLI nobody schedules is what let expiry go unenforced for
    the whole life of the deployment."""
    conn = _db()
    _sweep_once(conn, idle_window_ns=int(a.idle_hours * 3600 * 10**9))
    conn.close()
    return 0


DEFAULT_PIN = os.path.expanduser("~/.reveille/launcher-src")


def pin_refusal(dirty, has_upstream, is_ff):
    """Why this pinned tree must not be moved to origin/main, or None. Pure.

    The pin is a DEPLOYMENT, so it refuses anything it cannot describe: local
    edits (whose are they? nobody's -- this tree is not for editing) and any
    move that is not a fast-forward (a rewritten main is a decision, not a
    routine update). Both refusals leave the running code exactly where it was,
    which is the safe direction for a service."""
    if dirty:
        return ("the pinned tree has local changes. Nothing edits this tree -- "
                "it exists to be a known commit. Inspect it, then `git -C <path>"
                " checkout -- .` if the changes are junk.")
    if not has_upstream:
        return "no origin/main to pin to (is the clone's remote reachable?)"
    if not is_ff:
        return ("origin/main is not a fast-forward from the pinned commit -- "
                "main was rewritten or this tree was moved by hand. Deleting "
                "the pinned tree and pinning again is the honest fix; a forced "
                "move would silently change what is serving.")
    return None


def _git(path, *args, check=True):
    return subprocess.run(["git", "-C", path, *args], check=check,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          text=True)


def source_stamp(path):
    """(commit, branch, version) of the tree the RUNNING code came from, as
    strings that are safe to print. A service that cannot say what it is running
    can only be identified by asking a person, which is how a developer's
    checkout ended up serving the operator (msg 8568)."""
    commit = branch = "unknown"
    r = _git(path, "rev-parse", "--short", "HEAD", check=False)
    if r.returncode == 0:
        commit = r.stdout.strip()
        b = _git(path, "rev-parse", "--abbrev-ref", "HEAD", check=False)
        branch = b.stdout.strip() or "detached"
        if _git(path, "status", "--porcelain", check=False).stdout.strip():
            branch += "+dirty"
    version = "unknown"
    with contextlib.suppress(OSError):
        with open(os.path.join(path, "pyproject.toml")) as f:
            m = re.search(r'^version\s*=\s*"([^"]+)"', f.read(), re.M)
            version = m.group(1) if m else "unknown"
    return commit, branch, version


def cmd_pin(a):
    """Create or fast-forward the tree the launcher is SERVED from.

    Not the developer's checkout: that tree changes under the service every time
    someone reviews a branch, which makes `git checkout` a deployment and leaves
    "what is serving?" answerable only by a person. This one only ever moves
    forward along main."""
    path, src = os.path.abspath(a.path), os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))
    if not os.path.isdir(os.path.join(path, ".git")):
        origin = a.origin or _git(src, "remote", "get-url", "origin").stdout.strip()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        r = subprocess.run(["git", "clone", "--branch", "main", origin, path],
                           capture_output=True, text=True)
        if r.returncode != 0:
            die(f"clone of {origin} failed: {r.stderr.strip()}")
        print(f"cloned {origin} -> {path}")
    _git(path, "fetch", "--quiet", "origin", "main", check=False)
    head = _git(path, "rev-parse", "HEAD").stdout.strip()
    up = _git(path, "rev-parse", "origin/main", check=False)
    upstream = up.stdout.strip() if up.returncode == 0 else ""
    if (why := pin_refusal(
            bool(_git(path, "status", "--porcelain").stdout.strip()),
            bool(upstream),
            upstream and _git(path, "merge-base", "--is-ancestor", head,
                              upstream, check=False).returncode == 0)):
        die(f"reveille-launch pin: {why}")
    _git(path, "checkout", "--quiet", "main", check=False)
    _git(path, "merge", "--ff-only", "--quiet", "origin/main")
    # The venv is part of the artifact: serve imports uvicorn and starlette, and
    # a pinned tree without them falls back to system python and dies at import.
    r = subprocess.run(["uv", "sync", "--quiet"], cwd=path, capture_output=True,
                       text=True)
    if r.returncode != 0:
        die(f"uv sync in {path} failed: {r.stderr.strip()}")
    commit, branch, version = source_stamp(path)
    print(f"pinned {path} -> {commit} ({branch}), reveille {version}\n"
          f"declare it so the supervisor uses it:\n"
          f"  echo 'REVEILLE_LAUNCH_REPO={path}' >> ~/.reveille/launcher.env\n"
          f"then stop the running launcher; the Stop hook respawns it from here.")
    return 0


def _sweep_forever(interval_s, idle_window_ns, stop):
    """The sweep's ONLY scheduler, run as a thread inside serve.

    It lives here and not in a systemd timer or a crontab because a periodic
    task whose scheduler is a separate deployment step is a task that does not
    run: `sweep --loop` existed, worked, was covered by two smoke gates, and had
    never once executed on the live box -- so grant expiry was enforced only at
    the doorway (a session attached when its grant expired ran forever) and the
    24h idle stop had never fired. serve is already supervised, flock-guarded
    and running; anything hung off it inherits all three.

    Own connection because sqlite connections belong to one thread. Own
    try/except around each tick because a sweep that dies takes expiry
    enforcement with it silently -- and the tick shells out to docker, which is
    exactly the kind of thing that fails at 3am for reasons that resolve
    themselves by the next tick."""
    conn = _db()
    while True:
        try:
            _sweep_once(conn, idle_window_ns=idle_window_ns)
        except Exception as e:  # noqa: BLE001 -- a bad tick must not end the loop
            print(f"reveille-launch: sweep tick failed ({e.__class__.__name__}:"
                  f" {e}) -- retrying in {interval_s}s", file=sys.stderr,
                  flush=True)
        if stop.wait(interval_s):
            return


# ---- role templates (DES-005 sec 9) ------------------------------------------------
# DRAFTS: the operator edits these before public use (8469 ruling -- do not
# treat as final text). The chosen role's text rides REVEILLE_ROLE_PROMPT into
# the container, where the entrypoint writes it into ~/.claude/CLAUDE.md; its
# NAME is what the agent passes to brief(role=...) so hive doctrine ranks to it.
ROLE_PROMPTS = {
    "architect": (
        "You design and review; you do not implement. Produce design docs and "
        "rulings, review branches, and merge as acceptance. Verify gates "
        "yourself rather than trusting a report. When you rule, say what is "
        "binding and why; record durable rulings in the hive so the fleet "
        "reads them at boot. Prefer one clear invariant over three special "
        "cases."),
    "senior-dev": (
        "You implement slices on feature branches and ship them green. One "
        "slice = one branch = one ship message naming branch and head. Run "
        "the full gate before shipping and state what you ran. Flag deltas "
        "from the design rather than slipping them. Amendments append "
        "commits; never force-push over a reviewed head."),
    "senior-ui-ux": (
        "You own interface, interaction and accessibility. Design for the "
        "least-privilege default: the easy path should be the correct one. "
        "Every destructive or authority-changing action gets an explicit "
        "per-item confirm. Escape all untrusted text at render. State the "
        "keyboard and screen-reader path for anything you add, and prefer "
        "removing a control over explaining it."),
    "senior-devops": (
        "You own deploy, infrastructure and observability. Deploy follows "
        "main; a deploy that cannot be rolled back is not done. Snapshot "
        "before migrations. Instrument what you ship: if it breaks at 3am, "
        "the log line that explains it must already exist. Never signal a "
        "process by name on a host that also runs it in a container."),
}


# ---- HTTP API (DES-005 P1): the browser's path to the launcher --------------------
# The broker stays docker-free (G4); the LAUNCHER grows the web surface. AuthN is
# the broker's: every request's session cookie is forwarded to broker /me and the
# principal's own name becomes the ONLY user the request can touch -- cross-user
# isolation by construction, no user parameter exists on the wire.

def principal_from_me(body):
    """Resolve broker /me JSON to a principal name, or None. Pure. A first-run
    broker ({'setup': true}) has no users and therefore no principals.
    (is_admin was captured here through P2 and never used; deleted per the
    8477 ruling -- a dormant privilege field must not sit in an auth path.)"""
    if not isinstance(body, dict) or body.get("setup") or not body.get("name"):
        return None
    return {"user": body["name"]}


def _broker_json(auth_url, cookie_header, method, path, body=None):
    """One session-forwarded broker call (the _broker_me pattern, generalized
    for P3's server-side compose). Returns parsed JSON or None on any failure
    -- fails closed, exactly like authn."""
    if not cookie_header:
        return None
    req = urllib.request.Request(
        auth_url.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Cookie": cookie_header, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _broker_me(auth_url, cookie_header):
    return principal_from_me(_broker_json(auth_url, cookie_header, "GET", "/me"))


def mint_bound_token(auth_url, cookie_header, agent, rooms):
    """P3: mint the agent's bound state-tier token THROUGH the broker's
    existing session routes, server-side with the user's own forwarded cookie
    -- the browser never holds the secret, the launcher holds it for this
    call's lifetime only, and the broker learns nothing new (same POST /tokens
    + PATCH room-attach the Tokens tab issues). Raises LaunchError."""
    t = _broker_json(auth_url, cookie_header, "POST", "/tokens",
                     {"label": agent, "agent_name": agent, "mem_tier": "state"})
    if not isinstance(t, dict) or not t.get("secret"):
        raise LaunchError("broker refused the token mint")
    for rid in rooms:
        out = _broker_json(auth_url, cookie_header, "PATCH",
                           f"/tokens/{t['id']}", {"room": rid, "attach": True})
        if out is None:
            raise LaunchError(f"broker refused room attach for {rid}")
    return t["id"], t["secret"]


def revoke_minted_token(auth_url, cookie_header, token_id):
    """Best-effort cleanup when a provision fails AFTER its mint succeeded --
    otherwise every failed create leaves a live orphaned credential (observed
    on the P3 gate's own first run)."""
    _broker_json(auth_url, cookie_header, "DELETE", f"/tokens/{token_id}")


def login_need(prof, agent_names):
    """Will this user NEED a login to proceed? Pure. The predicate is the
    CONDITION -- a human about to be blocked -- not its exclusions (ruling
    8648, correcting 8633): the shipped version counted only EXISTING agents,
    so a user who set claude_mode=home-login before creating their first
    agent saw nothing at sign-in and met the provision refusal instead. The
    directive must arrive BEFORE the wall. needed = the GLOBAL mode resolves
    home-login OR any existing agent's mode does; needed_by still names the
    affected agents so the copy can say who, and is empty on the
    before-first-agent path where the copy says "before you can create one".
    Token-mode users still see nothing -- the noise concern 8633 protected,
    intact."""
    needed_by = sorted(
        a for a in agent_names
        if resolve_credentials(prof, a)["claude_mode"] == "home-login")
    needed = bool(needed_by) or \
        resolve_credentials(prof, "")["claude_mode"] == "home-login"
    return {"needed": needed, "needed_by": needed_by}


def claude_login_state(user, base=None):
    """A READING of the user's login home (operator requirement 2026-07-30:
    login state is managed in the settings area, and its absence is the first
    thing a signing-in user is told). present + the file's mtime, computed
    fresh on every call, no values, nothing cached -- the same discipline as
    every other derived state here. Pure aside from the stat."""
    path = os.path.join(user_auth_root(user, base), ".credentials.json")
    try:
        return {"present": True,
                "logged_in_at_ns": os.stat(path).st_mtime_ns}
    except OSError:
        return {"present": False, "logged_in_at_ns": None}


def revoke_bound_tokens(auth_url, cookie_header, agent):
    """Destroy's counterpart to the broker's mint-time supersede: an agent
    that no longer exists must not leave a live credential answering to its
    name. Lists the SESSION USER's own tokens (that is all GET /tokens
    returns) and revokes those bound to this agent. Best-effort like
    revoke_minted_token -- the broker's supersede-on-next-mint catches any
    residue if this call never lands."""
    out = _broker_json(auth_url, cookie_header, "GET", "/tokens")
    for t in (out or {}).get("tokens", []):
        if t.get("agent_name") == agent:
            _broker_json(auth_url, cookie_header, "DELETE", f"/tokens/{t['id']}")


def lifecycle_state(docker_status, has_files, hive):
    """The FOUR states a name can be in, from three independent readings
    (operator requirement 2026-07-30). Pure -- the join is the whole feature,
    so it is testable without docker, disk or a broker.

      running / stopped  a container exists       -> watch, or start
      retired            no container, files kept -> recreate resumes its
                         local ~/.claude and repos verbatim
      erased             no container, no files, but the HIVE remembers it
                         -> recreate resumes from memories, lessons, its own
                         state note and the room's history. THIS is the state
                         nothing could previously say, so an erased agent read
                         as gone forever when it was never gone at all.
      unknown            nothing anywhere -> a new agent

    Derived per call from live readings, never stored: a lifecycle flag that
    could lapse would be the deaf-agent shape wearing a lifecycle hat."""
    if docker_status in ("running", "restarting", "paused"):
        return "running"
    if docker_status and docker_status != "absent":
        return "stopped"
    if has_files:
        return "retired"
    if hive and (hive.get("messages") or hive.get("memories")
                 or hive.get("lessons")):
        return "erased"
    return "unknown"


# ---- edit-in-place (reconfig 2) ----------------------------------------
# The fields a human may change on an agent that already exists. Anything not
# here is not editable through this path: rooms ride the credential rotation
# below, and identity (user, agent name) is not a field at all.
EDITABLE_FIELDS = ("repo_url", "role", "append", "model", "image")


def split_role_prompt(prompt, role_prompts):
    """Recover (role, append) from the prompt text a container actually holds.

    The agents POST handler builds the env value as ROLE_PROMPTS[role] plus,
    optionally, "\\n\\n" + the user's append. Nothing stores the two halves, so
    an edit form that could not split them would have to show one opaque blob
    and make the user retype their own additions to keep them. Longest matching
    prefix wins, so a role whose text starts with a shorter role's text still
    resolves to the specific one.

    Pure, and returns ("", prompt) for a prompt no known role explains -- a
    hand-rolled prompt is preserved as an append rather than silently dropped,
    because dropping it is how an edit form eats the thing it was opened to
    change."""
    prompt = (prompt or "").strip()
    if not prompt:
        return "", ""
    best, best_len = "", 0
    for name, text in (role_prompts or {}).items():
        t = (text or "").strip()
        if t and prompt.startswith(t) and len(t) > best_len:
            best, best_len = name, len(t)
    if not best:
        return "", prompt
    return best, prompt[best_len:].strip()


def effective_config(env_lines, image, role_prompts):
    """The config the RUNNING container was actually given.

    `env_lines` is docker's Config.Env (a list of "K=V"), `image` its real
    image. This is the read-back half of edit-in-place and it reads the
    CONTAINER, not the request that created it: a provision returns 200 the
    moment the container comes up, which says the call succeeded and says
    nothing about whether any field took (contract cea8b522).

    Pure, so the whole read-back is testable with no docker socket -- which is
    the only way the agent who owns this UI can test it at all."""
    env = {}
    for line in env_lines or ():
        k, sep, v = str(line).partition("=")
        if sep:
            env[k] = v
    role, append = split_role_prompt(env.get("REVEILLE_ROLE_PROMPT", ""),
                                     role_prompts)
    return {"repo_url": env.get("REVEILLE_REPO_URL", ""),
            "role": role, "append": append,
            "model": env.get("ANTHROPIC_MODEL", ""),
            "image": image or ""}


def config_diff(requested, actual):
    """Per field: what was asked for, what is now running, and whether they
    agree. Only fields the caller actually SUBMITTED are judged -- an absent
    field means "leave it", and reporting it as applied would be inventing a
    result for something nobody asked for.

    This is what the UI renders. It never renders the POST's status code,
    because a status code cannot distinguish "changed" from "came up with the
    old value"."""
    out = {}
    for k in EDITABLE_FIELDS:
        if requested is None or k not in requested:
            continue
        want = (requested.get(k) or "").strip()
        have = ((actual or {}).get(k) or "").strip()
        out[k] = {"requested": want, "actual": have, "applied": want == have}
    return out


def read_container_config(user, agent):
    """effective_config for a live container, or None when there is none.

    None is not an error: a retired agent has files and no container, and the
    honest answer to "what is running" is then nothing at all. The form shows
    that as no-container rather than as an empty form, which would read as an
    agent configured with blanks."""
    res = _docker("inspect", "-f", "{{json .Config.Env}}\t{{.Config.Image}}",
                  container_name(user, agent), check=False, capture=True)
    if res.returncode != 0:
        return None
    env_json, _, image = (res.stdout or "").strip().partition("\t")
    try:
        env = json.loads(env_json)
    except ValueError:
        return None
    return effective_config(env, image.strip(), ROLE_PROMPTS)


def agent_rooms_now(auth_url, cookie_header, agent):
    """The rooms this agent is CURRENTLY a member of, as room IDs.

    Read from the presence call the launcher already makes with the user's
    forwarded cookie (ruling 8699): no new broker call, no new authority, no
    sixth deputy question. Returns IDs because that is what the token attach
    takes -- verified against store.presence, which carries room_id under the
    key "room", not the display name.

    May be EMPTY for a stopped agent whose member rows have been reaped. The
    caller must refuse rather than mint a credential attached to no room."""
    pres = _broker_json(auth_url, cookie_header, "GET", "/presence") or {}
    out = []
    for a in pres.get("agents") or []:
        if a.get("name") == agent and a.get("room") and a["room"] not in out:
            out.append(a["room"])
    return out


def _agent_status(conn, user, hive_by_name=None):
    """Every name this user could act on, in one list: their provisioned
    containers PLUS names the hive still remembers whose container is gone.
    The second half is what makes recovery reachable -- a destroyed agent
    leaves no launcher record at all, so before this it was invisible and its
    resume path may as well not have existed."""
    hive_by_name = hive_by_name or {}
    rows = conn.execute(
        "SELECT * FROM containers WHERE user=? ORDER BY agent", (user,)).fetchall()
    out, listed = [], set()
    for r in rows:
        st = (_docker("inspect", "-f", "{{.State.Status}}", r["container"],
                      check=False, capture=True).stdout or "").strip() or "absent"
        agent = r["agent"]
        listed.add(agent)
        hive = hive_by_name.get(agent, {})
        has_files = os.path.isdir(data_root(user, agent))
        out.append({"agent": agent, "container": r["container"], "status": st,
                    "image": r["image"], "repo_url": r["repo_url"],
                    "created_ns": r["created_ns"],
                    "state": lifecycle_state(st, has_files, hive),
                    "has_files": has_files, "hive": hive})
    for agent, hive in sorted(hive_by_name.items()):
        # present = alive right now somewhere else (a host agent, another
        # user's container). Remembered-and-gone is a recovery case;
        # remembered-and-working is not, and offering to recreate it would
        # invite someone to duplicate a live identity.
        if agent in listed or hive.get("present") or not ROLE_RE.match(agent or ""):
            continue
        has_files = os.path.isdir(data_root(user, agent))
        state = lifecycle_state("absent", has_files, hive)
        if state == "unknown":
            continue
        out.append({"agent": agent, "container": container_name(user, agent),
                    "status": "absent", "image": "", "repo_url": "",
                    "created_ns": hive.get("last_ns") or 0,
                    "state": state, "has_files": has_files, "hive": hive})
    return out


# The Agents tab (DES-005 sec 2: served by the LAUNCHER). One static page; the
# broker's session cookie reaches this origin for free (cookies are host-
# scoped, not port-scoped), so every fetch below is same-origin and authed.
# All dynamic text goes through esc() -- agent names and room names are user
# input.
# ---- the served UI: a flat file, not a string literal (operator, msg 8634) --
# The agents page lives at src/reveille/ui/launcher/index.html, beside the bus
# page (one ui/ tree, split by OWNER: this service serves only its own
# subtree). Same rules as the broker's loader (ruling 8635): a FIXED table of
# named files, never a request-derived path; read per-request so
# REVEILLE_UI_PATH (dev override, edited live) takes effect immediately; the
# override ANNOUNCES itself in the serve banner and on the page.
_UI_PACKAGED = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            os.pardir, "src", "reveille", "ui", "launcher")
_UI_FILES = frozenset({"index.html"})


def _ui_override():
    return os.environ.get("REVEILLE_UI_PATH") or None


def _ui_read(name):
    """name comes from CODE (the route table), never from the request."""
    if name not in _UI_FILES:
        raise LaunchError(f"not a served UI file: {name!r}")
    with open(os.path.join(_ui_override() or _UI_PACKAGED, name)) as f:
        return f.read()


def build_api(auth_url):
    """The starlette app. Deferred imports: the CLI paths must keep working on a
    box that only ever uses the launcher as a CLI."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    def guarded(fn):
        async def wrapped(request):
            p = _broker_me(auth_url, request.headers.get("cookie"))
            if p is None:
                return JSONResponse({"error": "no session"}, status_code=401)
            conn = _db()
            try:
                return await fn(request, p, conn)
            except LaunchError as e:
                return JSONResponse({"error": str(e)}, status_code=400)
            finally:
                conn.close()
        return wrapped

    @guarded
    async def agents(request, p, conn):
        if request.method == "GET":
            # The hive half comes from the BROKER, with this request's own
            # cookie -- the launcher never learns bus facts on its own
            # authority, exactly as it never mints on its own authority.
            seen = _broker_json(auth_url, request.headers.get("cookie"),
                                "GET", "/agents-seen") or {}
            hive = {a["name"]: a for a in seen.get("agents", [])}
            return JSONResponse({"agents": _agent_status(conn, p["user"], hive)})
        d = await request.json()
        # P3: no token in the body + rooms ticked -> the LAUNCHER mints the
        # bound state-tier token through the broker's session routes with THIS
        # request's forwarded cookie. The secret exists in this frame and the
        # docker-run child env only -- the browser never sees it at all. A
        # token in the body (the P1/CLI path) is still honored.
        token, minted_id = d.get("token") or "", None
        if not token and d.get("rooms"):
            minted_id, token = mint_bound_token(
                auth_url, request.headers.get("cookie"),
                (d.get("agent") or "").strip(), list(d["rooms"]))
        role = (d.get("role") or "").strip()
        try:
            if role and role not in ROLE_PROMPTS:
                raise LaunchError(f"unknown role {role!r}")
            prompt = ROLE_PROMPTS.get(role, "")
            if d.get("append"):
                prompt = (prompt + "\n\n" + str(d["append"])).strip()
            name, kind = provision_agent(
                conn, p["user"], (d.get("agent") or "").strip(),
                (d.get("repo_url") or "").strip(), token,
                image=d.get("image") or DEFAULT_IMAGE,
                network=d.get("network") or DEFAULT_NETWORK,
                broker=d.get("broker") or DEFAULT_BROKER,
                boot_cmd=d.get("boot_cmd"), replace=bool(d.get("replace")),
                role_prompt=prompt or None,
                model=(d.get("model") or "").strip() or None)
        except (LaunchError, subprocess.CalledProcessError):
            if minted_id:
                # A failed provision must not leave a live orphaned credential.
                revoke_minted_token(auth_url, request.headers.get("cookie"),
                                    minted_id)
            raise
        return JSONResponse({"container": name, "agent": d.get("agent"),
                             "credential": kind})

    @guarded
    async def agent(request, p, conn):
        name = request.path_params["agent"]
        if request.method == "DELETE":
            _known_agent(conn, p["user"], name)
            destroy_agent(conn, p["user"], name,
                          purge=request.query_params.get("purge") == "1")
            # The agent's bus credential dies with it (same doctrine as
            # grants): a destroyed agent must not leave a live token
            # answering to its name.
            revoke_bound_tokens(auth_url, request.headers.get("cookie"), name)
            return JSONResponse({"destroyed": name})
        _known_agent(conn, p["user"], name)
        rows = _agent_status(conn, p["user"])
        out = next(r for r in rows if r["agent"] == name)
        # P3 live status: the same health-by-presence the CLI polls, read
        # through the broker with the USER's forwarded cookie -- the launcher
        # holds no broker credential of its own.
        pres = _broker_json(auth_url, request.headers.get("cookie"),
                            "GET", "/presence")
        agents_list = (pres or {}).get("agents", [])
        out["live"] = health_from_presence(agents_list, name)
        return JSONResponse(out)

    @guarded
    async def my_rooms(request, p, conn):
        """The form's room checkboxes: exactly the set assign_room admits for
        this user (owned + member + public), read from the broker with the
        forwarded cookie. IDs and names only."""
        me = _broker_json(auth_url, request.headers.get("cookie"), "GET", "/me")
        if not isinstance(me, dict):
            raise LaunchError("broker unreachable")
        rooms = []
        for key in ("owned", "member", "public"):
            for r in me.get(key) or []:
                rooms.append({"id": r["id"], "name": r["name"], "kind": key})
        # role_prompts: full text, so the form can SHOW what a role supplies
        # before the user augments it (operator ask, U1) -- prompts are not
        # secrets, they land in the container's CLAUDE.md verbatim.
        return JSONResponse({"rooms": rooms,
                             "roles": sorted(ROLE_PROMPTS),
                             "role_prompts": ROLE_PROMPTS,
                             "models": MODEL_SUGGESTIONS})

    @guarded
    async def rooms_create(request, p, conn):
        """POST {name}: the deputy's FIFTH call (M3 ruling, msg 8489) -- create
        the room the user just named in the first-run chain. Create and nothing
        else: the bound on the deputy is on KIND, not count -- rename, delete,
        retention, public-flip and invite/remove stay broker-UI actions, and if
        the launcher ever needs them the answer is a link, not a sixth call."""
        d = await request.json()
        name = (d.get("name") or "").strip()
        if not name:
            raise LaunchError("room name required")
        r = _broker_json(auth_url, request.headers.get("cookie"),
                         "POST", "/rooms", {"name": name})
        if not isinstance(r, dict) or not r.get("id"):
            raise LaunchError("broker refused the room create")
        return JSONResponse({"id": r["id"], "name": r["name"]})

    @guarded
    async def agent_lifecycle(request, p, conn):
        name = request.path_params["agent"]
        verb = request.path_params["verb"]
        if verb not in ("start", "stop"):
            raise LaunchError(f"unknown verb {verb!r}")
        _known_agent(conn, p["user"], name)
        _docker(verb, container_name(p["user"], name), check=False, capture=True)
        return JSONResponse({verb: name})

    @guarded
    async def agent_grants(request, p, conn):
        name = request.path_params["agent"]
        if request.method == "GET":
            rows = conn.execute(
                "SELECT id, grantee, mode, issued_ns, expiry_ns, revoked_ns "
                "FROM grants WHERE user=? AND agent=? ORDER BY issued_ns",
                (p["user"], name)).fetchall()
            return JSONResponse({"grants": [dict(r) for r in rows]})
        d = await request.json()
        out = mint_grant(conn, p["user"], name,
                         (d.get("grantee") or "someone").strip(),
                         d.get("mode") or "viewer",
                         int(d.get("ttl") or 86400))
        return JSONResponse(out)   # the attach URL appears here ONCE, never again

    @guarded
    async def agent_grant(request, p, conn):
        revoke_grant(conn, p["user"], request.path_params["agent"],
                     request.path_params["gid"], actor=f"web:{p['user']}")
        return JSONResponse({"revoked": request.path_params["gid"]})

    # ---- browser-mediated login (rulings 8644) -----------------------------
    # THE CODE-RELAY BOUNDARY, stated where the endpoints live: this is not a
    # send-keys surface. The target container is DERIVED from the session
    # (never named on the wire), it must be the user's own PENDING login,
    # exactly one relay is accepted per login container, only a code-shaped
    # string travels (literally, via tmux load-buffer on stdin -- no key-name
    # interpretation, no argv), and the code is never parsed, stored, logged,
    # echoed, or audited. A general keystroke endpoint would be an RCE
    # primitive against every agent container; this one cannot address them.

    def _login_pending(user):
        return _docker("inspect", "-f", "{{.State.Running}}",
                       login_container_name(user), check=False,
                       capture=True).stdout.strip() == "true"

    @guarded
    async def login_start(request, p, conn):
        if _login_pending(p["user"]):
            raise LaunchError(
                f"a login is already pending for {p['user']} -- finish or "
                f"cancel it first (one login at a time; two flows would race "
                f"on one credential file)")
        _docker("rm", "-f", login_container_name(p["user"]), check=False,
                capture=True)   # a stopped leftover holds the name
        ensure_login_home(p["user"], DEFAULT_IMAGE)
        env = dict(os.environ, SEED=_SEED_BASE)
        subprocess.run(login_bg_argv(p["user"], DEFAULT_IMAGE, DEFAULT_NETWORK),
                       env=env, check=True, stdout=subprocess.DEVNULL)
        _audit("LOGIN_START", user=p["user"])
        return JSONResponse({"started": True})

    @guarded
    async def login_status(request, p, conn):
        """The pending login as a READING: the credential file fresh, the pane
        fresh. Completion is the FILE appearing -- when it has, the container
        is done being useful and is removed here, on observation."""
        out = dict(claude_login_state(p["user"]))
        if out["present"] and _login_pending(p["user"]):
            _docker("rm", "-f", login_container_name(p["user"]), check=False,
                    capture=True)
            _audit("LOGIN_DONE", user=p["user"])
        if not out["present"] and _login_pending(p["user"]):
            pane = _docker("exec", login_container_name(p["user"]), "tmux",
                           "capture-pane", "-t", "login", "-p",
                           check=False, capture=True).stdout
            st = parse_login_pane(pane or "")
            out["pending"] = st["stage"]
            out["url"] = st["url"]
            out["relayed"] = _docker(
                "exec", login_container_name(p["user"]), "test", "-f",
                "/tmp/.code-relayed", check=False, capture=True).returncode == 0
        return JSONResponse(out)

    @guarded
    async def login_code(request, p, conn):
        name = login_container_name(p["user"])   # derived, never from the wire
        if not _login_pending(p["user"]):
            raise LaunchError("no login is pending -- start one first")
        if _docker("exec", name, "test", "-f", "/tmp/.code-relayed",
                   check=False, capture=True).returncode == 0:
            raise LaunchError(
                "a code was already relayed to this login -- if it failed, "
                "cancel and start a fresh login (each flow takes one code)")
        code = ((await request.json()).get("code") or "").strip()
        if not _LOGIN_CODE_RE.match(code):
            raise LaunchError("that does not look like a login code")
        # stdin -> tmux buffer -> paste: the code never rides argv and cannot
        # be interpreted as key names.
        r = subprocess.run(
            ["docker", "exec", "-i", name, "tmux", "load-buffer",
             "-b", "relay", "-"], input=code.encode(), capture_output=True)
        if r.returncode != 0:
            raise LaunchError("the login container refused the relay -- "
                              "check its status")
        _docker("exec", name, "tmux", "paste-buffer", "-d", "-b", "relay",
                "-t", "login", check=True, capture=True)
        _docker("exec", name, "tmux", "send-keys", "-t", "login", "Enter",
                check=True, capture=True)
        _docker("exec", name, "touch", "/tmp/.code-relayed", check=True,
                capture=True)
        _audit("LOGIN_CODE_RELAY", user=p["user"])   # THAT it happened; never what
        return JSONResponse({"relayed": True})

    @guarded
    async def login_cancel(request, p, conn):
        _docker("rm", "-f", login_container_name(p["user"]), check=False,
                capture=True)
        _audit("LOGIN_CANCEL", user=p["user"])
        return JSONResponse({"cancelled": True})

    @guarded
    async def agent_config(request, p, conn):
        """Edit an agent in place (reconfig 2).

        GET  -> what is ACTUALLY RUNNING, for the form to prefill from, plus
                the agent's current rooms. Never the creation request: the
                request is what someone once asked for, the container is what
                happened.
        PUT  -> apply, then READ IT BACK and answer with a per-field verdict.
                The read-back lives HERE rather than in the page so that no
                future client can skip it and report a 200 as success
                (contract cea8b522: coming up is not having applied it).

        Every edit re-provisions, and provisioning mints -- and a mint
        supersedes the agent's previous bound token. So EVERY edit rotates the
        agent's bus credential, not only a rooms change (ruling 8606 as
        confirmed at 8699). The page says so before the click; this endpoint
        says so again in its answer, because the two surfaces drift."""
        name = request.path_params["agent"]
        _known_agent(conn, p["user"], name)
        cookie = request.headers.get("cookie")
        if request.method == "GET":
            return JSONResponse({
                "agent": name,
                "config": read_container_config(p["user"], name),
                "editable": list(EDITABLE_FIELDS),
                "rooms": agent_rooms_now(auth_url, cookie, name),
                "rotates_credential": True})
        d = await request.json()
        requested = {k: (d.get(k) or "").strip()
                     for k in EDITABLE_FIELDS if k in d}
        role = requested.get("role", "")
        if role and role not in ROLE_PROMPTS:
            raise LaunchError(f"unknown role {role!r}")
        # Rooms are explicit on the wire, prefilled by GET from presence. A
        # stopped agent's member rows may have been reaped, so deriving them
        # here could silently mint a credential attached to NO room -- an
        # agent that boots, joins nothing, and looks fine. Refuse and name the
        # fix instead.
        rooms = [r for r in (d.get("rooms") or []) if r]
        if not rooms:
            raise LaunchError(
                f"no rooms given for {name}: an edit re-mints its credential, "
                f"and a token attached to no room is an agent that boots and "
                f"joins nothing -- tick at least one room in the form (it is "
                f"prefilled from where the agent is now)")
        prompt = ROLE_PROMPTS.get(role, "")
        if requested.get("append"):
            prompt = (prompt + "\n\n" + requested["append"]).strip()
        minted_id, token = mint_bound_token(auth_url, cookie, name, rooms)
        try:
            provision_agent(
                conn, p["user"], name, requested.get("repo_url", ""), token,
                image=requested.get("image") or DEFAULT_IMAGE,
                network=DEFAULT_NETWORK, broker=DEFAULT_BROKER,
                replace=True, role_prompt=prompt or None,
                model=requested.get("model") or None)
        except (LaunchError, subprocess.CalledProcessError):
            revoke_minted_token(auth_url, cookie, minted_id)
            raise
        # THE READ-BACK. Not "did the call return", but "what does the
        # container hold now" -- the only question whose answer the user cares
        # about.
        actual = read_container_config(p["user"], name)
        diff = config_diff(requested, actual)
        _audit("RECONFIG", user=p["user"], agent=name,
               fields=",".join(sorted(diff)) or "none",
               applied=",".join(sorted(k for k, v in diff.items()
                                       if v["applied"])) or "none")
        return JSONResponse({"agent": name, "config": actual, "fields": diff,
                             "applied": all(v["applied"] for v in diff.values()),
                             "credential_rotated": True})

    @guarded
    async def profile(request, p, conn):
        """GET: quotas + usage + MASKED credentials (set/absent, never values)
        + the custody/rotation notes stated out loud (sec 3 / Q1 / Q2).
        PUT {claude_token?, github_token?, repo_url?}: set globals; empty
        string clears a field. Values land in the 0600 profile file and are
        echoed by nothing, this response included."""
        if request.method == "PUT":
            d = await request.json()
            save_profile(p["user"],
                         merge_profile(load_profile(p["user"]), d))
        q = _quotas_for(conn, p["user"])
        used = conn.execute("SELECT count(*) FROM containers WHERE user=?",
                            (p["user"],)).fetchone()[0]
        prof = load_profile(p["user"])
        cl = claude_login_state(p["user"])
        cl.update(login_need(
            prof, [r["agent"] for r in conn.execute(
                "SELECT agent FROM containers WHERE user=?", (p["user"],))]))
        return JSONResponse({"user": p["user"], "quotas": q,
                             "containers": used,
                             "credentials": masked_profile(prof),
                             "claude_login": cl,
                             "notes": PROFILE_NOTES,
                             "disk_note": "disk_gb recorded, not yet enforced"})

    @guarded
    async def agent_profile(request, p, conn):
        """PUT: per-agent credential overrides (sec 3 table). Same masking,
        same file, nested under agents.<name>."""
        d = await request.json()
        save_profile(p["user"],
                     merge_profile(load_profile(p["user"]), d,
                                   agent=request.path_params["agent"]))
        return JSONResponse(
            {"agent": request.path_params["agent"],
             "credentials": masked_profile(load_profile(p["user"]))})

    # ---- attach-through-proxy (DES-006 U4) ---------------------------------
    # The launcher is a DUMB PIPE BETWEEN TWO CHECKS, never an authority over
    # attachment: it refuses unless the request carries a session principal AND
    # that agent belongs to that user, and the grant token is still verified AT
    # THE CONTAINER by attach-gate, untouched. The forward target is resolved
    # here from our own records -- no client-supplied host, port or scheme ever
    # reaches it.
    def _attach_target(cookie, agent):
        """(target, principal) or (None, None). Both gates, in one place."""
        p = _broker_me(auth_url, cookie)
        if p is None:
            return None, None
        conn = _db()
        try:
            _known_agent(conn, p["user"], agent)   # ownership; raises otherwise
        except LaunchError:
            return None, None
        finally:
            conn.close()
        return container_addr(p["user"], agent), p

    async def attach_http(request):
        """ttyd's page and its assets. Only the sub-path under /attach/<agent>/
        is forwarded, and it is joined onto an address we resolved -- a path
        that tries to name a host is refused rather than dialled."""
        from starlette.responses import PlainTextResponse, RedirectResponse, Response
        agent = request.path_params["agent"]
        sub = request.path_params.get("path", "")
        if "://" in sub or sub.startswith("//"):
            return PlainTextResponse("bad path", status_code=400)
        target, _p = _attach_target(request.headers.get("cookie"), agent)
        if target is None:
            # One message for "not signed in", "not yours" and "no such agent":
            # a distinguishable refusal is an ownership oracle.
            return PlainTextResponse("no attachable agent by that name",
                                     status_code=403)
        if not request.url.path.startswith(f"/attach/{agent}/"):
            # ttyd builds its websocket URL RELATIVE to the document, so the
            # trailing slash is load-bearing: without it /ws resolves one level
            # too high and the terminal never connects.
            q = f"?{request.url.query}" if request.url.query else ""
            return RedirectResponse(f"/attach/{agent}/{q}", status_code=307)
        url = f"http://{target}/{sub}"
        if request.url.query:
            url += f"?{request.url.query}"

        def fetch():
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read(), r.headers.get("Content-Type", "")
        try:
            status, body, ctype = await asyncio.to_thread(fetch)
        except urllib.error.HTTPError as e:
            return Response(e.read(), status_code=e.code)
        except OSError:
            return PlainTextResponse("agent terminal unreachable",
                                     status_code=502)
        return Response(body, status_code=status, media_type=ctype or None)

    async def attach_ws(websocket):
        """The terminal itself. Same two gates before a single byte moves."""
        import websockets
        agent = websocket.path_params["agent"]
        target, _p = _attach_target(websocket.headers.get("cookie"), agent)
        if target is None:
            await websocket.close(code=4403)
            return
        subs = list(websocket.scope.get("subprotocols") or [])
        q = websocket.scope.get("query_string", b"").decode()
        upstream_url = f"ws://{target}/ws" + (f"?{q}" if q else "")
        try:
            up = await websockets.connect(upstream_url,
                                          subprotocols=subs or None,
                                          max_size=None, open_timeout=10)
        except Exception:
            await websocket.close(code=1011)
            return
        await websocket.accept(subprotocol=subs[0] if subs else None)

        async def to_upstream():
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    return
                data = msg.get("text")
                await up.send(data if data is not None else msg.get("bytes", b""))

        async def to_client():
            async for data in up:
                if isinstance(data, str):
                    await websocket.send_text(data)
                else:
                    await websocket.send_bytes(data)

        pump = [asyncio.create_task(to_upstream()),
                asyncio.create_task(to_client())]
        try:
            done, pending = await asyncio.wait(
                pump, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            for t in done:
                with contextlib.suppress(Exception):
                    t.result()
        finally:
            await up.close()
            with contextlib.suppress(Exception):
                await websocket.close()

    async def health(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("ok")

    async def ui(_request):
        from starlette.responses import HTMLResponse
        page = _ui_read("index.html")
        override = _ui_override()
        if override:
            # Visible only under the override -- a dev deployment must never
            # be mistaken for the artifact's own UI (ruling 8635).
            page = page.replace(
                "<body>",
                f'<body><div style="position:fixed;bottom:4px;right:8px;'
                f'z-index:99;opacity:.6;font:11px monospace;color:#e0a44c">'
                f'UI OVERRIDE: {html.escape(override)}</div>', 1)
        return HTMLResponse(page)

    # grants routes BEFORE the {verb} catch-all: starlette matches in order, and
    # POST /agents/x/grants must be a mint, never an "unknown verb 'grants'".
    return Starlette(routes=[
        Route("/health", health),
        Route("/ui", ui),
        # Behind the proxy /agents is stripped to "/", so the page must answer
        # there too -- same handler, no redirect to a path the browser cannot
        # see (DES-006 U3).
        Route("/", ui),
        Route("/rooms-mine", my_rooms),
        Route("/rooms", rooms_create, methods=["POST"]),
        Route("/agents", agents, methods=["GET", "POST"]),
        Route("/agents/{agent}", agent, methods=["GET", "DELETE"]),
        Route("/agents/{agent}/grants", agent_grants, methods=["GET", "POST"]),
        Route("/agents/{agent}/grants/{gid}", agent_grant, methods=["DELETE"]),
        Route("/agents/{agent}/profile", agent_profile, methods=["PUT"]),
        Route("/agents/{agent}/config", agent_config, methods=["GET", "PUT"]),
        Route("/agents/{agent}/{verb:str}", agent_lifecycle, methods=["POST"]),
        Route("/profile", profile, methods=["GET", "PUT"]),
        Route("/login/start", login_start, methods=["POST"]),
        Route("/login/status", login_status),
        Route("/login/code", login_code, methods=["POST"]),
        Route("/login/pending", login_cancel, methods=["DELETE"]),
        # /ws BEFORE the catch-all: the terminal's socket must never be served
        # as a static asset. WebSocketRoute and Route do not collide (different
        # scope types), so both /attach/{agent}/ws entries can coexist.
        WebSocketRoute("/attach/{agent}/ws", attach_ws),
        Route("/attach/{agent}", attach_http),
        Route("/attach/{agent}/{path:path}", attach_http),
    ])


def cmd_serve(a):
    import uvicorn
    # Refuse before binding, then say out loud WHERE state lives: the data root
    # follows whoever started the process, so an unnoticed wrong one silently
    # moves every agent's home and its history (msg 8499). Printing it makes a
    # wrong root visible at boot instead of after an agent's memory vanishes.
    server = _require_docker()
    db_path = os.environ.get(
        "REVEILLE_LAUNCH_DB", os.path.expanduser("~/.reveille/launcher.db"))
    lock = _singleton(os.path.join(os.path.dirname(db_path), ".launcher.lock"))
    app = build_api(a.auth_url)
    idle_ns = int(a.idle_hours * 3600 * 10**9)
    # WHAT IS SERVING, in the log, at boot. This process used to be started from
    # whichever tree the spawn line pointed at, and the answer lived in a
    # person's head (msg 8568). A commit + branch here is what makes "the
    # operator is on unreviewed code" something you can SEE.
    src = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    commit, branch, version = source_stamp(src)
    print(f"reveille-launch {version} ({commit} {branch}) "
          f"api on {a.host}:{a.port} (auth: {a.auth_url}/me)\n"
          f"  source  {src}\n"
          f"  docker  {server}\n  data    {DEFAULT_DATA}\n  db      {db_path}\n"
          f"  sweep   every {a.sweep_seconds}s"
          + (f"\n  UI OVERRIDE {_ui_override()} (not the artifact's own UI)"
             if _ui_override() else "")
          + (f", idle stop after {a.idle_hours}h" if idle_ns else ", idle stop OFF"),
          flush=True)
    # Daemon thread: it holds nothing that must be flushed, so it dies with the
    # process rather than delaying a restart by up to one interval. The Event is
    # what makes the interval interruptible -- time.sleep would keep the thread
    # alive past shutdown for no benefit.
    stop = threading.Event()
    threading.Thread(target=_sweep_forever, name="sweep",
                     args=(a.sweep_seconds, idle_ns, stop), daemon=True).start()
    try:
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    finally:
        stop.set()
        lock.close()
    return 0


def cmd_join_here(a):
    """Bootstrap THIS user's terminal for one agent identity (DES-003 2.4):
    after this, open a terminal, run `claude`, you are on the bus. Walks the
    same PROVISION_CHECKLIST container provisioning satisfies, host-side."""
    if not ROLE_RE.match(a.role):
        die(f"bad role {a.role!r}: lowercase alnum + dash, 2-64 chars")
    token = read_secret(f"bound broker token for {a.role}")
    if not token:
        die("no token given (stdin empty and no prompt)")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    home = os.path.expanduser("~")
    done = {}

    # env: the ONLY file the token ever lands in, mode 0600 before a byte is
    # written (open with restrictive mode, not chmod-after).
    frag_dir = os.path.join(home, ".reveille")
    os.makedirs(frag_dir, exist_ok=True)
    frag = os.path.join(frag_dir, f"{a.role}.env")
    fd = os.open(frag, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"export REVEILLE_AGENT_ROLE={a.role}\n"
                f"export REVEILLE_URL={a.broker}\n"
                f"export REVEILLE_TOKEN={token}\n")
    # One marker block in .bashrc points at the LAST-joined role; re-running
    # join-here (same or another role) replaces it. ponytail: one identity per
    # user shell -- per-terminal multi-identity stays the `agent` launcher's job.
    rc = os.path.join(home, ".bashrc")
    marker = "# reveille join-here"
    lines = []
    if os.path.exists(rc):
        with open(rc) as f:
            lines = [ln for ln in f.read().splitlines()
                     if marker not in ln]
    lines.append(f"source {frag}  {marker}")
    with open(rc, "w") as f:
        f.write("\n".join(lines) + "\n")
    done["env"] = f"{frag} (0600) + .bashrc source line"

    # register: identical shape to `make register` -- headers are ${VAR}
    # templates expanded per session from env, so the CONFIG carries no token.
    subprocess.run(["claude", "mcp", "remove", "reveille", "--scope", "user"],
                   capture_output=True)
    r = subprocess.run(
        ["claude", "mcp", "add", "--transport", "http", "--scope", "user",
         "reveille", f"{a.broker}/mcp",
         "--header", "Authorization: Bearer ${REVEILLE_TOKEN:-}",
         "--header", "X-Agent: ${REVEILLE_AGENT_ROLE:-unset-agent}"],
        capture_output=True, text=True)
    if r.returncode != 0:
        die(f"claude mcp add failed: {r.stderr.strip()}")
    done["register"] = "reveille -> " + a.broker + "/mcp (headers from env)"

    # hook: the waked supervisor + watcher gate, user scope, idempotent.
    r = subprocess.run([sys.executable, os.path.join(repo, "scripts",
                                                     "install-hook")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        die(f"install-hook failed: {r.stderr.strip()}")
    done["hook"] = r.stdout.strip()

    # path: console scripts out of the repo venv. Symlinks, so a repo rebuild
    # updates them for free; stale links are replaced.
    bindir = os.path.join(home, ".local", "bin")
    os.makedirs(bindir, exist_ok=True)
    linked = []
    for tool in ("wake", "wake-watch", "reveille-waked"):
        src = os.path.join(repo, ".venv", "bin", tool)
        if not os.path.exists(src):
            die(f"{src} missing -- run `uv sync` in {repo} first")
        dst = os.path.join(bindir, tool)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(dst)
        os.symlink(src, dst)
        linked.append(tool)
    done["path"] = f"{', '.join(linked)} -> {bindir}"

    # spool
    spooldir = os.path.join(os.environ.get(
        "REVEILLE_SPOOL", os.path.join(home, ".reveille", "spool")), a.role)
    for sub in ("tmp", "new"):
        os.makedirs(os.path.join(spooldir, sub), exist_ok=True)
    done["spool"] = spooldir

    for step, what in PROVISION_CHECKLIST:
        print(f"  [ok] {step:9s} {what}\n       {done[step]}")
    print(f"joined: open a new terminal (or `source {frag}`) and run `claude` "
          f"-- the Stop hook arms the rest.")
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="reveille-launch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="provision one agent container for one user")
    n.add_argument("user", help="owning user (namespaces the container + data root)")
    n.add_argument("agent", help="agent name (the bus identity)")
    n.add_argument("repo_url")
    n.add_argument("--broker", default=DEFAULT_BROKER,
                   help="broker URL the AGENT dials (container DNS; "
                        f"default {DEFAULT_BROKER})")
    n.add_argument("--health-url", default=DEFAULT_HEALTH,
                   help="broker URL the LAUNCHER polls from the host "
                        f"(default {DEFAULT_HEALTH})")
    n.add_argument("--network", default=DEFAULT_NETWORK,
                   help="docker network for the agent + broker; `host` is an ops "
                        f"escape hatch, never the default (default {DEFAULT_NETWORK})")
    n.add_argument("--replace", action="store_true",
                   help="re-provision an existing agent (destroys its container; "
                        "the data root is kept)")
    n.add_argument("--image", default=DEFAULT_IMAGE)
    n.add_argument("--timeout", type=int, default=90)
    n.add_argument("--no-wait", action="store_true",
                   help="return after docker run without the presence health wait")
    n.add_argument("--boot-cmd", default=None,
                   help="override the image command (default: claude reveille); "
                        "e.g. agent-probe to health-check without an Anthropic login")
    n.set_defaults(fn=cmd_new)

    sub.add_parser("ls", help="list provisioned containers").set_defaults(fn=cmd_ls)

    for name, fn in (("stop", cmd_stop), ("start", cmd_start)):
        s = sub.add_parser(name)
        s.add_argument("user")
        s.add_argument("agent")
        s.set_defaults(fn=fn)

    d = sub.add_parser("destroy")
    d.add_argument("user")
    d.add_argument("agent")
    d.add_argument("--purge", action="store_true",
                   help="also drop the data root (default keeps everything the "
                        "agent learned; recreate picks it back up)")
    d.set_defaults(fn=cmd_destroy)

    ow = sub.add_parser("owner", help="who owns an agent NAME, and --release to "
                                      "free it (ruling 8660; recorded now, "
                                      "enforced later)")
    ow.add_argument("agent")
    ow.add_argument("--release", action="store_true",
                    help="free the name for anyone to claim -- audited")
    ow.add_argument("--by", default="admin",
                    help="who is releasing it (goes on the audit line)")
    ow.set_defaults(fn=cmd_owner)

    pr = sub.add_parser("profile", help="show (masked) or set stored credentials "
                                        "(DES-005 P2); token values via stdin, "
                                        "never argv")
    pr.add_argument("user")
    pr.add_argument("--agent", default=None,
                    help="set a per-agent override instead of the user global")
    pr.add_argument("--claude-token", action="store_true",
                    help="read a claude credential (setup-token or API key, "
                         "detected by prefix) from stdin/prompt")
    pr.add_argument("--github-token", action="store_true",
                    help="read a github token from stdin/prompt")
    pr.add_argument("--repo-url", default=None)
    pr.add_argument("--claude-mode", default=None, choices=list(CLAUDE_MODES),
                    help="token: env-injected credential, zero-touch "
                         "provisioning (default). home-login: NO token env; "
                         "the agent auths from the file `claude /login` wrote "
                         "into its home (reveille-launch login <user> <agent>, "
                         "once) -- NOT zero-touch")
    pr.add_argument("--clear-claude", action="store_true")
    pr.add_argument("--clear-github", action="store_true")
    pr.set_defaults(fn=cmd_profile)

    q = sub.add_parser("quota", help="show or override one user's quotas (sec 6)")
    q.add_argument("user")
    q.add_argument("--cpus", type=float, default=None)
    q.add_argument("--mem", default=None)
    q.add_argument("--disk-gb", type=int, default=None, dest="disk_gb")
    q.add_argument("--pids", type=int, default=None)
    q.add_argument("--max-containers", type=int, default=None,
                   dest="max_containers")
    q.set_defaults(fn=cmd_quota)

    g = sub.add_parser("grant", help="mint a per-grant URL token (printed once)")
    g.add_argument("user")
    g.add_argument("agent")
    g.add_argument("grantee", help="who this grant names (audit attribution, Q3)")
    g.add_argument("--mode", choices=("viewer", "driver"), default="viewer",
                   help="driver is the agent's whole identity (4.4); default viewer")
    g.add_argument("--ttl", type=int, default=86400,
                   help="seconds until expiry (Q2: 24h default, renew = re-grant)")
    g.set_defaults(fn=cmd_grant)

    gl = sub.add_parser("grants", help="list grant records")
    gl.add_argument("user", nargs="?", default=None)
    gl.add_argument("agent", nargs="?", default=None)
    gl.set_defaults(fn=cmd_grants)

    r = sub.add_parser("revoke", help="kill the grant's session now (<1s)")
    r.add_argument("user")
    r.add_argument("agent")
    r.add_argument("grant_id")
    r.set_defaults(fn=cmd_revoke)

    f = sub.add_parser("flip", help="toggle multi-driver on a container (4.3)")
    f.add_argument("user")
    f.add_argument("agent")
    f.add_argument("state", choices=("on", "off"))
    f.set_defaults(fn=cmd_flip)

    s = sub.add_parser("sweep", help="ONE expiry/revoke tick + audit harvest "
                                     "(4.6) + idle stop (DES-005 7.1); the "
                                     "recurring one runs inside serve")
    s.add_argument("--idle-hours", type=float, default=24.0,
                   help="stop (never destroy) containers idle this long -- no "
                        "attached client, no session activity, no waiting ring "
                        "(default 24; 0 disables)")
    s.set_defaults(fn=cmd_sweep)

    sv = sub.add_parser("serve", help="HTTP API for the browser (DES-005 P1); "
                                      "auth = broker session cookie via /me")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8766)
    sv.add_argument("--auth-url", default=DEFAULT_HEALTH,
                    help="broker URL whose /me resolves the session cookie "
                         f"(default {DEFAULT_HEALTH})")
    sv.add_argument("--sweep-seconds", type=int, default=300, metavar="N",
                    help="how often the 4.6 sweep tick runs INSIDE this process "
                         "-- grant expiry, orphan sessions, idle stop. Serve is "
                         "the scheduler; there is no unit or crontab to install "
                         "(default 300)")
    sv.add_argument("--idle-hours", type=float, default=24.0,
                    help="the sweep stops (never destroys) containers idle this "
                         "long (default 24; 0 disables the idle stop only)")
    sv.set_defaults(fn=cmd_serve)

    pn = sub.add_parser("pin", help="create/fast-forward the tree the launcher "
                                    "is SERVED from (never your checkout)")
    pn.add_argument("--path", default=DEFAULT_PIN,
                    help=f"where the served clone lives (default {DEFAULT_PIN})")
    pn.add_argument("--origin", default="",
                    help="clone URL for a first pin (default: this checkout's "
                         "origin)")
    pn.set_defaults(fn=cmd_pin)

    dbg = sub.add_parser("debug", help="docker run --rm -ti into an agent's "
                                       "exact environment (bash; run claude by "
                                       "hand to watch billing/login behave)")
    dbg.add_argument("user")
    dbg.add_argument("agent")
    dbg.add_argument("--image", default=DEFAULT_IMAGE)
    dbg.add_argument("--network", default=DEFAULT_NETWORK)
    dbg.add_argument("--entrypoint", default="bash",
                     help="override to /usr/local/bin/entrypoint.sh for the "
                          "full boot (then REVEILLE_* env is on you)")
    dbg.add_argument("--force", action="store_true",
                     help="open the shell even while the managed container is "
                          "running (two claudes share one ~/.claude and step on "
                          "each other's state)")
    dbg.set_defaults(fn=cmd_debug)

    lg = sub.add_parser("login", help="interactive `claude /login` for ONE "
                                      "user, shared by ALL their home-login "
                                      "agents as a boot-time copy; re-run "
                                      "anytime to switch accounts, then "
                                      "restart the agents")
    lg.add_argument("user")
    lg.add_argument("--image", default=DEFAULT_IMAGE)
    lg.add_argument("--network", default=DEFAULT_NETWORK)
    lg.set_defaults(fn=cmd_login)

    j = sub.add_parser("join-here",
                       help="bootstrap THIS terminal for one agent (DES-003 2.4)")
    j.add_argument("role")
    j.add_argument("--broker", default="http://127.0.0.1:8765",
                   help="broker URL for this host (default http://127.0.0.1:8765)")
    j.set_defaults(fn=cmd_join_here)
    return p


def main():
    a = build_parser().parse_args()
    raise SystemExit(a.fn(a))


if __name__ == "__main__":
    main()
