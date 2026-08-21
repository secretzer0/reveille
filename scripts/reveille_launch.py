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
import shutil
import sqlite3
import subprocess
import sys
import tempfile
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
DEFAULT_IMAGE = os.environ.get("REVEILLE_AGENT_IMAGE", "reveille-agent:0.2.28")
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


def no_login_refusal(user):
    """THE REFUSAL NAMES THE REACHABLE DOOR, AND ONLY THAT DOOR. This text
    renders verbatim in the web UI's create-agent dialog, whose reader is a
    REMOTE user: the Account tab is one click away and the launcher host is
    unreachable by construction, so naming the CLI there is prescribing a door
    that does not exist for the person reading (operator ruling, 2026-08-13 --
    the first fix named both and the second half was noise). A function rather
    than an inline f-string so the gate asserts over the SENTENCE THE USER
    READS, not over source bytes a line wrap can split mid-phrase."""
    return (f"claude_mode=home-login but {user} has no login on file. Log in "
            f"once -- open the Account tab at the top of this page and use "
            f"its Claude login -- and every agent copies it at boot.")


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


def rfc3339_ns(stamp):
    """docker's .State.StartedAt (RFC3339, nanosecond fraction) as epoch ns,
    0 when unparseable -- and 0 is safe here only because is_idle treats it as
    "no boot reading", which fails toward the other clocks."""
    import datetime
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?"
                 r"(Z|[+-]\d{2}:\d{2})$", (stamp or "").strip())
    if not m:
        return 0
    base, frac, tz = m.groups()
    dt = datetime.datetime.fromisoformat(base + ("+00:00" if tz == "Z" else tz))
    return int(dt.timestamp()) * 10**9 + int((frac or "0").ljust(9, "0")[:9])


def is_idle(attached, last_activity_ns, last_ring_ns, now_ns, window_ns,
            started_at_ns):
    """The 24h-idle-stop decision (sec 7.1), pure. Idle = no attached tmux
    client AND no session activity AND no wake ring inside the window. An agent
    working autonomously overnight shows session activity (its panes update on
    every bus turn) and is never reclaimed -- that case is the point.

    started_at_ns is the container's boot, and it is IN the max (ruling 12401):
    a container never observed has been idle since it BOOTED, not since the
    epoch. Without it, a probe landing in the seconds before tmux comes up read
    (False, 0, 0) and now-0 cleared any window -- the launcher idle-stopped a
    22-second-old container, whose SIGKILL then interrupted a claude
    self-update and bricked the body (measured 2026-08-19, twice). One clock,
    no grace knob: boot-time fixes the race and a 30-day untouched container
    still reclaims."""
    if window_ns <= 0 or attached:
        return False
    newest = max(last_activity_ns or 0, last_ring_ns or 0, started_at_ns or 0)
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


def _add_role_name(conn):
    """Additive: containers.role_name, for launcher databases that predate r3.
    Empty is the honest value for a container provisioned before anything
    recorded it -- the move dialog then asks, exactly as it did before."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(containers)")}
    if "role_name" not in cols:
        conn.execute("ALTER TABLE containers ADD COLUMN role_name TEXT NOT NULL DEFAULT ''")
        conn.commit()


def _add_roll_desired(conn):
    """Additive: containers.roll_desired_running -- the IN-FLIGHT record of
    what a roll FOUND running (ruled 13457, property 1). Set before the
    roll's first mutation, cleared by the per-body restore; an orphaned row
    means a roll died between the start and the restore, and the next sweep
    tick reconciles it. NULL = no roll in flight."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(containers)")}
    if "roll_desired_running" not in cols:
        conn.execute("ALTER TABLE containers ADD COLUMN roll_desired_running INTEGER")
        conn.commit()


