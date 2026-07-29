#!/usr/bin/env python3
"""reveille-launch (DES-002 T2): the ONLY thing that touches docker.

The broker never gains docker awareness (G4); this is a separate host-side process
that owns the docker socket and reads the bus like any other client -- no new broker
surface. Provisioning takes the bound token as INPUT (operator mints in /ui, pipes it
here); the launcher holds NO standing broker credential and launcher.db NEVER persists
a token or a gate secret (R1) -- env dies with the container and re-provision prompts
again.

  reveille-launch new <role> <repo-url> [--broker URL] [--mem 4g] [--cpus 2]
                                        [--network host] [--image IMG] [--timeout 90]
      Provision one agent container. Token read from stdin (piped) or prompted --
      NEVER argv (argv leaks; the wake-127 lesson is fleet law). Succeeds when the
      broker's presence shows the role live+connected (health-by-presence, 3.2.5).
  reveille-launch ls
  reveille-launch stop <role> | start <role>
  reveille-launch destroy <role> [--purge]     --purge also drops the claude-home volume
  reveille-launch grant <role> <grantee> [--mode viewer|driver] [--ttl 86400]
      Mint a per-grant URL token (docker exec attach-gate mint -- the secret never
      leaves the container) and record the grant. The token is PRINTED ONCE, never
      stored (4.5.2): re-issue is re-mint, never retrieval.
  reveille-launch grants [role]                 list grant records
  reveille-launch revoke <role> <grant-id>      kill d-/v-<id> now; audit line exact
  reveille-launch flip <role> on|off            multi-driver opt-out toggle (4.3)
  reveille-launch sweep [--loop SECONDS]
      The tick 4.6 assigns the launcher: kill d-*/v-* sessions whose grant is
      expired/revoked/mode-mismatched, harvest the gate's ATTACH lines, and derive
      DETACH lines from sessions observed gone (observation time, stated as such).

launcher.db: $REVEILLE_LAUNCH_DB or ~/.reveille/launcher.db. Container + grant
records only, never a secret and never a minted token. Audit log: audit.log next
to the db ($REVEILLE_LAUNCH_AUDIT overrides).
"""
import argparse
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
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
DEFAULT_IMAGE = os.environ.get("REVEILLE_AGENT_IMAGE", "reveille-agent:0.2.0")
ROLE_RE = re.compile(r"\A[a-z0-9][a-z0-9-]{1,63}\Z")

# Secrets ride the ENVIRONMENT of the docker-run child (docker reads `-e NAME` from the
# launcher's env), so their VALUES never appear in argv. This is the whole no-argv
# discipline in one list: names here, values in env, never the two together on a
# command line a `ps` can read.
ENV_PASSTHROUGH_SECRET = ("REVEILLE_TOKEN", "REVEILLE_GATE_SECRET")


def die(msg, code=2):
    print(f"reveille-launch: {msg}", file=sys.stderr)
    raise SystemExit(code)


def container_name(role):
    return f"rev-{role}"


def volume_name(role):
    return f"rev-{role}-claude"


def docker_run_argv(role, image, mem, cpus, network, forward_anthropic, boot_cmd=None):
    """The docker-run command as argv. `-e NAME` entries pass values BY NAME from the
    child's env, so no secret is ever a token in this list -- the test asserts exactly
    that. boot_cmd overrides the image's default (claude reveille) -- for a shell, a
    diagnostic like `agent-probe`, or any ops need. Pure: no env read, no side effects."""
    argv = [
        "docker", "run", "-d",
        "--name", container_name(role),
        "--label", f"reveille.role={role}",
        "--restart", "unless-stopped",
        "--network", network,
        "--memory", mem,
        "--cpus", str(cpus),
        "-v", f"{volume_name(role)}:/home/agent/.claude",
        "-e", "REVEILLE_AGENT_ROLE",
        "-e", "REVEILLE_URL",
        "-e", "REVEILLE_REPO_URL",
    ]
    for name in ENV_PASSTHROUGH_SECRET:
        argv += ["-e", name]
    if forward_anthropic:
        # Headless claude needs its Anthropic credential; the claude-home volume carries
        # a login (R2), but a fresh volume has none, so forward the operator's key by
        # name if they have one. Not a broker secret, not persisted.
        argv += ["-e", "ANTHROPIC_API_KEY"]
    argv.append(image)
    if boot_cmd:
        import shlex
        argv += shlex.split(boot_cmd)
    return argv


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

