#!/usr/bin/env python3
"""A whole bus on this machine, from a sample dataset, in one command.

WHY THIS EXISTS. The live bus is the only place several of these shapes have
ever been exercised, and "try it and see" against the fleet's own broker is how
a bad afternoon starts. This builds a COMPLETE one locally -- database, rooms,
people, agents, mail, memories -- so a change can be driven end to end, looked
at in the real UI, and thrown away.

TWO RULES, and both are the point:

1. THE DATASET IS DATA, AND IT IS REAPPLIED FROM SCRATCH. `--wipe` deletes the
   database and rebuilds it from DATASET below, so there is always a way back
   to a known state, and growing the fixture is editing a list rather than
   remembering which rows somebody inserted by hand last month.

2. EVERY ROW GOES THROUGH THE PUBLIC API, never an INSERT. A raw INSERT keeps
   working after the shape it writes has changed -- it just writes something
   the application can no longer read, and the fixture rots silently into a
   thing that tests nothing. Seeding through store.create_room / join / send /
   memory_add means a schema change BREAKS THE SEED, loudly, here, which is
   exactly when it is cheap to notice.

TIMES ARE RELATIVE TO NOW, so presence and activity mean what they say every
time it runs: an agent that spoke seconds ago reads active, one that has been
quiet for an hour reads idle, and the row that left is absent from the rail
rather than merely old.

    python scripts/local_bus.py --wipe          rebuild and serve
    python scripts/local_bus.py                 serve what is there
    python scripts/local_bus.py --wipe --seed-only
    python scripts/local_bus.py --port 8799 --db /tmp/bus.db
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from reveille import store                                      # noqa: E402

SEC = 10 ** 9
# TWO PEOPLE, BECAUSE THE UI IS DIFFERENT FOR THEM. An owner sees the agent
# management, the token doors and every room; a plain member sees what a
# colleague sees. A harness with only an admin cannot show anyone the second
# view, which is the one most contributors are about to change.
# THE NAME, TWICE. A longer password would be theatre -- these are in this file,
# in a public repo, for a database `--wipe` destroys, and dressing them up as
# secrets would only invite somebody to treat this bus as one. `admin`/`user`
# would be better still and the BROKER REFUSES THEM: "password must be at least
# 8 characters", its own rule, enforced because this harness seeds through the
# public API rather than around it.
USERS = [
    {"name": "admin", "password": "adminadmin", "role": "admin"},
    {"name": "user", "password": "useruser", "role": "user"},
]
OWNER = USERS[0]["name"]
OWNER_PW = USERS[0]["password"]

# THE SAMPLE DATASET. Every state the UI draws differently is represented, so a
# rendering change can be judged against all of them at once instead of against
# whichever one the live bus happens to be in.
#
#   runtime   which CLI flavour the body reports at attach
#   sessions  what waked last saw in its directory: >0 a body is there, 0 gone,
#             -1 never said (an old daemon), which must fall back, not read gone
#   spoke/rung  seconds ago that direction last moved; None means never
#   left      called leave(): keeps its history, must NOT appear in the rail
AGENTS = [
    {"name": "local-architect", "runtime": "claude", "sessions": 1,
     "spoke": 4, "rung": 12, "left": False},
    {"name": "local-codex-dev", "runtime": "codex", "sessions": 2,
     "spoke": 30, "rung": 3, "left": False},
    {"name": "local-quiet", "runtime": "claude", "sessions": 1,
     "spoke": 3600, "rung": None, "left": False},
    # A body that is GONE but still addressed: offline is not gone-for-good,
    # mail queues and is read on return, so the row stays and stays addressable.
    {"name": "local-offline", "runtime": "codex", "sessions": 0,
     "spoke": 7200, "rung": None, "left": False},
    # An OLD daemon that cannot report a count. Must not read as gone.
    {"name": "local-oldwaked", "runtime": "", "sessions": -1,
     "spoke": 900, "rung": None, "left": False},
    {"name": "local-departed", "runtime": "claude", "sessions": 0,
     "spoke": 86400, "rung": None, "left": True},
]

MESSAGES = [
    ("local-architect", "*", "the shape as it stands",
     "Three states, not two: connected, offline, left. Offline keeps its row."),
    ("local-codex-dev", "local-architect", "NEED: ruling",
     "Codex has no per-agent instruction file. Shared AGENTS.md, identity from "
     "the bus. Confirm before I build on it."),
    ("local-architect", "local-codex-dev", "re: NEED: ruling",
     "Confirmed. No identity in a tracked file."),
    # Unread DIRECT mail for a body that is not there: the queue that proves
    # offline is still addressable.
    ("local-architect", "local-offline", "for your return",
     "Not urgent. Read this when you are back."),
]

MEMORIES = [
    ("doctrine", "ANIMATION FOLLOWS OBSERVATION, NEVER INFERENCE. The bus can "
                 "see that a call landed and that a ring was delivered. It "
                 "cannot see an agent think."),
    ("contract", "A HEARTBEAT IS NOT A BUS MESSAGE IN EITHER DIRECTION. It "
                 "says the socket is alive and nothing about traffic."),
    ("decision", "Presence is three states. `left` leaves the rail; `offline` "
                 "keeps its row and stays addressable."),
    # NO lesson ROWS HERE, and the API is why: memory_add refuses kind='lesson'
    # ("lessons go through lesson_add(), which owns their gate"), which is an
    # MCP verb rather than a store call. Seeding one would mean reaching around
    # that gate -- the exact thing seeding through the public API exists to
    # prevent. A fixture that needs lessons drives lesson_add over the running
    # bus, against this same database.
    ("doctrine", "A signal outlives the architecture that made it true. "
                 "`connected` meant a wake socket, which one host daemon makes "
                 "true for every identity it serves."),
]


def wipe(db):
    for suffix in ("", "-wal", "-shm"):
        path = pathlib.Path(str(db) + suffix)
        if path.exists():
            path.unlink()
    print(f"local-bus: wiped {db}")


def seed(db):
    """Build the whole fixture through the public API. Returns the secrets."""
    conn = store.connect(str(db))
    store.migrate(conn, str(db))
    now = time.time_ns()
    people = {u["name"]: store.create_user(conn, u["name"], u["password"],
                                           role=u["role"]) for u in USERS}
    owner = people[OWNER]
    room = store.create_room(conn, owner["id"], "Local")
    # The plain member is IN the room, or "what a colleague sees" is an empty
    # page rather than a second view of the same bus.
    with store.tx(conn):
        store.join(conn, "user", "web:user", room["id"],
                   token_id=None, url=None)
    secrets = {}
    for spec in AGENTS:
        tok = store.create_token(conn, owner["id"], spec["name"],
                                 agent_name=spec["name"], create=True)
        store.assign_room(conn, tok["id"], room["id"], owner["id"])
        store.join(conn, spec["name"], spec["name"], room["id"], tok["id"])
        secrets[spec["name"]] = tok["secret"]
    # The mail, sent as the agents themselves so every row is a real message
    # with a real sender, thread and room.
    for sender, to, subject, body in MESSAGES:
        with store.tx(conn):
            store.send(conn, _principal(conn, sender), to, body, subject=subject,
                       room=room["id"])
    tok_id, agent_id = _token_of(conn, "local-architect")
    for kind, fact in MEMORIES:
        with store.tx(conn):
            store.memory_add(conn, author="local-architect", token_id=tok_id,
                             agent_bound=True, tier="admin", is_admin=True,
                             rooms=[room["id"]], owned_rooms=[room["id"]],
                             fact=fact, kind=kind, scope="global",
                             agent_id=agent_id)
    # THE PRESENCE FACTS LAST, because sending moved them: every send above is
    # a real bus call and stamps spoke_ns. The dataset says what each row should
    # LOOK like, so it is written after the traffic that would disturb it.
    for spec in AGENTS:
        p = _principal(conn, spec["name"])
        with store.tx(conn):
            store.set_attach_facts(conn, p, [room["id"]], "0.2.999",
                                   spec["runtime"])
            store.set_sessions(conn, p, [room["id"]], spec["sessions"])
            if spec["spoke"] is not None:
                store.mark_spoke(conn, p, [room["id"]],
                                 now - spec["spoke"] * SEC)
            if spec["rung"] is not None:
                store.mark_rung(conn, p, [room["id"]], now - spec["rung"] * SEC)
            if spec["left"]:
                store.leave(conn, p, [room["id"]])
    conn.commit()
    conn.close()
    where = write_secrets(db, secrets)
    print(f"local-bus: seeded {db} -- {len(USERS)} users, {len(AGENTS)} agents, "
          f"{len(MESSAGES)} messages, {len(MEMORIES)} memories")
    print(f"local-bus: agent tokens in {where} (0600)")
    return secrets


def _principal(conn, name):
    row = conn.execute("SELECT principal FROM members WHERE name=? LIMIT 1",
                       (name,)).fetchone()
    if not row:
        raise SystemExit(f"local-bus: {name!r} has no membership -- seed order?")
    return row["principal"]


def _token_of(conn, name):
    """The token id and agent id behind an agent NAME, read the way the schema
    actually relates them: tokens carry an agent_id, agents carry the name."""
    row = conn.execute(
        "SELECT t.id AS id, t.agent_id AS agent_id FROM tokens t "
        "JOIN agents a ON a.id = t.agent_id WHERE a.name=? "
        "ORDER BY t.rowid DESC LIMIT 1", (name,)).fetchone()
    if not row:
        raise SystemExit(f"local-bus: no token for {name!r}")
    return row["id"], row["agent_id"]


def _daemon_path():
    """THIS tree's broker, never whatever is on PATH.

    A contributor's machine may well have a released reveille installed; running
    that one would test a build this checkout did not produce, which is exactly
    how the gate scripts came to be testing the wrong artifact.
    """
    for candidate in (pathlib.Path(sys.executable).parent / "reveille-daemon",
                      ROOT / ".venv" / "bin" / "reveille-daemon"):
        if candidate.exists():
            return candidate
    raise SystemExit("local-bus: no reveille-daemon beside this interpreter or "
                     "in .venv -- run `uv sync` first")


def _refuse_if_busy(port):
    """Say WHICH port and WHAT to do, rather than let the daemon die obscurely."""
    import socket
    with socket.socket() as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            raise SystemExit(
                f"local-bus: 127.0.0.1:{port} is already in use. Pick another "
                f"with --port N (or LOCAL_PORT=N make local-bus)") from None


def serve(db, port, secrets, oidc=False):
    """Run THIS TREE's broker, never whatever is on PATH.

    The smoke gates spawned `reveille-daemon` by name and got the INSTALLED
    build -- a different version than the one under test, which is how a schema
    this tree wrote met a broker that refused to read it.
    """
    env = dict(os.environ, REVEILLE_DB=str(db), REVEILLE_PORT=str(port),
               REVEILLE_HOST="127.0.0.1")
    # NO VOICE, NO EAR, NO WRITER. Each is off unless its URL is set, so the
    # harness CLEARS them rather than merely not setting them: inheriting a
    # shell that exports one would point this local bus at a real model host,
    # and the thing being tested here is the bus.
    for var in ("REVEILLE_TTS_URL", "REVEILLE_STT_URL", "REVEILLE_SCRIPT_URL",
                "REVEILLE_TTS_TOKEN", "REVEILLE_STT_TOKEN",
                "REVEILLE_SCRIPT_TOKEN"):
        env.pop(var, None)
    # THE PASSWORD DOOR IS OPEN IFF NO PROVIDER IS CONFIGURED -- one condition,
    # `_password_closed()` is `bool(_oidc_doors)`. So an inherited shell that
    # exports the LIVE broker's REVEILLE_OIDC_* would close the door on this
    # local bus and lock out the very user it just seeded, against a provider
    # whose redirect URI points at production. Cleared unless --oidc says
    # otherwise, which is a decision rather than an accident.
    if not oidc:
        for var in [k for k in env if k.startswith("REVEILLE_OIDC_")]:
            env.pop(var, None)
        env.pop("REVEILLE_PUBLIC_URL", None)
    else:
        env.setdefault("REVEILLE_PUBLIC_URL", f"http://127.0.0.1:{port}")
        doors = sorted({k.split("_")[2].lower() for k in env
                        if k.startswith("REVEILLE_OIDC_") and k.endswith("_ID")})
        if not doors:
            raise SystemExit(
                "local-bus: --oidc but no REVEILLE_OIDC_<PROVIDER>_ID in the "
                "environment. Set the id and secret, and register the redirect "
                f"URI EXACTLY: http://127.0.0.1:{port}/auth/<provider>/callback")
        print(f"local-bus: doors {', '.join(doors)} -- password sign-in is CLOSED "
              f"while a door exists; redirect URI must be registered as "
              f"{env['REVEILLE_PUBLIC_URL']}/auth/<provider>/callback")
    daemon = _daemon_path()
    _refuse_if_busy(port)
    print(f"local-bus: the bus is at http://127.0.0.1:{port}/ui")
    if not oidc:
        for u in USERS:
            print(f"local-bus: sign in {u['name']} / {u['password']}  ({u['role']})")
    if secrets:
        # COPY-PASTEABLE, because the next thing anyone wants is to point a real
        # agent at this bus and watch the rail move.
        print("local-bus: run an agent against it with any of ->")
        for name, secret in secrets.items():
            print(f"  REVEILLE_URL=http://127.0.0.1:{port} "
                  f"REVEILLE_AGENT_ROLE={name} REVEILLE_TOKEN={secret}")
    print("local-bus: ctrl-c to stop")
    try:
        subprocess.run([str(daemon)], env=env, check=False)
    except KeyboardInterrupt:
        print("\nlocal-bus: stopped")


def provision(name, directory, runtime, port, db):
    """Make `directory` a real agent of the LOCAL bus, for a native TUI.

    USERS ARE THE WEB. An agent is a TUI body -- native here, in a container
    elsewhere -- and the way a directory becomes one is the installer itself,
    not a shortcut this harness invents: `reveille init` writes the credential,
    registers the MCP, verifies the headers command answers with this identity,
    and records the directory for waked. Running the real thing is the point;
    a harness that provisioned by hand would test a path nobody ships.
    """
    secret = _secret_of(db, name)
    url = f"http://127.0.0.1:{port}"
    cli = _tree_script("reveille")
    directory = pathlib.Path(directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    cmd = [str(cli), "init", url, name, "-", "--runtime", runtime,
           "--dir", str(directory), "--no-prompt"]
    if runtime == "codex":
        # Codex reads a project's .codex layer only for a trusted project, so an
        # unprovisioned trust makes every other artifact unreachable.
        cmd.append("--trust-project")
    env = dict(os.environ)
    for var in ("REVEILLE_TOKEN", "REVEILLE_AGENT_ROLE", "REVEILLE_URL"):
        env.pop(var, None)   # this directory's identity, never the caller's
    print(f"local-bus: provisioning {directory} as {name} ({runtime})")
    out = subprocess.run(cmd, input=secret + "\n", text=True, env=env)
    if out.returncode:
        raise SystemExit(f"local-bus: `reveille init` exited {out.returncode}")
    bindir = _tree_script("reveille").parent
    print(f"local-bus: now run   cd {directory} && {runtime}")
    # THE REGISTRATION NAMES A COMMAND ON PATH, and the TUI resolves it in the
    # shell YOU start it from -- not in this one. A machine with a released
    # reveille installed answers with that build, which for Codex does not know
    # `--runtime codex` and hands back no identity at all; `init` refuses when
    # that is already true here, and this is the line that makes the session
    # agree with the bus it was provisioned against.
    print(f'local-bus: in that shell, put this checkout first on PATH ->\n'
          f'  export PATH="{bindir}:$PATH"')
    return 0


def secrets_path(db):
    return pathlib.Path(db).with_name("agents.env")


def write_secrets(db, secrets):
    """The one place a seeded secret can be read back.

    A TOKEN IS SHOWN ONCE AND STORED AS A HASH, which is right, and it means a
    harness that wants to provision an agent twenty minutes later has to keep
    what it minted. These belong to a database that `--wipe` destroys, they sit
    0600 beside it inside the ignored .local/, and they are worth exactly one
    local bus -- but they are still credentials, so they are written the way
    credentials are written and never printed into a log file.
    """
    path = secrets_path(db)
    body = "".join(f"{name}={secret}\n" for name, secret in sorted(secrets.items()))
    path.write_text(body)
    path.chmod(0o600)
    return path


def _secret_of(db, name):
    path = secrets_path(db)
    if not path.exists():
        raise SystemExit(f"local-bus: {path} is missing -- the secrets are "
                         f"written when the dataset is seeded. Re-run with --wipe")
    for line in path.read_text().splitlines():
        got, _, secret = line.partition("=")
        if got == name and secret:
            return secret
    known = ", ".join(a["name"] for a in AGENTS)
    raise SystemExit(f"local-bus: no agent called {name!r}. Known: {known}")


def _tree_script(name):
    for candidate in (pathlib.Path(sys.executable).parent / name,
                      ROOT / ".venv" / "bin" / name):
        if candidate.exists():
            return candidate
    raise SystemExit(f"local-bus: no {name} beside this interpreter or in "
                     f".venv -- run `uv sync` first")


def main(argv=None):
    # SERVE IS THE DEFAULT, because it is what anyone runs first and a harness
    # that demands a subcommand to do the obvious thing is one more thing to
    # look up. `provision` is a verb you reach for later, with the bus already
    # running in another terminal.
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "provision":
        return _provision_main(argv[1:])
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0],
        epilog="provision a directory as an agent for a native TUI:\n"
               "  python scripts/local_bus.py provision --name local-architect "
               "--dir ~/agents/arch --runtime claude",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", default=str(ROOT / ".local" / "bus.db"),
                    help="where the local database lives (default .local/bus.db)")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--wipe", action="store_true",
                    help="delete the database and rebuild it from the dataset")
    ap.add_argument("--seed-only", action="store_true",
                    help="build the database and exit without serving")
    ap.add_argument("--oidc", action="store_true",
                    help="keep REVEILLE_OIDC_* from the environment and sign in "
                         "through a provider. This CLOSES the password door, so "
                         "the seeded user cannot sign in; the provider must have "
                         "http://127.0.0.1:<port>/auth/<provider>/callback "
                         "registered exactly")
    a = ap.parse_args(argv)
    db = pathlib.Path(a.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    secrets = {}
    if a.wipe or not db.exists():
        wipe(db)
        secrets = seed(db)
    if a.seed_only:
        return 0
    serve(db, a.port, secrets, oidc=a.oidc)
    return 0


def _provision_main(argv):
    ap = argparse.ArgumentParser(
        prog="local_bus.py provision",
        description="Make a directory a real agent of the local bus, so a "
                    "native claude/codex TUI started there IS that agent.")
    ap.add_argument("--name", required=True,
                    help="which seeded agent this directory becomes")
    ap.add_argument("--dir", required=True, help="the agent's directory")
    ap.add_argument("--runtime", default="claude", choices=("claude", "codex"))
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--db", default=str(ROOT / ".local" / "bus.db"))
    a = ap.parse_args(argv)
    return provision(a.name, a.dir, a.runtime, a.port, pathlib.Path(a.db))


if __name__ == "__main__":
    raise SystemExit(main())