def _launcher_tables(conn):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS containers("
        "user TEXT NOT NULL, agent TEXT NOT NULL, repo_url TEXT, container TEXT, "
        "image TEXT, broker_url TEXT, created_ns INTEGER, "
        # r3 (ruling 11938): the role this agent was last provisioned with, so a
        # MOVE can carry it instead of asking again. Deliberately NOT called
        # `role` -- that name belongs to the pre-P0 schema where it meant the
        # AGENT NAME, and _migrate_launcher_db keys its entire rewrite on seeing
        # a column by that name. Reusing it would make every modern database
        # look like an ancient one to the migration.
        "role_name TEXT NOT NULL DEFAULT '', "
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
    # 30s busy timeout (13245 half 2, repro first as ruled at 13280): the
    # roll step's open waited the full 5s DEFAULT (Python sets
    # busy_timeout=5000 on connect -- a 5000 pragma here was refused as a
    # no-op) and died at 5.006s, exhaustion not refusal. Not the 0.000s
    # read-to-write upgrade signature; that hypothesis was tested on this
    # path and did not reproduce. HONEST PROVENANCE OF THE 30 (13323 rider):
    # the only PRODUCTION fact is that the real holder outlived 5.006s -- its
    # true duration is UNMEASURED; 30 is six times the REPRO FIXTURE's 8s
    # holder, a modelled number, not an observed one. The slow-open line
    # below is what turns "30 is enough" from a belief into a series of
    # observations; a holder that outlives even this turns the #189-honest
    # trip red and names itself.
    _t0 = time.monotonic()
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name='containers'").fetchone():
        _migrate_launcher_db(conn)
    _launcher_tables(conn)
    _add_role_name(conn)
    _add_roll_desired(conn)
    waited = time.monotonic() - _t0
    if waited > 1.0:
        # instrument-and-wait (13323): the next contention MEASURES the
        # holder instead of us modelling it
        print(f"launcher.db open waited {waited:.1f}s (lock contention)",
              file=sys.stderr)
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


def _record(conn, user, agent, repo_url, image, broker_url, role_name=""):
    conn.execute(
        "INSERT OR REPLACE INTO containers"
        "(user, agent, repo_url, container, image, broker_url, created_ns, role_name) "
        "VALUES(?,?,?,?,?,?,?,?)",
        (user, agent, repo_url, container_name(user, agent), image,
         broker_url, time.time_ns(), role_name or ""))
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
    ensure_launcher_dir(os.path.dirname(path))
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
    if updates.get("multi_driver") and updates["multi_driver"] not in ("on", "off"):
        raise LaunchError(f"multi_driver must be \"on\" or \"off\", "
                          f"not {updates['multi_driver']!r}")
    for k in ("claude_token", "github_token", "repo_url", "claude_mode",
              "multi_driver"):
        if k in updates:
            if updates[k]:
                target[k] = updates[k]
            else:
                target.pop(k, None)
    return prof


def resolve_multi_driver(prof, agent):
    """Per-agent override > user global, the resolve_credentials shape.
    THE PROFILE IS THE DECLARATION (ruled 13448): provision copies it into
    the container as the ~/.multi-driver marker; `flip` mutates only the
    runtime copy. -> "on" | "off" | ""."""
    a = (prof.get("agents") or {}).get(agent) or {}
    return a.get("multi_driver") or prof.get("multi_driver") or ""


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
#
# THE CONTAINER ENDS ITSELF ON THE CREDENTIAL, which is why the wait loop reads
# the file and not only the session. `claude /login` does not leave when the
# login lands, so waiting on the tmux session waited on a REPL that outlives the
# flow: the container ran on, and the only thing that ever removed it was a
# browser polling /login/status -- which the Account tab stops doing the moment
# the credential appears. The SUCCESSFUL login was the one case with no reaper.
# The container is the first to know it succeeded; it should not need a witness
# in order to stop.
_LOGIN_BOOT = r"""
: "${FP0:?the stamped credential fingerprint must ride in -- existence is not a sentinel (13353)}"
python3 -c "$SEED"
tmux new-session -d -s login -x 220 -y 50 "claude /login"
for i in $(seq 1 120); do
  if tmux capture-pane -t login -p 2>/dev/null | grep -q "Select login method"; then
    tmux send-keys -t login 1 Enter
    break
  fi
  sleep 0.5
done
fp() { python3 -c 'import os
try: print(os.stat(os.path.expanduser("~/.claude/.credentials.json")).st_mtime_ns)
except OSError: print("nothing")'; }
while tmux has-session -t login 2>/dev/null; do
  [ "$(fp)" != "$FP0" ] && break
  sleep 1
done
"""

# The sign-in line AS THE PANE SHOWS IT (captured in-container 2026-07-30,
# msg 8643; re-captured 2026-08-20 after the operator's field report 13384):
# host claude.com BARE, anchored right after the scheme so the claude 2.1.237
# startup banner's "Learn more: https://support.claude.com/..." line can
# never win the match, path oauth/authorize required, then the query --
# HARD-WRAPPED by tmux across rows. The newline join in parse_login_pane
# reassembles the wrap; the URL CHARSET is what stops the join from swallowing
# pane decoration (the banner's U+2594 underline row rode a [^\s]+ match
# straight into the operator's browser). A pane whose path moves fails
# CLOSED -- no url -- and parse_login_pane names that state out loud (13390:
# fail-closed must not be fail-silent).
_LOGIN_URL_RE = re.compile(
    r"https://claude\.com/[A-Za-z0-9/._-]*oauth/authorize\?"
    r"[A-Za-z0-9%&=+._~-]+")
# The code is OPAQUE: never parsed beyond "printable, sane length, no control
# characters" -- enough to refuse key sequences, never enough to learn it.
_LOGIN_CODE_RE = re.compile(r"^[A-Za-z0-9_\-#%.~]{4,256}$")


def login_container_name(user):
    return f"rev-{user}-login"


# ONE constant for the two sites that must agree: login_bg_argv writes it,
# login_status inspects it. Two literals is how they drift.
_LOGIN_FP_LABEL = "reveille-login-fp0"


def login_bg_argv(user, image, network, fp0, data_base=None):
    """The DETACHED login container (browser path): same mounts and same
    no-credential-env rule as login_argv, but it holds a tmux the launcher can
    read, and its boot script advances the picker itself. fp0 is the credential
    fingerprint stamped BEFORE the flow (13353): the boot script waits for it
    to MOVE, and the label lets login_status judge the same way. Pure."""
    return ["docker", "run", "-d",
            "--name", login_container_name(user),
            "--network", network,
            "--label", f"{_LOGIN_FP_LABEL}={fp0}",
            "-v", f"{user_auth_root(user, data_base)}:/home/agent/.claude",
            "-e", "SEED",   # the first-run seed rides env BY NAME, like every secretish value
            "-e", "FP0",
            "--entrypoint", "sh", image, "-c", _LOGIN_BOOT]


def parse_login_pane(text):
    """The pending login's stage, READ from its pane. Pure. The pane is the
    truth (ruling 8644): expiry and failure surface here, not from exit codes.
    -> {stage: starting|picker|awaiting-code|url-missing|failed, url: str|None}"""
    url = None
    m = _LOGIN_URL_RE.search(text.replace("\n", ""))   # tmux wraps the long URL
    if m:
        url = m.group(0)
    if url:
        return {"stage": "awaiting-code", "url": url}
    if "Paste code here" in text:
        # FAIL-CLOSED, NEVER FAIL-SILENT (13390): the prompt is up but no URL
        # matched -- a vendor path move lands HERE as a state the page can say
        # out loud, not as an eternal "starting...".
        return {"stage": "url-missing", "url": None}
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
    ensure_launcher_dir(os.path.dirname(root), image)
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
        if d.get("multi_driver"):   # also a choice, never a secret
            out["multi_driver"] = d["multi_driver"]
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


def own_path_argv(path, image, uid, gid, recursive=False):
    """The chown container's argv for ONE path, pure for the same reason
    own_dirs_argv is: the uid-critical shape has to be assertable on a box
    where the accident cannot hide.

    NOT -R by default, and that default is the whole point. The per-user dir
    has the AGENT dirs beneath it, and those belong to the image's uid -- a
    recursive chown here would take them and undo _own_agent_dirs on every
    call. One directory, not a tree."""
    return ["docker", "run", "--rm", "--user", "0:0", "--entrypoint", "chown",
            "-v", f"{path}:/own", image,
            *(["-R"] if recursive else []), f"{uid}:{gid}", "/own"]


# data/<user>/ stays CLOSED at 0700, and the fix is about OWNERSHIP alone.
#
# I proposed 0711 and the suite refused it: test_profile_file_is_0600_and_holds
# _the_only_copy pins this mode because profile.json lives here and carries the
# user's github and claude tokens. 0600 protects the file; 0700 stops another
# host user traversing to the agent homes beneath it, which hold the credential
# copied in at boot. Once the launcher OWNS this directory nothing else needs to
# traverse it -- dockerd sets up every bind mount as root -- so the loosening
# bought nothing and widened exposure on exactly the multi-user box this whole
# change exists to support. The pin was right and it fired the moment I moved
# the contract, which is what pins are for.
USER_DIR_MODE = 0o700


def ensure_launcher_dir(path, image=None):
    """THIS directory belongs to the LAUNCHER. Everything BELOW it belongs to
    the IMAGE. Splitting those two was the operator's ruling on 2026-08-01.

    They were one uid until tonight, and only by accident: the image bakes
    ARG UID=1000 and the operator happens to be uid 1000, so `os.chmod` on a
    dir the launcher did not own had never once been asked to fail. Move the
    launcher to any other account -- which is what a real deployment is -- and
    ensure_login_home dies with EPERM on this exact path, taking the browser
    login and the credential save with it. chmod needs OWNERSHIP, not write
    permission, which is why a mode fix alone could never have worked.

    So the launcher TAKES ownership rather than assuming it, through the same
    privilege it uses everywhere else: not CAP_CHOWN, which it may not have,
    but the docker socket, which it has by definition. Non-recursive, so the
    agent homes underneath keep the image's uid.

    Idempotent by CONVERGENCE: an already-correct dir is chowned by nobody and
    re-chmodded to the same mode, and a wrong-owner dir is repaired rather than
    reported as present. The other shape is what made `reveille init` confirm a
    broken machine (msg 9067)."""
    os.makedirs(path, mode=USER_DIR_MODE, exist_ok=True)
    if os.stat(path).st_uid != os.getuid():
        subprocess.run(own_path_argv(path, image or DEFAULT_IMAGE,
                                     os.getuid(), os.getgid()),
                       check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    os.chmod(path, USER_DIR_MODE)
    return path


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

def provision_refusal(conn, user, agent, repo_url, *, boot_cmd=None,
                      role_prompt=None, replace=False):
    """Why this provision cannot happen, or None. NO SIDE EFFECTS.

    THE MINT IS THE LAST IRREVERSIBLE ACT (architect 11911, from a live
    incident). POST /agents minted the bound token FIRST -- which supersedes
    the identity's previous credential the moment it lands -- and only then
    called provision_agent, which refused on a missing role prompt. The refusal
    was correct and the cleanup revoked the new token, and the two together
    left reveille-red-shirt with NO live credential at all: identity fine, both
    bodies dark, gone from presence, and nothing said why. Every check that can
    refuse must therefore be answerable BEFORE anything is minted, which is
    what this function is for. provision_agent still calls it, so the CLI path
    and the invariant cannot drift apart."""
    for label, val in (("user", user), ("agent", agent)):
        if not ROLE_RE.match(val or ""):
            return f"bad {label} {val!r}: lowercase alnum + dash, 2-64 chars"
    name = container_name(user, agent)
    if _exists(name) and not replace:
        return (f"{name} already exists -- destroy first, or pass replace to "
                f"re-provision (its data root is kept)")
    quotas = _quotas_for(conn, user)
    others = conn.execute(
        "SELECT count(*) FROM containers WHERE user=? AND agent!=?",
        (user, agent)).fetchone()[0]
    if others >= quotas["max_containers"]:
        return (f"{user} is at their container cap ({quotas['max_containers']}); "
                f"destroy one or raise it with `quota {user} --max-containers N`")
    _, _, kind = credential_env(resolve_credentials(load_profile(user), agent, repo_url))
    if kind == "none" and not boot_cmd:
        return (f"no claude credential for {user}/{agent}: save one in the profile "
                f"(claude setup-token output), or choose claude_mode=home-login "
                f"and log in once (reveille-launch login {user}) -- an agent must "
                f"never inherit whatever credential is lying around in the "
                f"launcher's environment")
    if not (role_prompt or "").strip() and not boot_cmd:
        return (f"no role prompt for {user}/{agent}: pass one with --role-prompt (or "
                f"pick a role in the Agents form) -- an agent provisioned without one "
                f"boots with no CLAUDE.md role block and knows what it is only from "
                f"its bus name")
    return None


def provision_agent(conn, user, agent, repo_url, token, *, image=DEFAULT_IMAGE,
                    network=DEFAULT_NETWORK, broker=DEFAULT_BROKER,
                    boot_cmd=None, replace=False, role_prompt=None, model=None):
    """The one provisioning path (CLI and HTTP share it). Validates, enforces
    the per-user cap, lays the per-agent data root, runs the container. The
    token exists only in this frame and the docker-run child's env -- never
    argv, never any store. Raises LaunchError; returns the container name."""
    why = provision_refusal(conn, user, agent, repo_url, boot_cmd=boot_cmd,
                            role_prompt=role_prompt, replace=replace)
    if why:
        raise LaunchError(why)
    name = container_name(user, agent)
    # Re-provision, the cap, the credential and the role prompt are all decided
    # by provision_refusal above -- BEFORE a caller mints anything (11911).
    quotas = _quotas_for(conn, user)
    if not token:
        raise LaunchError("no token given")

    # P2: the user's stored credentials, override > global > request repo_url.
    prof = load_profile(user)
    creds = resolve_credentials(prof, agent, repo_url)
    cred_names, cred_env, kind = credential_env(creds)
    # No claude credential + the default boot (claude itself) = an agent that
    # will hang at a login prompt nobody is watching -- or worse, find some
    # ambient credential and bill it. REFUSE and name the fix. A custom
    # boot_cmd (agent-probe, gates) runs no claude and needs no credential:
    # the requirement follows the thing that consumes it. home-login mode has
    # the same requirement one level up: its credential is the file in the
    # USER's login home, checkable right here -- refuse-not-create, with the
    # one command that fixes it named.
    auth_mount = None
    if kind == "home-login":
        auth_mount = user_auth_root(user)
        if not os.path.isfile(os.path.join(auth_mount, ".credentials.json")) \
                and not boot_cmd:
            raise LaunchError(no_login_refusal(user))
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

    # The agent's home, nothing else's (sec 4). The USER root belongs to the
    # LAUNCHER and is traverse-only for everyone else; the agent dirs under it
    # belong to the AGENT. Two uids, split deliberately -- see
    # ensure_launcher_dir for why they were one by accident until 2026-08-01.
    root = data_root(user, agent)
    user_root = os.path.dirname(root)
    ensure_launcher_dir(user_root, image)
    for sub in ("claude", "repos"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)
    _own_agent_dirs(root, image)

    _ensure_network(network, broker)
    if replace:
        leave_roll_record(user, agent, to_image=image,
                          why="re-provision (--replace)")
        _docker("rm", "-f", name, check=False, capture=True)
    argv = docker_run_argv(user, agent, image, network, quotas,
                           boot_cmd=boot_cmd, extra_env=extra_env,
                           auth_mount=auth_mount)
    subprocess.run(argv, env=env, check=True, stdout=subprocess.DEVNULL)
    if resolve_multi_driver(prof, agent) == "on":
        # THE MARKER IS THE STATE (13443/13444, measured 13446): the writable
        # layer keeps it across stop/start, and the rm THIS function performs
        # on --replace is what loses it -- so the declaration is re-copied
        # here, on every path that creates a container. ONE SPELLING with the
        # gate and flip: "$HOME/.multi-driver", never a hardcoded home.
        r = _docker("exec", name, "sh", "-c", 'touch "$HOME/.multi-driver"',
                    check=False, capture=True)
        if r.returncode != 0:
            # Never fail the provision over it, never keep quiet about it
            # (13450): a silent revert is the defect this slice exists for.
            print(f"multi-driver declaration did NOT land on {user}/{agent} "
                  f"(exec rc={r.returncode}) -- flip it by hand: "
                  f"reveille-launch flip {user} {agent} on", file=sys.stderr)
    _record(conn, user, agent, creds["repo_url"], image, broker,
            role_name=split_role_prompt(role_prompt or "", ROLE_PROMPTS)[0])
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


# ---- UPGRADE IN PLACE (ruling 11600, proposal 11599) --------------------------------
# A bound token exists in exactly two places: the broker's store and the env of a
# container the launcher provisioned. The launcher may CARRY it between those --
# in this frame, into the docker-run child's env -- and never PARK it: no db, no
# log, no file, no HTTP body. That is not an amendment of the never-at-rest rule;
# provision already holds the token in-process, and read_container_config already
# reads Config.Env. Same reach against a same-host reader, nothing new. So an
# image bump is one call: read the token (and the gate secret, so grants signed
# against it survive) from the container we created, run the new image with the
# same everything, prove it is up, and only then throw the old one away.

def env_of(env_lines):
    """docker's Config.Env (a list of "K=V") as a dict. Pure."""
    env = {}
    for line in env_lines or ():
        k, sep, v = str(line).partition("=")
        if sep:
            env[k] = v
    return env


CARRIED_PREFIXES = ("REVEILLE_",)
CARRIED_NAMES = ("ANTHROPIC_MODEL",)


def carried_env_diff(old_env, new_env):
    """The names whose value the upgrade was supposed to carry and did not:
    every REVEILLE_* variable (token, gate secret, role, url, repo, role prompt)
    and ANTHROPIC_MODEL -- set-equality on names AND values, both directions.
    Image-derived variables (PATH, HOME, the image's own) are not compared: they
    are what an upgrade is allowed to change. Pure; empty list = carried."""
    def keep(k):
        return k.startswith(CARRIED_PREFIXES) or k in CARRIED_NAMES
    a = {k: v for k, v in (old_env or {}).items() if keep(k)}
    b = {k: v for k, v in (new_env or {}).items() if keep(k)}
    return sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))


def boot_cmd_of(container_cmd, image_cmd):
    """The boot_cmd provision was given, recovered from the container: None when
    the container runs its image's own command, else the container's command as
    one shell string (docker_run_argv splits it again). Pure."""
    if not container_cmd or list(container_cmd) == list(image_cmd or []):
        return None
    import shlex
    return " ".join(shlex.quote(str(x)) for x in container_cmd)


