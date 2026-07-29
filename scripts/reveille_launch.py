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
  reveille-launch grant <user> <agent> <grantee> [--mode viewer|driver] [--ttl 86400]
      Mint a per-grant URL token (docker exec attach-gate mint -- the secret never
      leaves the container) and record the grant. The token is PRINTED ONCE, never
      stored (4.5.2): re-issue is re-mint, never retrieval.
  reveille-launch grants [user] [agent]              list grant records
  reveille-launch revoke <user> <agent> <grant-id>   kill d-/v-<id> now; audit exact
  reveille-launch flip <user> <agent> on|off         multi-driver toggle (4.3)
  reveille-launch sweep [--loop SECONDS] [--idle-hours 24]
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
"""
import argparse
import contextlib
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
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
DEFAULT_IMAGE = os.environ.get("REVEILLE_AGENT_IMAGE", "reveille-agent:0.2.2")
# Per-agent persistent state (DES-005 sec 4, a7d389b): data/<user>/<agent>/{claude,repos}.
# The user segment exists for ownership and deletion ONLY -- two agents of one
# user share NOTHING on disk.
DEFAULT_DATA = os.environ.get(
    "REVEILLE_LAUNCH_DATA", os.path.expanduser("~/.reveille/data"))
ROLE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{1,63}\Z")

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


def docker_run_argv(user, agent, image, network, quotas, forward_anthropic,
                    boot_cmd=None, data_base=None):
    """The docker-run command as argv. `-e NAME` entries pass values BY NAME from the
    child's env, so no secret is ever a token in this list -- the test asserts exactly
    that. quotas is a resolved QUOTA_DEFAULTS-shaped dict. `--restart no` is explicit
    although it is docker's default: reboot and crash both leave the container down,
    so 'running' always means somebody meant it (sec 7.1). Pure: no env read, no
    side effects."""
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
    for name in ENV_PASSTHROUGH_SECRET:
        argv += ["-e", name]
    if forward_anthropic:
        # Headless claude needs its Anthropic credential; the claude home carries
        # a login (R2), but a fresh home has none, so forward the operator's key by
        # name if they have one. Not a broker secret, not persisted.
        argv += ["-e", "ANTHROPIC_API_KEY"]
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
                    boot_cmd=None, replace=False):
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

    env = dict(
        os.environ,
        REVEILLE_AGENT_ROLE=agent,
        REVEILLE_URL=broker,
        REVEILLE_REPO_URL=repo_url,
        REVEILLE_TOKEN=token,
        # Per-container gate secret (T1 4.3), minted HERE at provision, injected by
        # name, never stored -- dies with the container, re-provision mints a new one.
        REVEILLE_GATE_SECRET=secrets.token_hex(32),
    )

    # The agent's home, nothing else's (sec 4). The USER root is 0700 so no other
    # host user browses it; the agent dirs under it are plain mkdirs.
    root = data_root(user, agent)
    user_root = os.path.dirname(root)
    os.makedirs(user_root, mode=0o700, exist_ok=True)
    os.chmod(user_root, 0o700)
    for sub in ("claude", "repos"):
        os.makedirs(os.path.join(root, sub), exist_ok=True)

    _ensure_network(network, broker)
    if replace:
        _docker("rm", "-f", name, check=False, capture=True)
    argv = docker_run_argv(user, agent, image, network, quotas,
                           forward_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
                           boot_cmd=boot_cmd)
    subprocess.run(argv, env=env, check=True, stdout=subprocess.DEVNULL)
    _record(conn, user, agent, repo_url, image, broker)
    return name


def destroy_agent(conn, user, agent, purge=False):
    """Shared destroy path. Grants die with the container (4.5) -- the gate
    secret they were signed against is gone, so the records are history, not
    authority. The data root survives unless purge."""
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
    ip = _docker("inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 name, check=False, capture=True)
    host = (ip.stdout or "").strip() or name
    return {"id": gid, "mode": mode, "grantee": grantee,
            "expiry_ns": now + ttl * 10**9,
            "attach_url": f"http://{host}:7681/?arg={token}"}


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
        name = provision_agent(conn, a.user, a.agent, a.repo_url, token,
                               image=a.image, network=a.network,
                               broker=a.broker, boot_cmd=a.boot_cmd,
                               replace=a.replace)
    except LaunchError as e:
        conn.close()
        die(str(e))
    conn.close()
    if a.no_wait:
        print(f"provisioned {name} on network {a.network} (health wait skipped)")
        return 0
    print(f"provisioned {name} on network {a.network}; waiting for "
          f"presence live+connected (timeout {a.timeout}s)...")
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
    print(f"attach: {out['attach_url']}")
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
    conn = _db()
    window_ns = int(a.idle_hours * 3600 * 10**9)
    while True:
        _sweep_once(conn, idle_window_ns=window_ns)
        if not a.loop:
            break
        time.sleep(a.loop)
    conn.close()
    return 0


# ---- HTTP API (DES-005 P1): the browser's path to the launcher --------------------
# The broker stays docker-free (G4); the LAUNCHER grows the web surface. AuthN is
# the broker's: every request's session cookie is forwarded to broker /me and the
# principal's own name becomes the ONLY user the request can touch -- cross-user
# isolation by construction, no user parameter exists on the wire.

def principal_from_me(body):
    """Resolve broker /me JSON to a principal name, or None. Pure. A first-run
    broker ({'setup': true}) has no users and therefore no principals."""
    if not isinstance(body, dict) or body.get("setup") or not body.get("name"):
        return None
    return {"user": body["name"], "is_admin": bool(body.get("is_admin"))}


def _broker_me(auth_url, cookie_header):
    if not cookie_header:
        return None
    req = urllib.request.Request(auth_url.rstrip("/") + "/me",
                                 headers={"Cookie": cookie_header})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return principal_from_me(json.load(r))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return None


def _agent_status(conn, user):
    rows = conn.execute(
        "SELECT * FROM containers WHERE user=? ORDER BY agent", (user,)).fetchall()
    out = []
    for r in rows:
        st = _docker("inspect", "-f", "{{.State.Status}}", r["container"],
                     check=False, capture=True)
        out.append({"agent": r["agent"], "container": r["container"],
                    "status": (st.stdout or "").strip() or "absent",
                    "image": r["image"], "repo_url": r["repo_url"],
                    "created_ns": r["created_ns"]})
    return out


def build_api(auth_url):
    """The starlette app. Deferred imports: the CLI paths must keep working on a
    box that only ever uses the launcher as a CLI."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

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
            return JSONResponse({"agents": _agent_status(conn, p["user"])})
        d = await request.json()
        # The bound broker token rides the body and this frame only -- the
        # response echoes NOTHING back and nothing stores it (P2 owns
        # credential profiles; until then the browser supplies it per provision).
        name = provision_agent(
            conn, p["user"], (d.get("agent") or "").strip(),
            (d.get("repo_url") or "").strip(), d.get("token") or "",
            image=d.get("image") or DEFAULT_IMAGE,
            network=d.get("network") or DEFAULT_NETWORK,
            broker=d.get("broker") or DEFAULT_BROKER,
            boot_cmd=d.get("boot_cmd"), replace=bool(d.get("replace")))
        return JSONResponse({"container": name, "agent": d.get("agent")})

    @guarded
    async def agent(request, p, conn):
        name = request.path_params["agent"]
        if request.method == "DELETE":
            _known_agent(conn, p["user"], name)
            destroy_agent(conn, p["user"], name,
                          purge=request.query_params.get("purge") == "1")
            return JSONResponse({"destroyed": name})
        _known_agent(conn, p["user"], name)
        rows = _agent_status(conn, p["user"])
        return JSONResponse(next(r for r in rows if r["agent"] == name))

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

    @guarded
    async def profile(request, p, conn):
        q = _quotas_for(conn, p["user"])
        used = conn.execute("SELECT count(*) FROM containers WHERE user=?",
                            (p["user"],)).fetchone()[0]
        return JSONResponse({"user": p["user"], "quotas": q,
                             "containers": used,
                             "disk_note": "disk_gb recorded, not yet enforced"})

    async def health(_request):
        from starlette.responses import PlainTextResponse
        return PlainTextResponse("ok")

    # grants routes BEFORE the {verb} catch-all: starlette matches in order, and
    # POST /agents/x/grants must be a mint, never an "unknown verb 'grants'".
    return Starlette(routes=[
        Route("/health", health),
        Route("/agents", agents, methods=["GET", "POST"]),
        Route("/agents/{agent}", agent, methods=["GET", "DELETE"]),
        Route("/agents/{agent}/grants", agent_grants, methods=["GET", "POST"]),
        Route("/agents/{agent}/grants/{gid}", agent_grant, methods=["DELETE"]),
        Route("/agents/{agent}/{verb:str}", agent_lifecycle, methods=["POST"]),
        Route("/profile", profile),
    ])


def cmd_serve(a):
    import uvicorn
    app = build_api(a.auth_url)
    print(f"reveille-launch api on {a.host}:{a.port} (auth: {a.auth_url}/me)")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
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

    s = sub.add_parser("sweep", help="expiry/revoke sweep + audit harvest (4.6) "
                                     "+ idle stop (DES-005 7.1)")
    s.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="repeat every N seconds (default: one tick and exit)")
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
    sv.set_defaults(fn=cmd_serve)

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
