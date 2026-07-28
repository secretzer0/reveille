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
  reveille-launch grant ... | revoke ...        T3 -- not yet

launcher.db: $REVEILLE_LAUNCH_DB or ~/.reveille/launcher.db. Container records only,
never a secret.
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
    return conn


def _record(conn, role, repo_url, image, broker_url):
    conn.execute(
        "INSERT OR REPLACE INTO containers"
        "(role, repo_url, container, volume, image, broker_url, created_ns) "
        "VALUES(?,?,?,?,?,?,?)",
        (role, repo_url, container_name(role), volume_name(role), image,
         broker_url, time.time_ns()))
    conn.commit()


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
    return subprocess.run(
        ["docker", *args], check=check,
        stdout=subprocess.PIPE if capture else None, text=True)


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
    conn.commit()
    conn.close()
    return 0


def cmd_grant(a):
    die("grant/revoke land in T3 (per-grant URL tokens + audit + kill-path)", code=2)


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

    for name in ("grant", "revoke"):
        g = sub.add_parser(name, help="T3 -- not yet")
        g.add_argument("rest", nargs="*")
        g.set_defaults(fn=cmd_grant)
    return p


def main():
    a = build_parser().parse_args()
    raise SystemExit(a.fn(a))


if __name__ == "__main__":
    main()