def _inspect_container(name):
    """Config.Env, image, cmd, network, running -- or None when there is no such
    container (which is the prompt path, not an error)."""
    res = _docker("inspect", "-f",
                  "{{json .Config.Env}}\t{{.Config.Image}}\t{{json .Config.Cmd}}"
                  "\t{{.HostConfig.NetworkMode}}\t{{.State.Running}}\t{{.Image}}",
                  name, check=False, capture=True)
    if res.returncode != 0:
        return None
    try:
        (env_json, image, cmd_json, network, running,
         image_id) = (res.stdout or "").strip().split("\t")
        return {"env": env_of(json.loads(env_json)), "image": image.strip(),
                "cmd": json.loads(cmd_json) or [], "network": network.strip(),
                "running": running.strip() == "true",
                # .Image is the ID of what the container actually RUNS;
                # .Config.Image is only the name it was created with. They come
                # apart the moment a tag is rebuilt (ruling 8433's ambiguity).
                "image_id": image_id.strip()}
    except ValueError:
        return None


def _image_cmd(image):
    res = _docker("image", "inspect", "-f", "{{json .Config.Cmd}}", image,
                  check=False, capture=True)
    if res.returncode != 0:
        return []
    try:
        return json.loads((res.stdout or "").strip() or "[]") or []
    except ValueError:
        return []


def _token_alive(health_url, agent, token):
    """The carried token still opens the broker: presence answers. A 401/403 is
    a dead token (revoked, rotated) and must not be rolled forward; any other
    failure is the broker not answering, which is also not a time to upgrade."""
    try:
        _presence(health_url, agent, token)
        return None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return f"its bound token is dead (broker said {e.code}) -- re-provision it"
        return f"broker answered {e.code} on presence -- not upgrading"
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return f"broker unreachable at {health_url} ({e}) -- not upgrading"


def image_id_of(tag):
    """The image ID a tag currently points at, or "" when docker cannot say --
    and "" deliberately makes the same-image check below FALSE, because a
    comparison that cannot be made must not refuse an upgrade."""
    res = _docker("image", "inspect", "-f", "{{.Id}}", tag,
                  check=False, capture=True)
    return (res.stdout or "").strip() if res.returncode == 0 else ""


def record_found_state(conn, user, agent, running):
    """Property 1 (13457): what the roll FOUND, written durably BEFORE the
    first mutation. A MAINTENANCE ACTION RETURNS THE SYSTEM TO THE STATE IT
    FOUND -- and a crash between the start and the restore must leave a row
    that knows the body should be down, never a lost local variable (the
    thirteen-body field case rode exactly that gap)."""
    conn.execute("UPDATE containers SET roll_desired_running=? "
                 "WHERE user=? AND agent=?",
                 (1 if running else 0, user, agent))
    conn.commit()


def restore_found_state(conn, user, agent, was_running):
    """Property 2: per body, immediately after the health gate -- never at
    the end of the walk, which is the race the hand-restore lost in the
    field. Clears the in-flight record and returns the FINAL-state word for
    the log line: the line names the outcome, not the transition."""
    if not was_running:
        _docker("stop", container_name(user, agent), check=False, capture=True)
    conn.execute("UPDATE containers SET roll_desired_running=NULL "
                 "WHERE user=? AND agent=?", (user, agent))
    conn.commit()
    return "running" if was_running else "stopped as found"


def upgrade_agent(conn, user, agent, image=DEFAULT_IMAGE, *, health_url=DEFAULT_HEALTH,
                  timeout=120):
    """Re-provision an agent's container on `image`, carrying the bound token
    (and gate secret) from the container the launcher created. Same repo, boot
    command, network, role, model, quotas; same data root. The old container is
    renamed aside, stopped, and destroyed only after the new one is running,
    has written its boot report, and shows in the broker's presence -- else the
    new one is destroyed and the old one put back (and started again if it was
    running). Never two containers holding driver state: OLD is stopped before
    NEW starts. Refuses a name launcher.db does not record (the launcher never
    adopts a container it did not create), a container with no token to carry
    (that is today's prompt path, verbatim), and a dead token.

    The token exists here, in the docker-run child's env, and in the old
    container's env -- never in argv, launcher.db, the audit line, the log or
    the HTTP answer. Raises LaunchError; returns {"from", "to", "was_running"}."""
    _known_agent(conn, user, agent)
    name = container_name(user, agent)
    old = _inspect_container(name)
    if old is None or not old["env"].get("REVEILLE_TOKEN"):
        raise LaunchError(
            f"{user}/{agent} has no container to carry a token from -- re-provision it "
            f"(reveille-launch new {user} {agent} <repo_url> --replace, or the Agents "
            f"form), which asks for the token")
    # IDENTITY, NOT NAME (found 2026-08-19). Two builds raced onto one tag: a
    # container rolled from the stale build could not be rolled again, because
    # this check compared the tag STRING it was created with against the tag
    # string requested -- equal, while the image underneath had moved. The 8433
    # ambiguity living inside the check meant to enforce 8433. A container is
    # "already on" an image only when the ID it runs is the ID the tag names.
    if old.get("image_id") and old["image_id"] == image_id_of(image):
        raise LaunchError(f"{user}/{agent} is already on {image}")
    token = old["env"]["REVEILLE_TOKEN"]
    why = _token_alive(health_url, agent, token)
    if why:
        raise LaunchError(f"not upgrading {user}/{agent}: {why}")
    broker = old["env"].get("REVEILLE_URL") or DEFAULT_BROKER
    repo_url = old["env"].get("REVEILLE_REPO_URL", "")
    prof = load_profile(user)
    creds = resolve_credentials(prof, agent, repo_url)
    cred_names, cred_env, kind = credential_env(creds)
    boot_cmd = boot_cmd_of(old["cmd"], _image_cmd(old["image"]))
    if kind == "none" and not boot_cmd:
        raise LaunchError(
            f"no claude credential for {user}/{agent} in the profile -- the upgraded "
            f"container would boot to a login prompt nobody is watching; save one first")
    auth_mount = user_auth_root(user) if kind == "home-login" else None
    env = dict(os.environ, REVEILLE_AGENT_ROLE=agent, REVEILLE_URL=broker,
               REVEILLE_REPO_URL=repo_url, REVEILLE_TOKEN=token,
               REVEILLE_GATE_SECRET=old["env"].get("REVEILLE_GATE_SECRET") or secrets.token_hex(32),
               **cred_env)
    extra_env = list(cred_names)
    for k in ("REVEILLE_ROLE_PROMPT", "ANTHROPIC_MODEL"):
        if old["env"].get(k):
            extra_env.append(k)
            env[k] = old["env"][k]
    root = data_root(user, agent)
    ino_before = os.stat(root).st_ino if os.path.isdir(root) else None
    quotas = _quotas_for(conn, user)
    argv = docker_run_argv(user, agent, image, old["network"] or DEFAULT_NETWORK, quotas,
                           boot_cmd=boot_cmd, extra_env=extra_env, auth_mount=auth_mount)
    record_found_state(conn, user, agent, old["running"])
    prev = f"{name}.prev"
    _docker("rm", "-f", prev, check=False, capture=True)      # a leftover from a crashed upgrade
    leave_roll_record(user, agent, to_image=image, why="upgrade (image roll)")
    if old["running"]:
        _docker("stop", name, check=False, capture=True)         # never two containers with driver state
    _docker("rename", name, prev, check=True, capture=True)

    def rollback(reason):
        _docker("rm", "-f", name, check=False, capture=True)
        _docker("rename", prev, name, check=False, capture=True)
        if old["running"]:
            _docker("start", name, check=False, capture=True)
        # The old world is back exactly as found -- the in-flight record is
        # satisfied, not orphaned (13457 property 1).
        conn.execute("UPDATE containers SET roll_desired_running=NULL "
                     "WHERE user=? AND agent=?", (user, agent))
        conn.commit()
        _audit("UPGRADE-ROLLBACK", user=user, agent=agent, image=image, reason=reason)
        raise LaunchError(f"upgrade of {user}/{agent} to {image} failed: {reason} -- "
                          f"the old container ({old['image']}) is back"
                          + (" and running" if old["running"] else ""))

    try:
        subprocess.run(argv, env=env, check=True, stdout=subprocess.DEVNULL)
    except (subprocess.CalledProcessError, OSError) as e:
        rollback(f"docker run refused ({e})")
    if resolve_multi_driver(prof, agent) == "on":
        # Same re-copy as provision: the upgrade's rm is a path that loses
        # the marker, and the idle auto-roll reaches HERE with no human
        # present to notice a silent revert (13448).
        r = _docker("exec", name, "sh", "-c", 'touch "$HOME/.multi-driver"',
                    check=False, capture=True)
        if r.returncode != 0:
            print(f"multi-driver declaration did NOT land on {user}/{agent} "
                  f"(exec rc={r.returncode}) -- flip it by hand: "
                  f"reveille-launch flip {user} {agent} on", file=sys.stderr)
    # HEALTH BEFORE DESTROY (11600 s3): running, boot report written, presence shows it.
    if not wait_healthy(health_url, agent, token, timeout):
        rollback(f"not present on the broker within {timeout}s")
    if read_boot_report(user, agent) is None:
        rollback("no boot report")
    new = _inspect_container(name)
    if new is None or not new["running"]:
        rollback("new container not running")
    # THE SAME AGENT (11600 s4): carried env set-equal, same data root.
    diff = carried_env_diff(old["env"], new["env"])
    if diff:
        rollback("carried env differs: " + ", ".join(diff))
    if ino_before is not None and os.stat(root).st_ino != ino_before:
        rollback("data root moved")
    _docker("rm", "-f", prev, check=False, capture=True)
    conn.execute("UPDATE containers SET image=? WHERE user=? AND agent=?", (image, user, agent))
    conn.commit()
    final = restore_found_state(conn, user, agent, old["running"])
    _audit("UPGRADE", user=user, agent=agent, image_from=old["image"],
           image_to=image, final=final)
    return {"from": old["image"], "to": image, "was_running": old["running"],
            "final": final}


def behind_image(conn, image=DEFAULT_IMAGE):
    """Records whose image is not `image` -- what `make up` prints and `upgrade
    --all` walks. launcher.db only: never `docker ps`."""
    return [dict(r) for r in conn.execute(
        "SELECT user, agent, image FROM containers WHERE image IS NOT ? ORDER BY user, agent",
        (image,)).fetchall()]


# ---- DES-006 s7.2: auto-roll on deploy, under an idle rule (ruling 11807) ---
# A bumped image reaches a RUNNING container only through a re-provision that
# nobody schedules (lesson image-fix-never-reaches-a-running-container), and
# the fix for that must never become "restart whatever is working". So a
# behind container rolls only when it is IDLE, and idle is READ -- from the
# grants table, from its spool, and from the broker -- never guessed from a
# heartbeat, which every container about to be replaced also has.
# Unset and EMPTY mean the same thing here: a deploy passes optional variables
# through as ${VAR:-}, so "absent" arrives as "" (the broker crash-looped on
# exactly that, 2026-08-18). A present non-number is an operator typo and is
# refused by name rather than silently defaulted.
def _env_min(name, default):
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        die(f"{name}={raw!r} is not a number of minutes -- fix it or unset it "
            f"(unset means {default})")


