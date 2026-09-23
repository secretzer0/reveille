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
OWNER = "local"
OWNER_PW = "local-dev-not-a-real-secret"

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
    owner = store.create_user(conn, OWNER, OWNER_PW)
    room = store.create_room(conn, owner["id"], "Local")
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
    print(f"local-bus: seeded {db} -- {len(AGENTS)} agents, "
          f"{len(MESSAGES)} messages, {len(MEMORIES)} memories")
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


def serve(db, port, secrets):
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
    daemon = ROOT / ".venv" / "bin" / "reveille-daemon"
    if not daemon.exists():
        raise SystemExit(f"local-bus: {daemon} is missing -- run `uv sync`")
    print(f"local-bus: http://127.0.0.1:{port}/  (sign in {OWNER} / {OWNER_PW})")
    if secrets:
        print("local-bus: agent tokens ->")
        for name, secret in secrets.items():
            print(f"  REVEILLE_AGENT_ROLE={name} REVEILLE_TOKEN={secret}")
    print("local-bus: ctrl-c to stop")
    try:
        subprocess.run([str(daemon)], env=env, check=False)
    except KeyboardInterrupt:
        print("\nlocal-bus: stopped")


def main(argv=None):
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--db", default=str(ROOT / ".local" / "bus.db"),
                    help="where the local database lives (default .local/bus.db)")
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--wipe", action="store_true",
                    help="delete the database and rebuild it from the dataset")
    ap.add_argument("--seed-only", action="store_true",
                    help="build the database and exit without serving")
    a = ap.parse_args(argv)
    db = pathlib.Path(a.db)
    db.parent.mkdir(parents=True, exist_ok=True)
    secrets = {}
    if a.wipe or not db.exists():
        wipe(db)
        secrets = seed(db)
    if a.seed_only:
        return 0
    serve(db, a.port, secrets)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