def _db(path=None):
    path = path or os.environ.get(
        "REVEILLE_LAUNCH_DB", os.path.expanduser("~/.reveille/launcher.db"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE IF NOT EXISTS containers("
        "role TEXT PRIMARY KEY, repo_url TEXT, container TEXT, volume TEXT, "
        "image TEXT, broker_url TEXT, created_ns INTEGER)")
    # Grant records (4.5.2): metadata ONLY -- the minted token is signable solely
    # with the container's gate secret, which this process never persists. A db
    # that could reproduce a live token would be the standing-credential store
    # 3.1 forbids.
    conn.execute(
        "CREATE TABLE IF NOT EXISTS grants("
        "id TEXT PRIMARY KEY, role TEXT, grantee TEXT, mode TEXT, "
        "issued_ns INTEGER, expiry_ns INTEGER, revoked_ns INTEGER)")
    # What the sweep saw last tick, so a session OBSERVED GONE yields a DETACH
    # line (4.5.2: observation time, never a fabricated event time).
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sessions_seen("
        "role TEXT, session TEXT, first_seen_ns INTEGER, "
        "PRIMARY KEY(role, session))")
    return conn


def _record(conn, role, repo_url, image, broker_url):
    conn.execute(
        "INSERT OR REPLACE INTO containers"
        "(role, repo_url, container, volume, image, broker_url, created_ns) "
        "VALUES(?,?,?,?,?,?,?)",
        (role, repo_url, container_name(role), volume_name(role), image,
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

def cmd_new(a):
    if not ROLE_RE.match(a.role):
        die(f"bad role {a.role!r}: lowercase alnum + dash, 2-64 chars")
    # Re-provision is routine (3.5) but must never be UNPROMPTED: an accidental `new`
    # on a live role would destroy its session mid-conversation. Refuse unless the
    # operator says --replace; the volume (login) survives either way.
    if _exists(container_name(a.role)) and not a.replace:
        die(f"{container_name(a.role)} already exists -- `destroy {a.role}` first, "
            f"or pass --replace to re-provision (its claude-home volume is kept)")
    token = read_secret(f"bound broker token for {a.role}")
    if not token:
        die("no token given (stdin empty and no prompt)")

    env = dict(
        os.environ,
        REVEILLE_AGENT_ROLE=a.role,
        REVEILLE_URL=a.broker,
        REVEILLE_REPO_URL=a.repo_url,
        REVEILLE_TOKEN=token,
        # Per-container gate secret (T1 4.3), minted HERE at provision, injected by
        # name, never stored -- dies with the container, re-provision mints a new one.
        REVEILLE_GATE_SECRET=secrets.token_hex(32),
    )

    _ensure_network(a.network, a.broker)
    _docker("volume", "create", volume_name(a.role), capture=True)
    if a.replace:
        _docker("rm", "-f", container_name(a.role), check=False, capture=True)
    argv = docker_run_argv(a.role, a.image, a.mem, a.cpus, a.network,
                           forward_anthropic=bool(os.environ.get("ANTHROPIC_API_KEY")),
                           boot_cmd=a.boot_cmd)
    subprocess.run(argv, env=env, check=True, stdout=subprocess.DEVNULL)

    conn = _db()
    _record(conn, a.role, a.repo_url, a.image, a.broker)
    conn.close()

    print(f"provisioned {container_name(a.role)} on network {a.network}; waiting for "
          f"presence live+connected (timeout {a.timeout}s)...")
    if wait_healthy(a.health_url, a.role, token, a.timeout):
        print(f"OK: {a.role} is live+connected (broker {a.broker})")
        return 0
    print(f"UNHEALTHY: {a.role} never reached live+connected. Inspect:\n"
          f"  docker logs --tail 20 {container_name(a.role)}", file=sys.stderr)
    return 1


def cmd_ls(a):
    conn = _db()
    rows = conn.execute("SELECT * FROM containers ORDER BY role").fetchall()
    conn.close()
    if not rows:
        print("no containers provisioned")
        return 0
    for r in rows:
        st = _docker("inspect", "-f", "{{.State.Status}}", r["container"],
                     check=False, capture=True)
        status = (st.stdout or "").strip() or "absent"
        print(f"{r['role']:20s} {status:10s} {r['image']:22s} {r['repo_url']}")
    return 0


def cmd_stop(a):
    _docker("stop", container_name(a.role))
    return 0


def cmd_start(a):
    _docker("start", container_name(a.role))
    return 0


def cmd_destroy(a):
    _docker("rm", "-f", container_name(a.role), check=False, capture=True)
    if a.purge:
        _docker("volume", "rm", volume_name(a.role), check=False, capture=True)
        print(f"destroyed {a.role} and purged its claude-home volume")
    else:
        print(f"destroyed {a.role}; kept volume {volume_name(a.role)} (--purge to drop)")
    conn = _db()
    conn.execute("DELETE FROM containers WHERE role=?", (a.role,))
    # Grants die with the container (4.5) -- the gate secret they were signed
    # against is gone, so the records are history, not authority.
    conn.execute("DELETE FROM grants WHERE role=?", (a.role,))
    conn.execute("DELETE FROM sessions_seen WHERE role=?", (a.role,))
    conn.commit()
    conn.close()
    return 0


def _grant_row(conn, role, grant_id):
    row = conn.execute("SELECT * FROM grants WHERE id=? AND role=?",
                       (grant_id, role)).fetchone()
    if row is None:
        die(f"no grant {grant_id} on {role} (see `grants {role}`)")
    return row


def _known_role(conn, role):
    if conn.execute("SELECT 1 FROM containers WHERE role=?", (role,)).fetchone() is None:
        die(f"unknown role {role!r} -- provision it first (`new`)")


def cmd_grant(a):
    conn = _db()
    _known_role(conn, a.role)
    gid = secrets.token_hex(4)
    # Mint INSIDE the container: the gate secret never leaves it, re-issue is
    # re-mint (4.5.2). The launcher only ever holds the token long enough to
    # print it once.
    res = _docker("exec", container_name(a.role),
                  "attach-gate", "mint", a.mode, str(a.ttl), gid,
                  check=False, capture=True)
    token = (res.stdout or "").strip()
    if res.returncode != 0 or not token.startswith("v1."):
        die(f"mint failed in {container_name(a.role)} -- is it running?")
    now = time.time_ns()
    conn.execute(
        "INSERT INTO grants(id, role, grantee, mode, issued_ns, expiry_ns, revoked_ns) "
        "VALUES(?,?,?,?,?,?,NULL)",
        (gid, a.role, a.grantee, a.mode, now, now + a.ttl * 10**9))
    conn.commit()
    conn.close()
    ip = _docker("inspect", "-f",
                 "{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}",
                 container_name(a.role), check=False, capture=True)
    host = (ip.stdout or "").strip() or container_name(a.role)
    exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + a.ttl))
    print(f"grant {gid}: {a.mode} for {a.grantee} on {a.role}, expires {exp}")
    print(f"attach: http://{host}:7681/?arg={token}")
    print("token shown once, never stored; lost token = revoke + re-grant")
    return 0


def cmd_grants(a):
    conn = _db()
    q, args = "SELECT * FROM grants", ()
    if a.role:
        q += " WHERE role=?"
        args = (a.role,)
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
        print(f"{r['id']}  {r['role']:16s} {r['grantee']:16s} "
              f"{r['mode']:6s} {state:8s} expires {exp}")
    return 0


def _kill_grant_sessions(role, grant_id):
    for prefix in ("d", "v"):  # both: a flip may have left the other-mode name
        _docker("exec", container_name(role),
                "tmux", "kill-session", "-t", f"{prefix}-{grant_id}",
                check=False, capture=True)


def cmd_revoke(a):
    conn = _db()
    row = _grant_row(conn, a.role, a.grant_id)
    # Kill first, record after: the <1s promise is about the client dropping.
    _kill_grant_sessions(a.role, a.grant_id)
    conn.execute("UPDATE grants SET revoked_ns=? WHERE id=?",
                 (time.time_ns(), a.grant_id))
    # The killed session must not also read as an observed DETACH next tick.
    conn.execute("DELETE FROM sessions_seen WHERE role=? AND session IN (?,?)",
                 (a.role, f"d-{a.grant_id}", f"v-{a.grant_id}"))
    conn.commit()
    conn.close()
    _audit("REVOKE", role=a.role, grant=a.grant_id, mode=row["mode"],
           grantee=row["grantee"], actor="launcher-cli")
    print(f"revoked {a.grant_id} ({row['mode']} for {row['grantee']} on {a.role}); "
          f"its token stays signed until expiry -- the sweep kills any re-attach")
    return 0


def cmd_flip(a):
    conn = _db()
    _known_role(conn, a.role)
    conn.close()
    # Runtime toggle rides a marker file the gate checks per-attach: ttyd's
    # children only ever see create-time env, so env alone cannot flip live.
    shell = ("touch ~/.multi-driver" if a.state == "on"
             else "rm -f ~/.multi-driver")
    res = _docker("exec", container_name(a.role), "sh", "-c", shell,
                  check=False, capture=True)
    if res.returncode != 0:
        die(f"flip failed in {container_name(a.role)} -- is it running?")
    _audit("FLIP", role=a.role, multi_driver=a.state, actor="launcher-cli")
    print(f"{a.role}: multi-driver {a.state}")
    return 0


def _live_grant_sessions(role):
    res = _docker("exec", container_name(role),
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


def _harvest_gate_audit(role, grants_by_id):
    """Pull the gate's ATTACH lines (read-and-truncate) into the host log,
    keeping the gate's own timestamps and resolving grantee from the record."""
    # 4.6.1: move-then-read, never read-then-truncate -- a gate append between
    # cat and truncate would be dropped, and a boundary log with a known drop
    # window is not a boundary. rename is atomic within the fs; an append that
    # loses the race lands in a fresh ~/.attach-audit and survives to the next
    # tick. The $$-suffix keeps a crashed harvest's remainder readable (glob),
    # at worst re-reading it -- a duplicate line over a dropped one, always.
    res = _docker("exec", container_name(role), "sh", "-c",
                  "[ -f ~/.attach-audit ] && mv ~/.attach-audit ~/.attach-audit.h.$$;"
                  " cat ~/.attach-audit.h.* 2>/dev/null; rm -f ~/.attach-audit.h.*",
                  check=False, capture=True)
    for line in (res.stdout or "").splitlines():
        parts = line.split()  # <ts> ATTACH <mode> <id>
        if len(parts) != 4 or parts[1] != "ATTACH":
            continue
        ts, _, mode, gid = parts
        g = grants_by_id.get(gid)
        _audit("ATTACH", ts_iso=ts, role=role, grant=gid, mode=mode,
               grantee=(g["grantee"] if g else "unknown"), src="gate")


def _sweep_once(conn):
    now = time.time_ns()
    for c in conn.execute("SELECT role FROM containers").fetchall():
        role = c["role"]
        grants = {r["id"]: r for r in conn.execute(
            "SELECT * FROM grants WHERE role=?", (role,)).fetchall()}
        _harvest_gate_audit(role, grants)
        live = _live_grant_sessions(role)
        seen = {r["session"] for r in conn.execute(
            "SELECT session FROM sessions_seen WHERE role=?", (role,)).fetchall()}
        kills, detaches = sweep_actions(grants, live, seen, now)
        for session, reason in kills:
            _docker("exec", container_name(role),
                    "tmux", "kill-session", "-t", session,
                    check=False, capture=True)
            gid = session[2:]
            g = grants.get(gid)
            _audit("KILL", role=role, grant=gid, session=session, reason=reason,
                   grantee=(g["grantee"] if g else "unknown"))
        for session in detaches:
            gid = session[2:]
            g = grants.get(gid)
            _audit("DETACH", role=role, grant=gid,
                   mode=("driver" if session.startswith("d-") else "viewer"),
                   grantee=(g["grantee"] if g else "unknown"),
                   observed="sweep-tick")  # observation time, not event time
        killed = {s for s, _ in kills}
        conn.execute("DELETE FROM sessions_seen WHERE role=?", (role,))
        conn.executemany(
            "INSERT INTO sessions_seen(role, session, first_seen_ns) VALUES(?,?,?)",
            [(role, s, now) for s in set(live) - killed])
    conn.commit()


def cmd_sweep(a):
    conn = _db()
    while True:
        _sweep_once(conn)
        if not a.loop:
            break
        time.sleep(a.loop)
    conn.close()
    return 0


def build_parser():
    p = argparse.ArgumentParser(prog="reveille-launch", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("new", help="provision one agent container")
    n.add_argument("role")
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
                   help="re-provision an existing role (destroys its container; the "
                        "claude-home volume is kept)")
    n.add_argument("--image", default=DEFAULT_IMAGE)
    n.add_argument("--mem", default="4g")
    n.add_argument("--cpus", default="2")
    n.add_argument("--timeout", type=int, default=90)
    n.add_argument("--boot-cmd", default=None,
                   help="override the image command (default: claude reveille); "
                        "e.g. agent-probe to health-check without an Anthropic login")
    n.set_defaults(fn=cmd_new)

    sub.add_parser("ls", help="list provisioned containers").set_defaults(fn=cmd_ls)

    for name, fn in (("stop", cmd_stop), ("start", cmd_start)):
        s = sub.add_parser(name)
        s.add_argument("role")
        s.set_defaults(fn=fn)

    d = sub.add_parser("destroy")
    d.add_argument("role")
    d.add_argument("--purge", action="store_true",
                   help="also drop the claude-home volume (default keeps the login)")
    d.set_defaults(fn=cmd_destroy)

    g = sub.add_parser("grant", help="mint a per-grant URL token (printed once)")
    g.add_argument("role")
    g.add_argument("grantee", help="who this grant names (audit attribution, Q3)")
    g.add_argument("--mode", choices=("viewer", "driver"), default="viewer",
                   help="driver is the agent's whole identity (4.4); default viewer")
    g.add_argument("--ttl", type=int, default=86400,
                   help="seconds until expiry (Q2: 24h default, renew = re-grant)")
    g.set_defaults(fn=cmd_grant)

    gl = sub.add_parser("grants", help="list grant records")
    gl.add_argument("role", nargs="?", default=None)
    gl.set_defaults(fn=cmd_grants)

    r = sub.add_parser("revoke", help="kill the grant's session now (<1s)")
    r.add_argument("role")
    r.add_argument("grant_id")
    r.set_defaults(fn=cmd_revoke)

    f = sub.add_parser("flip", help="toggle multi-driver on a container (4.3)")
    f.add_argument("role")
    f.add_argument("state", choices=("on", "off"))
    f.set_defaults(fn=cmd_flip)

    s = sub.add_parser("sweep", help="expiry/revoke sweep + audit harvest (4.6)")
    s.add_argument("--loop", type=int, default=0, metavar="SECONDS",
                   help="repeat every N seconds (default: one tick and exit)")
    s.set_defaults(fn=cmd_sweep)
    return p


def main():
    a = build_parser().parse_args()
    raise SystemExit(a.fn(a))


if __name__ == "__main__":
    main()