ROLL_IDLE_MIN = _env_min("REVEILLE_ROLL_IDLE_MIN", 10.0)


def roll_block(*, grants, spool, unread, last_send_ns, now_ns, window_ns):
    """Why this container must not be rolled right now, or "" if it may be.
    Pure. Order is worst-first, so the sentence a human reads names the most
    telling reason rather than the first one checked."""
    if grants:
        return f"{grants} live attach grant" + ("s" if grants > 1 else "")
    if spool:
        return f"{spool} unprocessed ring" + ("s" if spool > 1 else "") + " in its spool"
    if unread:
        return f"{unread} unread message" + ("s" if unread > 1 else "") + " waiting"
    if last_send_ns and now_ns - last_send_ns < window_ns:
        return f"sent to the bus {max(1, (now_ns - last_send_ns) // (60 * 10**9))} min ago"
    return ""


def live_grants(conn, user, agent, now_ns):
    """Attach grants still good: not revoked, not expired. A live grant means a
    human may be at that terminal RIGHT NOW -- the sweep is what expires them,
    so this reads the same rows the sweep would act on."""
    return conn.execute(
        "SELECT count(*) FROM grants WHERE user=? AND agent=? AND revoked_ns IS NULL "
        "AND expiry_ns > ?", (user, agent, now_ns)).fetchone()[0]


def _spool_pending(user, agent):
    """Rings DELIVERED and not yet processed, counted inside the container.
    None = could not look, which the caller treats as busy: an unknown is not
    an idle."""
    res = _docker("exec", container_name(user, agent), "sh", "-c",
                  "find ~/.reveille/spool -path '*/new/*' -name '*.ring' 2>/dev/null | wc -l",
                  check=False, capture=True)
    if res.returncode != 0:
        return None
    txt = (res.stdout or "").strip()
    return int(txt) if txt.isdigit() else None


def _broker_activity(broker_url, role, token):
    """{last_send_ns, unread} for the agent, read with its OWN carried token --
    the same credential upgrade_agent carries and the same never-at-rest rule
    (11600). None = the broker did not answer, which is never a time to roll."""
    req = urllib.request.Request(
        broker_url.rstrip("/") + "/agent/activity",
        headers={"Authorization": f"Bearer {token}", "X-Agent": role})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.load(r)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None


def roll_reason(conn, user, agent, *, health_url=DEFAULT_HEALTH, now_ns=None,
                window_ns=None):
    """Why NOT to roll this behind container, or "" if it is idle. Every input
    is read: grants from launcher.db, rings from the container's spool, unread
    and last-send from the broker. A container that is not running is idle by
    construction -- nothing is at its keyboard and nothing is mid-task."""
    now_ns = now_ns or time.time_ns()
    window_ns = int(ROLL_IDLE_MIN * 60 * 10**9) if window_ns is None else window_ns
    g = live_grants(conn, user, agent, now_ns)
    if g:
        return roll_block(grants=g, spool=0, unread=0, last_send_ns=0,
                          now_ns=now_ns, window_ns=window_ns)
    c = _inspect_container(container_name(user, agent))
    if c is None:
        return "no container (the record is stale -- re-provision it)"
    if not c["running"]:
        return ""
    token = c["env"].get("REVEILLE_TOKEN")
    if not token:
        return "no token to carry (re-provision it instead)"
    spool = _spool_pending(user, agent)
    if spool is None:
        return "could not read its spool"
    act = _broker_activity(c["env"].get("REVEILLE_URL") or DEFAULT_BROKER, agent, token)
    if act is None:
        act = _broker_activity(health_url, agent, token)
    if act is None:
        return "the broker did not answer for it"
    return roll_block(grants=0, spool=spool, unread=act.get("unread") or 0,
                      last_send_ns=act.get("last_send_ns") or 0,
                      now_ns=now_ns, window_ns=window_ns)


def refuse_unless_forced(conn, user, agent, force, *, doing):
    """12959 HALF 1: the DIRECT paths ask the same pure gate the automated
    roll has had since it was written (roll_reason), and refuse BY NAME. The
    asymmetry this closes: the sweep was careful and the human path was
    blunt, which is the wrong way round -- a human is the one who can be
    interrupted mid-sentence. The gate protects LIVE bodies only: a stopped
    or absent container has nobody at its keyboard, and refusing its
    replacement would block the heal path (the 2026-08-20 architect revival
    came through exactly that door). --force is the deliberate override; a
    consented force is not the gate overridden."""
    c = _inspect_container(container_name(user, agent))
    if c is None or not c["running"]:
        return
    r = roll_reason(conn, user, agent)
    if r and not force:
        raise LaunchError(
            f"not {doing} {user}/{agent}: busy -- {r}. A roll of a live body "
            f"is a body swap (12959); pass --force to do it anyway.")


def leave_roll_record(user, agent, *, to_image, why):
    """12959 HALF 2 (as amended at 12954/12956): THE ROLL LEAVES ITS OWN
    RECORD IN THE DATA ROOT, WRITTEN BEFORE THE STOP, by the LAUNCHER while
    the old container still stands. It lands beside the boot report -- the
    surface the next body already reads about its own boots -- and covers
    precisely what the bus-activity gate cannot see: local work with no bus
    traffic. It still works when the credential is dead or the body is
    wedged, because the writer is the launcher, not the body. OBSERVED
    state, numbers not adjectives; a tree that cannot be read is RECORDED as
    unreadable, never skipped -- the refused-credential case is the whole
    point. PROVENANCE: the record says the launcher wrote it. It is an
    observation about a body, never a note in the body's voice; a five-field
    handover note means a live body looked at its own work, and nothing else
    may wear that shape."""
    name = container_name(user, agent)
    c = _inspect_container(name)
    if c is None:
        return                     # no body existed; there was no roll to record

    # READ THE TREE FROM THE HOST (13335): docker_run_argv binds
    # {root}/repos:/home/agent/repos, so the container's work tree IS
    # data_root(user, agent)/repos/work on this host. Plain git here works
    # when the body is stopped, wedged, credential-dead or already gone --
    # the exact states a roll is most likely in, and the states where the
    # exec-through-the-body read came back blank on the first fleet roll.
    # safe.directory is passed deliberately: the tree is owned by the image's
    # agent uid and this process is the host user, and git's ownership
    # refusal must be RECORDED as unreadable, never misread as a clean tree.
    work = os.path.join(data_root(user, agent), "repos", "work")

    def observed(args):
        if not os.path.isdir(work):
            return None
        try:
            r = subprocess.run(
                ["git", "-c", f"safe.directory={work}", "-C", work] + args,
                capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            return None
        if r.returncode != 0:
            if "dubious ownership" in (r.stderr or ""):
                return "unreadable: ownership refused"
            return None
        return (r.stdout or "").strip()

    st = observed(["status", "--porcelain"])
    dirty = (st if st == "unreadable: ownership refused"
             else None if st is None
             else str(len([ln for ln in st.splitlines() if ln.strip()])))
    unpushed = observed(["rev-list", "--count", "@{u}..HEAD"])
    unreadable = "unreadable (no repo, no upstream, or an unreadable tree)"
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    text = "\n".join([
        "# roll record -- written by the LAUNCHER, not by the body",
        "",
        f"- when: {ts}",
        f"- what: {why}",
        f"- image: {c['image'] or 'unknown'} -> {to_image}",
        "- dirty files in /home/agent/repos/work: "
        + (dirty if dirty is not None else unreadable),
        "- unpushed commits: "
        + (unpushed if unpushed is not None else unreadable),
        "",
        "This is an observation about the body, not the body's own note: a",
        "five-field handover note means a live body looked at its own work,",
        "and nothing else may wear that shape.",
    ]) + "\n"
    try:
        # "claude", NOT ".claude" (13343-era field check): docker_run_argv
        # binds {root}/claude to /home/agent/.claude -- the DOT-less host dir
        # is the mount. The first write targeted {root}/.claude, a sibling no
        # container sees, so the pointer (#193) could never find the record
        # and the gates hid it by faking data_root flat. The contract gate
        # beside this reads the mount line from the argv builder itself.
        path = os.path.join(data_root(user, agent), "claude", "roll-record.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
    except OSError as e:
        # a record that cannot be written must not stop the roll -- but it
        # is SAID, never swallowed
        print(f"roll record could not be written for {user}/{agent}: {e}",
              file=sys.stderr)



def roll_idle(conn, image=DEFAULT_IMAGE, *, health_url=DEFAULT_HEALTH, timeout=120,
              window_ns=None, out=print):
    """Roll every BEHIND container that is idle; skip the rest and LIST them.
    A skipped container is retried on the next `make up` -- the deploy never
    kills work in progress to make a version number tidy. Returns
    (rolled, busy)."""
    rolled, busy = [], []
    for r in behind_image(conn, image):
        user, agent = r["user"], r["agent"]
        why = roll_reason(conn, user, agent, health_url=health_url, window_ns=window_ns)
        if why:
            busy.append((user, agent, why))
            out(f"  {user}/{agent}: behind, busy: {why}")
            continue
        try:
            res = upgrade_agent(conn, user, agent, image, health_url=health_url,
                                timeout=timeout)
            rolled.append((user, agent))
            out(f"  {user}/{agent}: rolled {res['from']} -> {res['to']}, "
                f"{res['final']}")
        except (LaunchError, subprocess.CalledProcessError) as e:
            busy.append((user, agent, str(e)))
            out(f"  {user}/{agent}: behind, busy: {e}")
    return rolled, busy


def mint_grant(conn, user, agent, grantee, mode, ttl):
    """Shared grant-mint path. The token is RETURNED once, never stored
    (4.5.2): re-issue is re-mint, never retrieval."""
    _known_agent(conn, user, agent)
    # THE SAME GRANTEE ASKING AGAIN IS NOT A SECOND DRIVER, IT IS THE SAME ONE
    # RECONNECTING, so their previous driver grant is superseded rather than
    # treated as a rival for the keyboard.
    #
    # Without this, attach worked exactly ONCE per agent per TTL. The web page
    # mints a fresh 24h driver grant on every attach and nothing releases the
    # old one -- a closed tab revokes nothing -- so the owner's own hour-old
    # grant refused them, naming a grant id they had no reason to recognise.
    # Found live: three live driver grants, all grantee 'me', all the operator's,
    # locking the operator out of the operator's own agent.
    #
    # Re-issue is re-mint and never retrieval (4.5.2), so reusing the old grant
    # is not available -- its token was returned once and never stored. Supersede
    # is the shape already used for bound tokens and for a second waiter attach.
    # Revoking KILLS the old session first, which is what makes the new tab the
    # driver rather than the two of them fighting.
    if mode == "driver":
        for row in conn.execute(
                "SELECT id FROM grants WHERE user=? AND agent=? AND grantee=? "
                "AND mode='driver' AND revoked_ns IS NULL",
                (user, agent, grantee)).fetchall():
            revoke_grant(conn, user, agent, row["id"], actor="supersede")
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
    # THE FLAG THE REFUSAL PRESCRIBES EXISTS (ruling 12401 D4). provision_refusal
    # has demanded a role prompt since r3 and its text said "pass one with
    # --role-prompt" -- a flag this parser never had, so the CLI path was a doc
    # that was false as written. --role picks a template by name; --role-prompt
    # is the explicit text; both is template + appended text, the same shape the
    # web form sends.
    prompt = (ROLE_PROMPTS.get(a.role, "") + ("\n\n" + a.role_prompt
              if a.role_prompt else "")).strip() or None
    if a.role and a.role not in ROLE_PROMPTS:
        conn.close()
        die(f"unknown role {a.role!r} -- one of {', '.join(sorted(ROLE_PROMPTS))}")
    try:
        if a.replace:
            refuse_unless_forced(conn, a.user, a.agent,
                                 getattr(a, "force", False),
                                 doing="re-provisioning")
        token = read_secret(f"bound broker token for {a.agent}")
        name, kind = provision_agent(conn, a.user, a.agent, a.repo_url, token,
                                     image=a.image, network=a.network,
                                     broker=a.broker, boot_cmd=a.boot_cmd,
                                     replace=a.replace, role_prompt=prompt)
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


def cmd_upgrade(a):
    conn = _db()
    try:
        if a.all and getattr(a, "idle", False):
            # THE DEPLOY'S OWN VERB (DES-006 s7.2): roll what is idle, say what
            # is busy, exit 0 either way -- a busy agent is not a deploy failure.
            print(f"rolling agent containers behind {a.image} (idle rule: "
                  f"{ROLL_IDLE_MIN:g} min)")
            rolled, busy = roll_idle(conn, a.image, health_url=a.health_url,
                                     timeout=a.timeout)
            if not rolled and not busy:
                print(f"  nothing behind {a.image}")
            elif busy:
                print(f"  {len(busy)} left for the next deploy; force one now with "
                      f"`reveille-launch upgrade <user> <agent>`")
            return
        if a.all:
            todo = [(r["user"], r["agent"]) for r in behind_image(conn, a.image)]
            if not todo:
                print(f"nothing behind {a.image}")
                return
        elif a.user and a.agent:
            refuse_unless_forced(conn, a.user, a.agent,
                                 getattr(a, "force", False), doing="upgrading")
            todo = [(a.user, a.agent)]
        else:
            die("usage: reveille-launch upgrade USER AGENT [--image X] | upgrade --all [--image X]")
        failed = 0
        for user, agent in todo:
            try:
                out = upgrade_agent(conn, user, agent, a.image, health_url=a.health_url,
                                    timeout=a.timeout)
                print(f"upgraded {user}/{agent}: {out['from']} -> {out['to']}, "
                      f"{out['final']}")
            except LaunchError as e:
                failed += 1
                print(f"FAILED {user}/{agent}: {e}", file=sys.stderr)
        if failed:
            sys.exit(1)
    finally:
        conn.close()


def cmd_behind(a):
    conn = _db()
    try:
        rows = behind_image(conn, a.image)
    finally:
        conn.close()
    if not rows:
        print(f"agent containers: all on {a.image}")
        return
    print(f"agent containers BEHIND {a.image}:")
    for r in rows:
        print(f"  {r['user']}/{r['agent']}  {r['image']}")
    print("upgrade in place (token carried from the container, data kept):")
    print(f"  reveille-launch upgrade --all --image {a.image}")


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
    if a.state == "off":
        # A TOGGLE NEVER REPORTS A CHANGE IT DID NOT MAKE (13443/13444): the
        # gate is env OR marker, and rm-ing the marker under a hand-set env
        # would print "off" while the gate still says on. We never set the
        # env; if someone did, refuse and name it.
        r = _docker("exec", name, "sh", "-c",
                    '[ "${REVEILLE_MULTI_DRIVER:-0}" = 1 ]',
                    check=False, capture=True)
        if r.returncode == 0:
            die(f"flip off refused: {name} was created with "
                f"REVEILLE_MULTI_DRIVER=1 in its environment -- the gate's "
                f"env door stays open no matter what this toggle removes; "
                f"recreate the container without the variable")
    # Runtime toggle rides a marker file the gate checks per-attach: ttyd's
    # children only ever see create-time env, so env alone cannot flip live.
    shell = ("touch ~/.multi-driver" if a.state == "on"
             else "rm -f ~/.multi-driver")
    res = _docker("exec", name, "sh", "-c", shell, check=False, capture=True)
    if res.returncode != 0:
        die(f"flip failed in {name} -- is it running?")
    _audit("FLIP", user=a.user, agent=a.agent, multi_driver=a.state,
           actor="launcher-cli")
    # A FLIP STATES ITS OWN LIFETIME (13448): the profile is the declaration
    # and this is a live override of the runtime copy -- real until the next
    # re-provision copies the declaration back. One act, one scope; flip
    # never writes the profile (two writers of one boolean is the defect).
    declared = resolve_multi_driver(load_profile(a.user), a.agent)
    scope = ""
    if declared and declared != a.state:
        scope = (f" on this container until it is re-provisioned; "
                 f"the profile still says {declared}")
    print(f"{a.user}/{a.agent}: multi-driver {a.state}{scope}")
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
    newest session activity, the newest spool entry (a ring that arrived but
    has not fired yet). Epoch seconds. An exec that FAILS returns None -- the
    caller skips, because could-not-tell is not an observation. An exec that
    succeeds and sees nothing (no tmux yet) returns (False, 0, 0), and is_idle
    reads that against the container's boot time, never the epoch."""
    res = _docker("exec", container_name(user, agent), "sh", "-c",
                  "tmux list-clients -F x 2>/dev/null | wc -l;"
                  " tmux list-sessions -F '#{session_activity}' 2>/dev/null"
                  " | sort -rn | head -1;"
                  " find ~/.reveille/spool -name '*.ring' -printf '%T@\\n'"
                  " 2>/dev/null | sort -rn | head -1 | cut -d. -f1",
                  check=False, capture=True)
    if res.returncode != 0:
        # COULD-NOT-TELL IS NEVER READ AS IDLE (ruling 12401, doctrine 8866,
        # same discipline _credential_known keeps): a failed exec and "observed
        # nothing" are different facts, and only the second is evidence. None
        # means SKIP -- the sweep must not stop a container it could not probe.
        return None
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


def _container_credential(user, agent):
    """The token the RUNNING container holds, read from its env, or "".

    Read, never stored: launcher.db persists no credential (R1) and this one
    lives exactly as long as the probe below. The launcher already handles this
    secret at provision -- it pipes it into `docker run` -- so asking the
    container what it is holding adds no new exposure, and it is the only way
    to ask the broker a question ABOUT that body without the launcher holding a
    standing credential of its own.
    """
    res = _docker("inspect", "-f", "{{json .Config.Env}}",
                  container_name(user, agent), check=False, capture=True)
    if res.returncode != 0:
        return ""
    try:
        env = json.loads((res.stdout or "").strip() or "[]")
    except ValueError:
        return ""
    for e in env:
        k, _, v = str(e).partition("=")
        if k == "REVEILLE_TOKEN":
            return v
    return ""


def _credential_known(broker_url, token):
    """Does the broker still know this credential? None when it could not say.

    THE BODY'S OWN SECRET ASKS THE QUESTION, which is what keeps the launcher
    credential-less: it borrows the credential for one read and answers only
    about that body. A 401 is the broker saying this credential no longer
    speaks for anyone -- the supersede deleted it. Any other failure (broker
    down, DNS, timeout) returns None and the caller does NOTHING: an
    unreachable broker must never be read as "every body here is dead".

    The five-minute grace is the BROKER's to keep, not this loop's: a
    just-superseded credential still resolves for its handover writes (R2), so
    it answers 200 here for exactly as long as it is still allowed to write its
    note, and 401 the moment that window closes. The timing ruling 12320 A asks
    for therefore needs no clock on this side.
    """
    if not token or not broker_url:
        return None
    req = urllib.request.Request(
        broker_url.rstrip("/") + "/presence",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except urllib.error.HTTPError as e:
        return False if e.code == 401 else None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None


def _stop_superseded(conn):
    """STOP the containers whose identity has moved on (ruling 12320 A).

    Measured 2026-08-19, twice in one afternoon: a body superseded by an
    arrival kept its CPU, its tmux and its ttyd, and the Agents page went on
    offering a terminal into it. The operator found it both times -- "the
    container for red-shirt-01 is still running" -- because nothing in the
    system was responsible for the corpse. The launcher is: it is the only
    thing that touches docker, and the broker already knows the arrival
    happened. G4 holds -- no docker awareness moves into the broker.

    STOP, never destroy: the home, the image and the record stay, so the same
    body restarts on a send-back or a materialize with its work intact. Destroy
    would drop the data root, and a body may be sent back to this machine (s14).
    """
    stopped = []
    for c in conn.execute("SELECT user, agent, broker_url FROM containers").fetchall():
        user, agent = c["user"], c["agent"]
        st = (_docker("inspect", "-f", "{{.State.Running}}", container_name(user, agent),
                      check=False, capture=True).stdout or "").strip()
        if st != "true":
            continue
        known = _credential_known(c["broker_url"], _container_credential(user, agent))
        if known is not False:      # True = still ours; None = we could not tell
            continue
        _docker("stop", container_name(user, agent), check=False, capture=True)
        _audit("SUPERSEDEDSTOP", user=user, agent=agent,
               reason="the identity moved to another body")
        stopped.append((user, agent))
    return stopped


def _sweep_once(conn, idle_window_ns=0):
    now = time.time_ns()
    # OBSERVE FIRST, WRITE LAST (13366, measured 2026-08-20): this walk shells
    # out to docker two-to-three times per container, and _stop_superseded
    # adds a docker inspect plus a broker HTTP read per running body -- one
    # write-lock hold of 7.54s was measured across the fleet, once per tick,
    # because the transaction used to open at the FIRST container's
    # sessions_seen DELETE and stay open across every LATER container's
    # observations. Every launcher open during a tick waited the remainder
    # (the 2.1-7.5s series behind the _db timeout). SELECTs take no write
    # lock; the writes are a handful of tiny rows and land in one short
    # transaction at the end of the tick.
    record = []
    reconciled = []
    for c in conn.execute("SELECT user, agent, roll_desired_running "
                          "FROM containers").fetchall():
        user, agent = c["user"], c["agent"]
        if c["roll_desired_running"] is not None:
            # AN ORPHANED IN-FLIGHT RECORD (13457 property 3): a roll died
            # between the start and the per-body restore. Desired-stopped
            # but running gets stopped, with its why; the other polarity
            # just clears -- a WIP row must not live forever.
            if c["roll_desired_running"] == 0:
                st = (_docker("inspect", "-f", "{{.State.Running}}",
                              container_name(user, agent),
                              check=False, capture=True).stdout or "").strip()
                if st == "true":
                    _docker("stop", container_name(user, agent),
                            check=False, capture=True)
                    _audit("ROLLRECONCILE", user=user, agent=agent,
                           reason="a roll died between the start and the "
                                  "restore -- stopped as the record says "
                                  "it was found")
            reconciled.append((user, agent))
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
        record.append((user, agent, set(live) - killed))
        # 24h idle STOP, never destroy (sec 7.1): data is on bind mounts, so a
        # restart is one `start` and loses nothing. A stopped container probes
        # as (False, 0, 0) with an exec error and is skipped above the is_idle
        # window by construction -- but skip it explicitly: stopping the
        # stopped is noise.
        if idle_window_ns > 0:
            st = _docker("inspect", "-f",
                         "{{.State.Running}} {{.State.StartedAt}}",
                         container_name(user, agent), check=False, capture=True)
            running, _, started_at = (st.stdout or "").strip().partition(" ")
            if running == "true":
                # None = the exec failed = could-not-tell, and could-not-tell
                # never stops a container (ruling 12401, doctrine 8866).
                probe = _idle_probe(user, agent)
                if probe is not None and is_idle(
                        probe[0], probe[1] * 10**9, probe[2] * 10**9, now,
                        idle_window_ns, rfc3339_ns(started_at)):
                    _docker("stop", container_name(user, agent),
                            check=False, capture=True)
                    _audit("IDLESTOP", user=user, agent=agent,
                           window_s=idle_window_ns // 10**9)
    # THE CORPSE IS THE SWEEP'S JOB TOO (ruling 12320 A). Last of the
    # observations, so a container this tick is about to stop for supersession
    # is not also probed for idle -- and unconditional, because unlike the
    # idle stop this one is not a policy with a window to disable: a body
    # whose credential the broker has deleted is not running for anybody.
    _stop_superseded(conn)
    # WRITE PHASE: everything above observed; only now does the transaction
    # open, and it holds nothing but these rows.
    for user, agent in reconciled:
        conn.execute("UPDATE containers SET roll_desired_running=NULL "
                     "WHERE user=? AND agent=?", (user, agent))
    for user, agent, sessions in record:
        conn.execute("DELETE FROM sessions_seen WHERE user=? AND agent=?",
                     (user, agent))
        conn.executemany(
            "INSERT INTO sessions_seen(user, agent, session, first_seen_ns) "
            "VALUES(?,?,?,?)",
            [(user, agent, s, now) for s in sessions])
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


def _broker_call(auth_url, cookie_header, method, path, body=None):
    """_broker_json that KEEPS THE BROKER'S REFUSAL. Returns (status, parsed
    body): 200 with the payload, or the HTTP status with the broker's error
    document, or (0, None) when nothing answered. Used where the human must be
    TOLD why (architect BLOCKING 1 on PR #7): the tokens route answers a held
    name with 409 name_held and BOTH remedies, and swallowing that into None
    made the create dialog say "broker refused the token mint" -- refused but
    not told, which the operator's rule (10969) forbids. Every other caller
    keeps _broker_json's None-on-failure: fail-closed is right for reads."""
    if not cookie_header:
        return 0, None
    req = urllib.request.Request(
        auth_url.rstrip("/") + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Cookie": cookie_header, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode())
        except (ValueError, UnicodeDecodeError):
            return e.code, None
    except (urllib.error.URLError, TimeoutError):
        return 0, None


def _broker_me(auth_url, cookie_header):
    return principal_from_me(_broker_json(auth_url, cookie_header, "GET", "/me"))


def mint_bound_token(auth_url, cookie_header, agent, rooms, create=False):
    """P3: mint the agent's bound state-tier token THROUGH the broker's
    existing session routes, server-side with the user's own forwarded cookie
    -- the browser never holds the secret, the launcher holds it for this
    call's lifetime only, and the broker learns nothing new (same POST /tokens
    + PATCH room-attach the Tokens tab issues). Raises LaunchError.

    create IS A PARAMETER, NEVER A PROPERTY OF THIS FUNCTION (architect
    BLOCKING 1, msg 10919): baked in here, every future caller would inherit
    deliberate-creation silently -- and the next caller is S3 migration, whose
    whole contract is that it attaches to the EXISTING identity and never
    forks. The create-agent dialog passes True because the form IS the human's
    deliberate act (10896/10905); the edit path keeps the default, where a
    live name makes it inert and a dead one makes the refusal correct -- an
    edit must never create."""
    code, t = _broker_call(auth_url, cookie_header, "POST", "/tokens",
                           {"label": agent, "agent_name": agent,
                            "mem_tier": "state", "create": bool(create)})
    if not isinstance(t, dict) or not t.get("secret"):
        # THE HUMAN IS TOLD, not merely refused: the broker's detail carries
        # both remedies for a held name and the reason for anything else.
        detail = (t or {}).get("detail") or (t or {}).get("error") if isinstance(t, dict) else None
        raise LaunchError(f"broker refused the token mint ({code}): {detail}"
                          if detail else "broker refused the token mint")
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


def login_fingerprint(user, base=None):
    """THE sentinel (ruling 13353): the credential's IDENTITY, never its
    existence -- A SENTINEL THAT PRE-EXISTS THE WORK CANNOT SIGNAL THE WORK,
    and on a RE-login the file pre-exists by definition. Absent is the
    fingerprint "nothing", so a first login falls out of the same rule
    instead of being a special case. Stamped at login_start, compared at
    login_status; the boot script makes the same comparison in-container
    against $FP0. Pure aside from the stat."""
    path = os.path.join(user_auth_root(user, base), ".credentials.json")
    try:
        return str(os.stat(path).st_mtime_ns)
    except OSError:
        return "nothing"


def login_reap_due(fp0, current):
    """Completion is FINGERPRINT MOVED, never FILE PRESENT (13353): the old
    reap keyed on `present and container` and would kill a live RE-login on
    the page's first poll -- the previous account's credential satisfies
    present. fp0 is the stamp read off the container's label, None when no
    stamped flow exists to judge. Pure."""
    return fp0 is not None and current != fp0


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


def repo_status(user, agent):
    """What the container's boot said about its repo: "ok", "none", or a
    "failed: ..." line. "" when nothing was written -- an older image, or a
    container that has not booted since this shipped.

    R2 (ruling 11938): HEALTH THAT IGNORES THE REPO IS THE HOLE. An agent whose
    clone never happened looked exactly like one that never wanted a repo, and
    red-shirt came up with a repo URL, no repo, and every control green."""
    # UNDER claude/, BECAUSE THAT IS WHAT IS MOUNTED. The entrypoint used to
    # write this to /home/agent/ -- container-local since the home mount became
    # two subdir mounts -- so the launcher read a host path nothing wrote and
    # repo failures could never show state=degraded (found 2026-08-19 while
    # wiring the degraded boot through the same file).
    try:
        with open(os.path.join(data_root(user, agent), "claude",
                               ".reveille-repo-status")) as f:
            return f.read().strip()
    except OSError:
        return ""


def lifecycle_state(docker_status, has_files, hive, repo=""):
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
        # DEGRADED IS RUNNING WITH SOMETHING MISSING (r2), not a fourth kind of
        # stopped: the container is up and the agent is reachable, but the work
        # tree it was provisioned with never arrived. Said here so the pane can
        # say it, rather than leaving it in a boot report nobody opens.
        return "degraded" if str(repo).startswith("failed") else "running"
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


# ---- the boot report, made reachable ------------------------------------
# The entrypoint writes what each boot ATTEMPTED, SUCCEEDED at and is MISSING
# to a file in the agent's own home, because the agent has no docker socket and
# could not otherwise read its own broken boot. That closed the gap for the
# AGENT. It did not close it for the HUMAN, who has no shell in the container
# and whose first question about a silent agent is why it is silent.
# Under ~/.claude, which is the bind mount, so this path outlives the container
# (ruling 8732). docker cp reads it either way; what the mount buys is that a
# RETIRED agent -- container gone, home kept -- can still be asked why its last
# boot failed.
BOOT_REPORT_PATH = "/home/agent/.claude/boot-report.md"
# The two markers the entrypoint writes. Kept as data rather than inlined: the
# report's own header promises these words to its reader, so they are a shared
# format between two files and not a private detail of either.
BOOT_PROBLEM_MARKERS = ("**MISSING**", "**FAILED**")


def boot_report_problems(text):
    """The lines of a boot report that say something is wrong. PURE.

    Returns them verbatim, stripped of the leading list dash -- never a count
    and never a boolean. A row saying "2 problems" sends the reader looking for
    them; a row saying "role prompt: MISSING" has already answered the
    question, and that is the difference between surfacing a fact and
    advertising that one exists.

    Marker-based rather than clever: the entrypoint's own header promises the
    reader that MISSING or FAILED is how a problem announces itself, so this
    reads the format the report documents. A problem the entrypoint states in
    some other vocabulary will be missed here, which is the right failure --
    the fix is to write the marker, not to teach this to guess."""
    out = []
    for line in (text or "").splitlines():
        if any(m in line for m in BOOT_PROBLEM_MARKERS):
            out.append(line.strip().lstrip("-").strip())
    return out


def read_boot_report(user, agent, limit=40000):
    """The agent's boot report, copied out of its container.

    docker cp rather than exec, deliberately: exec needs a RUNNING container,
    and "why did this agent never come up" is asked precisely about one that is
    not running. cp reads a stopped container's filesystem, so the report is
    reachable in the state where it matters most.

    Returns None when there is no container or no report -- an agent that was
    never provisioned and one whose boot predates the report are both "nothing
    to show", and neither is an error."""
    if not _exists(container_name(user, agent)):
        return None
    tmpdir = tempfile.mkdtemp(prefix="reveille-boot-")
    try:
        dest = os.path.join(tmpdir, "boot-report.md")
        res = _docker("cp", f"{container_name(user, agent)}:{BOOT_REPORT_PATH}",
                      dest, check=False, capture=True)
        if res.returncode != 0 or not os.path.isfile(dest):
            return None
        with open(dest, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


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
        repo = repo_status(user, agent)
        out.append({"agent": agent, "container": r["container"], "status": st,
                    "image": r["image"], "repo_url": r["repo_url"],
                    "role_name": r["role_name"],
                    "created_ns": r["created_ns"],
                    "state": lifecycle_state(st, has_files, hive, repo=repo),
                    "repo_status": repo,
                    "has_files": has_files, "hive": hive})
    for agent, hive in sorted(hive_by_name.items()):
        if agent in listed or not ROLE_RE.match(agent or ""):
            continue
        # ALIVE SOMEWHERE ELSE IS A STATE, NOT AN OMISSION (operator 11883).
        # This used to skip a present agent outright, because "offering to
        # recreate it would invite someone to duplicate a live identity" -- true
        # when a mint could fork. DES-011 s2.1 settled that: a bare attach
        # SUPERSEDES the previous body's credential in the same transaction, so
        # the offer is a MOVE and duplication is not one of the outcomes. The
        # row says `elsewhere` and the page offers exactly one verb on it.
        # ONLY YOUR OWN (architect BLOCKING on #124). These rooms are shared,
        # so the hive's present names include OTHER humans' agents. Moving one
        # of those is not a swap a person may perform alone: it is a VISIT, and
        # DES-012 s3 requires both humans to consent, per visit. An unowned or
        # ambiguous name is not offered either -- the broker answers "" when two
        # owners wear one name, and a guess there would move the wrong being.
        # MOVING: a credential is minted for this identity and has not arrived.
        # Distinct from `elsewhere` (a body working somewhere else) and from
        # `no-live-body` (nothing can act as it at all) -- three situations that
        # all used to render as one, so the pane could not tell a swap in
        # flight from an agent that was simply away.
        if hive.get("moving"):
            out.append({"agent": agent, "container": container_name(user, agent),
                        "status": "absent", "image": "", "repo_url": "",
                        "created_ns": hive.get("last_ns") or 0,
                        "state": "moving",
                        "has_files": os.path.isdir(data_root(user, agent)),
                        "hive": hive})
            continue
        # NO LIVE BODY: the identity exists, the hive remembers it, and no
        # credential can act as it. This is the state reveille-red-shirt sat in
        # while every control read normal, and the state the two-phase swap
        # exists to make unreachable -- so if it ever appears again, it must be
        # visible rather than inferred from an agent that answers nothing.
        if hive.get("bodyless") and hive.get("owner") == user:
            out.append({"agent": agent, "container": container_name(user, agent),
                        "status": "absent", "image": "", "repo_url": "",
                        "created_ns": hive.get("last_ns") or 0,
                        "state": "no-live-body",
                        "has_files": os.path.isdir(data_root(user, agent)),
                        "hive": hive})
            continue
        if hive.get("present") and hive.get("owner") == user:
            out.append({"agent": agent, "container": container_name(user, agent),
                        "status": "absent", "image": "", "repo_url": "",
                        "created_ns": hive.get("last_ns") or 0,
                        "state": "elsewhere",
                        "has_files": os.path.isdir(data_root(user, agent)),
                        "hive": hive})
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


def fetch_with_boot_grace(url, *, opener=urllib.request.urlopen,
                          clock=time.monotonic, snooze=time.sleep):
    """One GET with a BOUNDED grace for the attach boot race (13407, ruled
    13408, re-ruled 13433 on the OPERATOR'S numbers): a
    ConnectionRefusedError from a RUNNING container means ttyd has not bound
    its port yet -- press-play-then-connect loses that race, and the
    operator's close-tab-and-reopen was the retry this loop now performs.

    THE LADDER: one early probe 0.25s after the first refusal (the common
    bind is ~hundreds of ms and must stay fast), then 1.5s steps to a 15.0s
    deadline. 15 came from the operator who watched the field case; the
    original 3 was a number that felt like a boot and had nothing behind it.
    15 is MODELLED, same discipline as the launcher.db 30 (13348): the wait
    lines below are the observations that let it retire to a measured number
    -- or be found WRONG, at which point the COLD START is the problem, not
    the wait. Printed values are OBSERVED, never a success word.

    ANY other failure -- timeout, resolution, HTTP error -- propagates on
    the FIRST throw: retrying those turns a fault into a hang. The
    injectable opener/clock/snooze exist so the give-up half is testable
    without wall time."""
    t0 = clock()
    deadline = t0 + 15.0
    dials = 0
    while True:
        req = urllib.request.Request(url, method="GET")
        dials += 1
        try:
            with opener(req, timeout=10) as r:
                if dials > 1:
                    print(f"attach boot grace: ttyd answered after "
                          f"{clock() - t0:.1f}s ({dials} dials)",
                          file=sys.stderr)
                return r.status, r.read(), r.headers.get("Content-Type", "")
        except urllib.error.URLError as e:
            if not isinstance(e.reason, ConnectionRefusedError):
                raise
            if clock() >= deadline:
                print(f"attach boot grace: exhausted after "
                      f"{clock() - t0:.1f}s ({dials} dials, all refused)",
                      file=sys.stderr)
                raise
            snooze(0.25 if dials == 1 else 1.5)


def build_api(auth_url):
    """The starlette app. Deferred imports: the CLI paths must keep working on a
    box that only ever uses the launcher as a CLI."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route, WebSocketRoute

    # STAMPED ONCE, HERE, because this is the moment the running code was loaded.
    # Reading the tree per-request answers "what is pinned", not "what is
    # running", and those differ for exactly as long as it takes to restart --
    # which is the window the pin-check exists to refuse. Deploying 0.2.46 the
    # check passed the instant `pin` moved the tree, with the old process still
    # serving; a frozen stamp is what makes it a claim about the process.
    running_stamp = source_stamp(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
            # default_image rides the list so a row can say whether it is
            # BEHIND (reconfig 3). Records pin the image at provision, so an
            # agent never follows a bump -- and the gap is invisible from
            # inside the container, which is how one ran for days with no Stop
            # hook while every health signal it could see stayed green.
            return JSONResponse({"agents": _agent_status(conn, p["user"], hive),
                                 "default_image": DEFAULT_IMAGE})
        d = await request.json()
        # P3: no token in the body + rooms ticked -> the LAUNCHER mints the
        # bound state-tier token through the broker's session routes with THIS
        # request's forwarded cookie. The secret exists in this frame and the
        # docker-run child env only -- the browser never sees it at all. A
        # token in the body (the P1/CLI path) is still honored.
        role = (d.get("role") or "").strip()
        if role and role not in ROLE_PROMPTS:
            raise LaunchError(f"unknown role {role!r}")
        prompt = ROLE_PROMPTS.get(role, "")
        if d.get("append"):
            prompt = (prompt + "\n\n" + str(d["append"])).strip()
        # EVERY REFUSAL IS ANSWERED BEFORE ANYTHING IS MINTED (11911). A mint
        # supersedes the identity's previous credential the instant it lands, so
        # a mint followed by a refusal leaves that identity with NO live body --
        # measured, on reveille-red-shirt: minted, refused on the missing role
        # prompt, revoked, and the agent vanished from presence with nothing
        # said. The checks are the same ones provision_agent runs; asking them
        # here first costs one function call and cannot strand anybody.
        why = provision_refusal(conn, p["user"], (d.get("agent") or "").strip(),
                                (d.get("repo_url") or "").strip(),
                                boot_cmd=d.get("boot_cmd"), role_prompt=prompt,
                                replace=bool(d.get("replace")))
        if why:
            raise LaunchError(why)
        token, minted_id = d.get("token") or "", None
        if not token and d.get("rooms"):
            # CREATE IS THE CALLER'S WORD, NEVER THIS ROUTE'S (10919). The
            # create-agent form says create=true because the form IS the human's
            # deliberate act; the MOVE-IT-HERE path says nothing, so the mint is
            # a bare attach on the identity that already exists -- which is the
            # body swap (DES-011 s2.1), and the previous body's credential is
            # superseded in the broker's own transaction. Hardcoding True here
            # made the second case impossible from the web, which is how a body
            # swap came to need an ssh session (operator 11883).
            minted_id, token = mint_bound_token(
                auth_url, request.headers.get("cookie"),
                (d.get("agent") or "").strip(), list(d["rooms"]),
                create=bool(d.get("create")))
        try:
            if d.get("replace"):
                refuse_unless_forced(conn, p["user"],
                                     (d.get("agent") or "").strip(),
                                     bool(d.get("force")),
                                     doing="re-provisioning")
            name, kind = provision_agent(
                conn, p["user"], (d.get("agent") or "").strip(),
                (d.get("repo_url") or "").strip(), token,
                image=d.get("image") or DEFAULT_IMAGE,
                network=d.get("network") or DEFAULT_NETWORK,
                broker=d.get("broker") or DEFAULT_BROKER,
                boot_cmd=d.get("boot_cmd"), replace=bool(d.get("replace")),
                role_prompt=prompt or None,
                model=(d.get("model") or "").strip() or None)
        except (LaunchError, subprocess.CalledProcessError) as e:
            if minted_id:
                # A failed provision must not leave a live orphaned credential --
                # and must not leave the human guessing either (11911). Revoking
                # is right; revoking SILENTLY is how an agent disappeared from
                # presence with no explanation. Every precondition ran before the
                # mint, so reaching here means docker itself failed: say what
                # state that leaves the identity in, and what fixes it.
                revoke_minted_token(auth_url, request.headers.get("cookie"),
                                    minted_id)
                raise LaunchError(
                    f"{e} -- the new credential was revoked. "
                    f"{(d.get('agent') or '').strip()} KEPT the body it had: a "
                    f"mint no longer supersedes anything until the new body "
                    f"joins (two-phase swap), and this one never got that far. "
                    f"Nothing was taken from the working machine. "
                    f"Retry the move once the cause above is fixed.")
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
        if verb == "upgrade":
            # Ruling 11600: the OWNER's session (guarded; _known_agent inside)
            # upgrades to the launcher's default image, token carried from the
            # container -- the answer names images only, never the credential.
            # OFF THE LOOP (review 11609): docker stop + wait_healthy is up to
            # two minutes, and provision deliberately never blocks the loop that
            # serves every user's /agents poll -- so this runs in a thread with
            # its OWN connection (sqlite is per-thread); the answer still waits.
            force = request.query_params.get("force") in ("1", "true")

            def _upgrade_owned(user, agent):
                c = _db()
                try:
                    refuse_unless_forced(c, user, agent, force,
                                         doing="upgrading")
                    return upgrade_agent(c, user, agent, DEFAULT_IMAGE)
                finally:
                    c.close()
            out = await asyncio.to_thread(_upgrade_owned, p["user"], name)
            return JSONResponse({"upgraded": name, "from": out["from"], "to": out["to"]})
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
        """A login is pending while its tmux SESSION is alive -- not while a
        container with that name exists. The two came apart on the success
        path: the container outlived the flow, `docker inspect` kept saying
        "running", and start refused a re-login on a login that was over. A
        container whose session is gone is residue, and residue must never be
        the thing that strands a user; the cancel button that was their only
        way out is rendered only while the UI believes a login is pending."""
        return _docker("exec", login_container_name(user), "tmux",
                       "has-session", "-t", "login",
                       check=False, capture=True).returncode == 0

    def _login_container(user):
        """Any container by that name, running or exited -- what has to be
        REMOVED, which is a wider question than what is pending."""
        return _docker("inspect", "-f", "{{.State.Status}}",
                       login_container_name(user), check=False,
                       capture=True).returncode == 0

    def _login_fp0(user):
        """The baseline stamped at login_start, read off the container
        itself -- the flow carries its own yardstick, so status needs no
        table. None when no container exists: nothing to judge."""
        r = _docker("inspect", "-f",
                    '{{index .Config.Labels "' + _LOGIN_FP_LABEL + '"}}',
                    login_container_name(user), check=False, capture=True)
        return r.stdout.strip() if r.returncode == 0 else None

    @guarded
    async def login_start(request, p, conn):
        if _login_pending(p["user"]):
            raise LaunchError(
                f"a login is already pending for {p['user']} -- finish or "
                f"cancel it first (one login at a time; two flows would race "
                f"on one credential file)")
        _docker("rm", "-f", login_container_name(p["user"]), check=False,
                capture=True)   # a finished or stopped login holds the name
        ensure_login_home(p["user"], DEFAULT_IMAGE)
        # the stamp is taken BEFORE the flow it judges (13353) -- a baseline
        # taken after the start is the pre-existence coincidence again
        fp0 = login_fingerprint(p["user"])
        env = dict(os.environ, SEED=_SEED_BASE, FP0=fp0)
        subprocess.run(login_bg_argv(p["user"], DEFAULT_IMAGE, DEFAULT_NETWORK,
                                     fp0),
                       env=env, check=True, stdout=subprocess.DEVNULL)
        _audit("LOGIN_START", user=p["user"])
        return JSONResponse({"started": True})

    @guarded
    async def login_status(request, p, conn):
        """The pending login as a READING: the credential file fresh, the pane
        fresh. Completion is the FINGERPRINT MOVING (13353) -- the file
        appearing was the old sentinel, and it pre-exists a RE-login, so this
        reap used to kill a live re-login container on the page's first poll.
        When the fingerprint has moved the container is done being useful and
        is removed here, on observation. The removal covers an EXITED
        container too: the container ends itself on the new credential, so
        reaping only what still runs would leave the finished one holding the
        name against the next login. A flow that never mints is NEVER
        reported complete; its residue is cancel's to remove."""
        out = dict(claude_login_state(p["user"]))
        if login_reap_due(_login_fp0(p["user"]), login_fingerprint(p["user"])):
            _docker("rm", "-f", login_container_name(p["user"]), check=False,
                    capture=True)
            _audit("LOGIN_DONE", user=p["user"])
        if _login_pending(p["user"]):
            pane = _docker("exec", login_container_name(p["user"]), "tmux",
                           "capture-pane", "-t", "login", "-p",
                           check=False, capture=True).stdout
            st = parse_login_pane(pane or "")
            out["pending"] = st["stage"]
            out["url"] = st["url"]
            out["relayed"] = _docker(
                "exec", login_container_name(p["user"]), "test", "-f",
                "/tmp/.code-relayed", check=False, capture=True).returncode == 0
        # Read AFTER the reap above, so it reports what is still there rather than
        # what was there when the call began. This is what the cancel button renders
        # on: a RECOVERY CONTROL MUST NOT BE GATED ON THE STATE IT RECOVERS FROM
        # (architect, msg 8867). `pending` is a reading of a tmux session and can be
        # wrong in either direction; a container either exists or it does not, and
        # that is the thing cancel actually removes.
        out["container"] = _login_container(p["user"])
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
    async def agent_boot_report(request, p, conn):
        """The agent's own account of its last boot, and what went wrong in it.

        Read on demand rather than cached: it is the container's file, it is
        rewritten every boot, and a cached copy would be a second thing that
        can lapse -- in a UI whose whole job this week has been surfacing
        things that lapsed quietly."""
        name = request.path_params["agent"]
        _known_agent(conn, p["user"], name)
        text = read_boot_report(p["user"], name)
        return JSONResponse({"agent": name, "report": text,
                             "problems": boot_report_problems(text)})

    @guarded
    async def agent_read(request, p, conn):
        """S2 (ruled 11961/11965): the launcher's READ verbs -- logs, version,
        inspect. Owner-scoped, host-scoped, and READ ONLY.

        THE HARD LINE, ruled explicitly and not softened here: logs / version /
        inspect ONLY. NO exec, NO run, NO compose file, ever. The launcher holds
        the docker socket, so a verb that could run something in a container is
        a verb that hands an HTTP caller the host -- and every argument for
        "just this once" is an argument for the socket-in-the-container design
        that r1 refused on the same day.

        A BODY ON ANOTHER HOST GETS AN ANSWER, NOT AN EMPTY ONE (ruled 12066).
        This launcher knows only its own docker. Returning "no logs" for an
        agent that is alive elsewhere would be the unreachable-control defect
        the whole week has been closing: a control that says nothing, and reads
        as "nothing to say".
        """
        name = request.path_params["agent"]
        verb = request.path_params["verb"]
        _known_agent(conn, p["user"], name)
        row = conn.execute("SELECT * FROM containers WHERE user=? AND agent=?",
                           (p["user"], name)).fetchone()
        if not row:
            return JSONResponse(
                {"agent": name, "verb": verb, "here": False,
                 "detail": f"{name} has no container on this host. If it is alive, "
                           f"it is alive somewhere else -- this launcher can only "
                           f"read its own docker."}, status_code=409)
        cname = row["container"]
        if verb == "logs":
            n = min(int(request.query_params.get("lines") or 200), 2000)
            out = _docker("logs", "--tail", str(n), cname, check=False, capture=True)
            return JSONResponse({"agent": name, "verb": verb, "here": True,
                                 "lines": (out.stdout or "") + (out.stderr or "")})
        if verb == "version":
            # ONE VERSION HERE, DELIBERATELY, AND THE OTHER IS NOT THIS ROUTE'S
            # TO FETCH. Since waked converges the toolchain to the broker, a
            # container can legitimately run newer reveille than the image it
            # was built from, and reporting only the image pin now understates
            # what is running. The obvious fix -- ask the container --  is
            # `docker exec`, and THE READ ROUTE MAY NOT EXEC (ruling 11965: this
            # process holds the docker socket, so a verb that runs something in
            # a container hands an HTTP caller the host). A drift this route
            # cannot see is not a reason to hand out that capability; the
            # toolchain version belongs on the agent's own report to the broker,
            # where it costs nobody the socket. Left for that.
            return JSONResponse({"agent": name, "verb": verb, "here": True,
                                 "image": row["image"], "default": DEFAULT_IMAGE,
                                 "behind": row["image"] != DEFAULT_IMAGE})
        if verb == "inspect":
            out = _docker("inspect", cname, check=False, capture=True)
            try:
                d = (json.loads(out.stdout or "[]") or [{}])[0]
            except ValueError:
                d = {}
            st = d.get("State") or {}
            # A SHAPE, not the raw blob: docker's inspect carries the whole
            # environment, and the environment is where credentials live.
            return JSONResponse({"agent": name, "verb": verb, "here": True,
                                 "status": st.get("Status") or "unknown",
                                 "started_at": st.get("StartedAt") or "",
                                 "restarts": d.get("RestartCount") or 0,
                                 "image": row["image"],
                                 "health": (st.get("Health") or {}).get("Status") or ""})
        return JSONResponse({"error": "unknown read verb",
                             "detail": f"{verb!r} is not a read verb. This launcher "
                                       f"reads logs, version and inspect, and runs "
                                       f"nothing on request."}, status_code=400)

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
        try:
            status, body, ctype = await asyncio.to_thread(
                fetch_with_boot_grace, url)
        except urllib.error.HTTPError as e:
            return Response(e.read(), status_code=e.code)
        except OSError as e:
            if isinstance(getattr(e, "reason", None), ConnectionRefusedError):
                # The grace ran out and ttyd is STILL not bound: name the
                # state (13408) -- not-up-yet is a wait, not a fault, and
                # "unreachable" reads as broken-or-gone.
                started = (_docker(
                    "inspect", "-f", "{{.State.StartedAt}}",
                    container_name(_p["user"], agent),
                    check=False, capture=True).stdout or "").strip()
                return PlainTextResponse(
                    f"terminal not listening yet -- container running since "
                    f"{started or '(unknown)'}; try again",
                    status_code=502)
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
        """ANSWERS FOR WHAT IT IS RUNNING, not merely that it is up.

        The launcher is the fleet's SECOND deploy unit and the only unverified
        one: `make up` builds the broker image, probes it by name and prints its
        version, so a merged-but-undeployed broker announces itself. The launcher
        ships from a pinned clone that nothing restarts on merge, so "merged" and
        "running" could differ indefinitely with nothing saying so -- and did.
        A login-home crash was fixed, reviewed and merged while the process kept
        running the commit from before it, for six reviews (msg 8681).

        A banner at boot could not close that: it is read once, by whoever
        happened to restart. An endpoint can be ASKED, which is what makes the
        deploy able to refuse.

        The stamp is the one taken when this app was BUILT, never a fresh read:
        see build_api. A per-request read describes the disk, and the disk is
        what moves first."""
        from starlette.responses import JSONResponse
        commit, branch, version = running_stamp
        return JSONResponse({"ok": True, "version": version, "commit": commit,
                             "branch": branch,
                             "source": os.path.dirname(os.path.dirname(
                                 os.path.abspath(__file__)))})

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
        Route("/agents/{agent}/boot-report", agent_boot_report),
        # READ verbs, before the lifecycle catch-all: that route takes any verb
        # on POST, and a read must never be reachable by a method that also
        # reaches start/stop/destroy.
        Route("/agents/{agent}/read/{verb:str}", agent_read, methods=["GET"]),
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
    # The hook installer moved INTO the package (DES-008 A): its command has to
    # be a name on PATH rather than a path into a clone, or a machine installed
    # without one gets a hook naming a file that was never there. Invoked as a
    # module so this path works from the repo checkout the launcher runs from.
    r = subprocess.run([sys.executable, "-m", "reveille.install"],
                       capture_output=True, text=True, cwd=repo,
                       env=dict(os.environ, PYTHONPATH=os.path.join(repo, "src")))
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
    n.add_argument("--force", action="store_true",
                   help="replace even a BUSY live body (12959: the direct path "
                        "asks roll_reason and refuses by name without this)")
    n.add_argument("--image", default=DEFAULT_IMAGE)
    n.add_argument("--timeout", type=int, default=90)
    n.add_argument("--no-wait", action="store_true",
                   help="return after docker run without the presence health wait")
    n.add_argument("--boot-cmd", default=None,
                   help="override the image command (default: claude reveille); "
                        "e.g. agent-probe to health-check without an Anthropic login")
    n.add_argument("--role", default="",
                   help="role template for the new body's CLAUDE.md "
                        "(architect, senior-dev, ...) -- same list the web form offers")
    n.add_argument("--role-prompt", default="",
                   help="explicit role prompt text; with --role it is appended "
                        "to the template")
    n.set_defaults(fn=cmd_new)

    sub.add_parser("ls", help="list provisioned containers").set_defaults(fn=cmd_ls)

    up = sub.add_parser("upgrade", help="re-provision an agent on a new image, carrying "
                        "its bound token from the container it has (ruling 11600); "
                        "the old container comes back if the new one is not healthy")
    up.add_argument("user", nargs="?")
    up.add_argument("agent", nargs="?")
    up.add_argument("--all", action="store_true", help="every record behind --image")
    up.add_argument("--idle", action="store_true",
                    help="with --all: roll only the ones that are IDLE (no live attach "
                         "grant, empty spool, nothing unread, no bus send in "
                         "REVEILLE_ROLL_IDLE_MIN minutes); the busy ones are LISTED and "
                         "retried on the next deploy, never killed mid-task")
    up.add_argument("--image", default=DEFAULT_IMAGE)
    up.add_argument("--force", action="store_true",
                   help="roll even a BUSY live body (12959: the direct path "
                        "asks roll_reason and refuses by name without this)")
    up.add_argument("--health-url", default=DEFAULT_HEALTH)
    up.add_argument("--timeout", type=int, default=120)
    up.set_defaults(fn=cmd_upgrade)
    be = sub.add_parser("behind", help="list provisioned containers whose recorded image "
                        "is not --image, with the upgrade command")
    be.add_argument("--image", default=DEFAULT_IMAGE)
    be.set_defaults(fn=cmd_behind)

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
