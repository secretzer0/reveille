#!/usr/bin/env python3
"""SQLite broker core: users, tokens, rooms, presence, threaded messages, read state.

Pure stdlib. This is the data plane -- no MCP, no wake, no identity magic; those
live in daemon.py. Everything here is a function over a sqlite3 connection so it
is trivially testable, and it is transport-agnostic (stdio or HTTP, same core).

Model
-----
users      web principals (user/pass). role: 'admin' can manage users; 'user' cannot.
sessions   web login state; the cookie carries a secret, only its hash is stored.
rooms      an isolated message space: uuid + a human name, owned by a user. Names
           are unique PER OWNER, not globally -- the UI shows them as "owner -> room".
tokens     an agent credential (REVEILLE_TOKEN). The secret is never stored, only
           its sha256. A token encodes NOTHING about rooms.
token_rooms  which rooms a token may see. Resolved live on every request, so a
           revoke or a flip-to-private takes effect on the next call -- no reissue.
members    one row per (room, agent name): an agent's membership of a room. An
           agent can be a member of several rooms under one token.
messages   append-only. recipient = an agent name, or '*' for broadcast.
           thread_id = the root message's id; parent_id = the direct reply target.
reads      (message_id, agent) -- an agent has consumed a message.
files      an uploaded blob's room, so /files cannot be read across rooms.

A message is UNREAD for agent A when A is a recipient (direct or '*'), A is not
the sender, and no reads row exists for (message, A). Catch-up falls out of this
for free: join() pre-inserts reads rows for everything older than CATCHUP_NS, so
a joiner replays only recent traffic (--fresh pre-inserts for the whole backlog,
starting clean). Older mail stays queryable via history().

Rooms are a hard boundary: a reply never crosses one (send() refuses), and every
read path filters on the caller's room set. Carrying knowledge between rooms is a
NEW root message in the target room, never a threaded reply -- that edge would be
the leak.
"""
import contextlib
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid

# LIVE = heartbeat within this window. Exceeds the standup silence window (30m
# default) so an idle-but-alive agent that only wakes on its heartbeat stays LIVE.
LIVE_TTL_NS = 40 * 60 * 1_000_000_000
# Join catch-up window: a joiner replays only this much backlog by default; older
# mail is auto-marked read. Explicit recall of any period stays available via
# history(since=...).
CATCHUP_NS = 15 * 60 * 1_000_000_000
SESSION_TTL_NS = 14 * 24 * 60 * 60 * 1_000_000_000
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
ROOM_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}")
BROADCAST = "*"
SCHEMA_VERSION = 9

# Entity extraction (DES-001 S2): deterministic, no LLM, the whole list in one place.
# These are the identifier classes the fleet actually cites -- and the recovery path
# for the compounds FTS tokenization fuses (CHANGES 0.2.5): snake_case is in the list
# because "the S2 entities index owns the identifier class" (DES 5) is a promise about
# run_id AND disposal_run_id, not just CamelCase. Extend the list, never fork it.
_REPO_NAMES = ("roc-api", "roc-ui", "controller-api", "controller-ui", "vendor-api",
               "vendor-ui", "minimal-mobile", "streaming", "deployment", "kiosk",
               "reveille", "shared", "mobile")
_ENTITY_RES = (
    # word-dash-number covers the fleet's whole ID idiom in one rule: ADR-061,
    # DES-001, lesson slugs like wake-127. Found live: the ADR-only version left
    # entity=des-001 empty while the thread naming it was right there.
    re.compile(r"\b[A-Za-z]{2,}(?:-[A-Za-z0-9]+)*-\d+\b"),
    re.compile(r"(?<![\w&])#\d+\b"),                        # #263 (PRs/issues)
    re.compile(r"\bproto-v\d+\.\d+\.\d+\b", re.I),          # proto-v3.6.2
    re.compile(r"\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"),   # RunStatus (>=2 humps)
    re.compile(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"),       # run_id, disposal_run_id
    re.compile(r"\b(?:%s)\b" % "|".join(_REPO_NAMES), re.I),
)


def extract_entities(text):
    """Every entity the patterns above find, lowercased and deduped. Lowercase IS the
    normal form -- the entity= filter normalizes the same way, so RunStatus and
    runstatus are one key."""
    found = set()
    for rx in _ENTITY_RES:
        found.update(m.group(0).lower() for m in rx.finditer(text or ""))
    return found

# scrypt cost. 128 * n * r = 16 MB per hash; maxmem must clear that or it raises.
_SCRYPT = dict(n=2**14, r=8, p=1, dklen=32, maxmem=64 * 1024 * 1024)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL UNIQUE,
    pw_hash    TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    created_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_ns INTEGER NOT NULL,
    expires_ns INTEGER NOT NULL
);
-- A room name is unique per owner, not globally: two users may each own a
-- "Reveille". The UI disambiguates by showing "owner -> room", which is exactly
-- what the UNIQUE below makes safe.
CREATE TABLE IF NOT EXISTS rooms (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    owner_id     TEXT REFERENCES users(id),
    public       INTEGER NOT NULL DEFAULT 0,
    retention_ns INTEGER,
    created_ns   INTEGER NOT NULL,
    UNIQUE (owner_id, name)
);
-- The secret is shown once at creation and never stored; secret_hash is a plain
-- sha256 because token_urlsafe(32) is already 256 bits of entropy -- a KDF buys
-- nothing against a brute force that is already infeasible. Passwords are the
-- opposite case and get scrypt.
CREATE TABLE IF NOT EXISTS tokens (
    id           TEXT PRIMARY KEY,
    secret_hash  TEXT NOT NULL UNIQUE,
    owner_id     TEXT NOT NULL REFERENCES users(id),
    label        TEXT NOT NULL DEFAULT '',
    -- NULL = unbound (the migration-era fleet token: X-Agent stays self-asserted).
    -- Set = this credential IS that agent: a presented X-Agent must equal it or the
    -- request is a 401, same check on the wake WS. Immutable after mint -- rebinding
    -- a credential is a new token. agents == tokens is also what makes per-agent
    -- metering one count(*) (DECISIONS).
    agent_name   TEXT,
    -- Memory write tier (DES-001 section 6): state < write < ratify. Every new token
    -- starts at 'state' -- randy day one reads the hive and writes only his own state.
    mem_tier     TEXT NOT NULL DEFAULT 'state'
                 CHECK (mem_tier IN ('state','write','ratify')),
    created_ns   INTEGER NOT NULL,
    last_used_ns INTEGER
);
-- The token->rooms mapping lives here, NOT in the token. Read on every request so
-- assign/unassign/revoke/flip-to-private are all instant.
CREATE TABLE IF NOT EXISTS token_rooms (
    token_id TEXT NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
    room_id  TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    PRIMARY KEY (token_id, room_id)
);
-- One row per (room, agent). Names are unique WITHIN a room -- that is all bus
-- routing needs, and it lets one agent hold the same name in several rooms.
CREATE TABLE IF NOT EXISTS members (
    room_id   TEXT NOT NULL REFERENCES rooms(id),
    name      TEXT NOT NULL,
    tag       TEXT,
    url       TEXT,
    token_id  TEXT REFERENCES tokens(id),
    joined_ns INTEGER NOT NULL,
    seen_ns   INTEGER NOT NULL,
    PRIMARY KEY (room_id, name)
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER,
    parent_id INTEGER REFERENCES messages(id),
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL,
    room      TEXT NOT NULL REFERENCES rooms(id),
    ts_ns     INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS reads (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    agent      TEXT NOT NULL,
    read_ns    INTEGER NOT NULL,
    PRIMARY KEY (message_id, agent)
);
-- The full reply graph. parent_id on messages is the PRIMARY parent (cheap linear
-- back-trace + which thread a reply joins); links holds EVERY parent edge, so a
-- message can re-link/merge several branches (a message may have many parents).
-- messages form a DAG: fork = one parent, many children; re-link = many parents.
CREATE TABLE IF NOT EXISTS links (
    parent_id INTEGER NOT NULL REFERENCES messages(id),
    child_id  INTEGER NOT NULL REFERENCES messages(id),
    PRIMARY KEY (parent_id, child_id)
);
-- 1-n file attachments per message. The bytes live on disk under the broker's
-- files/ dir (served at /files/<url basename>); rows carry only the reference.
CREATE TABLE IF NOT EXISTS attachments (
    id         INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL REFERENCES messages(id),
    url        TEXT NOT NULL,
    name       TEXT,
    bytes      INTEGER
);
-- An upload's room, recorded at upload time so GET /files/<stored> can be refused
-- to a principal outside that room. Without this the blob store is world-readable
-- to anyone who learns a filename.
CREATE TABLE IF NOT EXISTS files (
    stored      TEXT PRIMARY KEY,
    room_id     TEXT NOT NULL REFERENCES rooms(id),
    uploaded_by TEXT NOT NULL,
    ts_ns       INTEGER NOT NULL
);
-- Lessons live in memories (kind='lesson') since v9 -- the structured columns ride
-- along nullable, lessons()/lesson_add() keep their signatures, and the fold-in is
-- DES-001 S3's clean cutover: one store, no dual path.
CREATE INDEX IF NOT EXISTS idx_att_message   ON attachments(message_id);
CREATE INDEX IF NOT EXISTS idx_msg_room      ON messages(room);
CREATE INDEX IF NOT EXISTS idx_msg_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_msg_thread    ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_msg_sender    ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_msg_parent    ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_links_child   ON links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent  ON links(parent_id);
CREATE INDEX IF NOT EXISTS idx_reads_agent   ON reads(agent);
CREATE INDEX IF NOT EXISTS idx_troom_room    ON token_rooms(room_id);
CREATE INDEX IF NOT EXISTS idx_members_name  ON members(name);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    subject, body,
    content='messages', content_rowid='id',
    tokenize="unicode61 tokenchars '-_'"
);
CREATE TABLE IF NOT EXISTS message_entities (
    entity     TEXT NOT NULL,
    message_id INTEGER NOT NULL,
    PRIMARY KEY (entity, message_id)
);
CREATE INDEX IF NOT EXISTS idx_msgent_msg ON message_entities(message_id);
"""

# The hive memory store (DES-001 S3). Separate constant so _upgrade_v8 can lay exactly
# this without replaying the whole schema; the fresh-db path gets it via _SCHEMA below.
_MEMORIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            INTEGER PRIMARY KEY,
    uid           TEXT NOT NULL UNIQUE,
    kind          TEXT NOT NULL CHECK (kind IN
                    ('doctrine','contract','decision','lesson','state')),
    scope         TEXT NOT NULL,
    fact          TEXT NOT NULL CHECK (length(fact) <= 1000),
    entities      TEXT NOT NULL DEFAULT '',
    source_msg_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    supersedes_id INTEGER REFERENCES memories(id),
    slug          TEXT,
    symptom       TEXT, root_cause TEXT, rule TEXT, detection TEXT,
    author        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'live'
                  CHECK (status IN ('live','draft','superseded','retracted')),
    occurred_ns   INTEGER,
    created_ns    INTEGER NOT NULL,
    expires_ns    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_super ON memories(supersedes_id);
CREATE INDEX IF NOT EXISTS idx_mem_slug  ON memories(scope, slug) WHERE slug IS NOT NULL;
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    fact, entities,
    content='memories', content_rowid='id',
    tokenize="unicode61 tokenchars '-_'"
);
CREATE TABLE IF NOT EXISTS memory_entities (
    entity    TEXT NOT NULL,
    memory_id INTEGER NOT NULL,
    PRIMARY KEY (entity, memory_id)
);
CREATE INDEX IF NOT EXISTS idx_mement_mem ON memory_entities(memory_id);
"""
_SCHEMA += _MEMORIES_SCHEMA
# messages_fts (DES-001 S1; tokenizer measured on the live corpus, bus msg 8366):
# unicode61 with tokenchars '-_' keeps fleet vocabulary (ADR-061, wake-127, run_id) as
# single tokens; trigram was refuted because <3-char queries (S1, qa) can never match a
# trigram index. External-content FTS is synced MANUALLY at the store's write choke
# points -- send() and _delete_messages() -- never by triggers: _exec_script splits DDL
# on ';' and would shred a trigger body. The 'delete' sync command must carry the OLD
# indexed values (FTS5 manual, "External Content Tables"), which is why
# _delete_messages reads the doomed rows before deleting them.


class BusError(Exception):
    """Caller-facing error (bad name, name collision, unknown agent)."""


class AuthError(Exception):
    """No/!valid credential. The transport turns this into a 401."""


class AccessError(Exception):
    """Valid principal, but not for this room/resource. The transport 403s it."""


class AmbiguousRoom(Exception):
    """2+ rooms in reach and the caller named none. The transport 400s it with
    the room list -- a guess here would post into the wrong room, which is not
    recoverable, while an error is."""

    def __init__(self, rooms):
        self.rooms = rooms
        super().__init__("room_required")


def valid_name(name):
    if not NAME_RE.fullmatch(name or ""):
        raise BusError(
            f"invalid name {name!r}: use [A-Za-z0-9_-], 1-64 chars, not starting with - or _"
        )


def valid_room_name(name):
    if not ROOM_NAME_RE.fullmatch(name or ""):
        raise BusError(f"invalid room name {name!r}: 1-64 chars of [A-Za-z0-9 _.-]")


def _uuid():
    return uuid.uuid4().hex


def _exec_script(conn, script):
    """Run a multi-statement DDL script one statement at a time.

    NOT executescript(): that issues an implicit COMMIT on any pending transaction,
    which would silently end the migration's transaction halfway through and leave a
    half-migrated DB with no rollback.

    Comments are stripped before splitting -- a prose semicolon inside a `--` comment
    would otherwise cut a statement in half. No string literal here contains `--`.
    """
    bare = "\n".join(line.split("--")[0] for line in script.splitlines())
    for stmt in bare.split(";"):
        if stmt.strip():
            conn.execute(stmt)


@contextlib.contextmanager
def tx(conn):
    """One explicit transaction. The connection is autocommit (isolation_level=None),
    so any multi-statement mutation MUST run inside this or it can half-apply."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def _version(conn):
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn, db_path):
    """Bring the DB to SCHEMA_VERSION. Explicit and versioned -- the old blind
    try/except ALTER could not tell "no such table" from "already migrated" from a
    real failure, and swallowed all three."""
    v = _version(conn)
    if v > SCHEMA_VERSION:
        raise BusError(f"db is newer than this build (user_version={v})")
    if v == SCHEMA_VERSION:
        return v
    if not _table_exists(conn, "messages"):        # fresh db: just lay the schema down
        _exec_script(conn, _SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return SCHEMA_VERSION
    # Branch on the version we FOUND, not the one we are at mid-chain: _upgrade_v0 lays
    # down the current schema, so it lands straight at SCHEMA_VERSION and must not then be
    # handed to v3's step (which looks for a revoked_ns column v0 never grows).
    if v == 0:
        _upgrade_v0(conn, db_path)
    elif v == 2:
        _upgrade_v2(conn, db_path)
        _upgrade_v3(conn, db_path)
        _upgrade_v4(conn, db_path)
        _upgrade_v5(conn, db_path)
        _upgrade_v7(conn, db_path)
        _upgrade_v8(conn, db_path)
    elif v == 3:
        _upgrade_v3(conn, db_path)
        _upgrade_v4(conn, db_path)
        _upgrade_v5(conn, db_path)
        _upgrade_v7(conn, db_path)
        _upgrade_v8(conn, db_path)
    elif v == 4:
        _upgrade_v4(conn, db_path)
        _upgrade_v5(conn, db_path)
        _upgrade_v7(conn, db_path)
        _upgrade_v8(conn, db_path)
    elif v == 5:
        _upgrade_v5(conn, db_path)
        _upgrade_v7(conn, db_path)
        _upgrade_v8(conn, db_path)
    elif v == 6:
        _upgrade_v6(conn, db_path)
        _upgrade_v7(conn, db_path)
        _upgrade_v8(conn, db_path)
    elif v == 7:
        _upgrade_v7(conn, db_path)
        _upgrade_v8(conn, db_path)
    elif v == 8:
        _upgrade_v8(conn, db_path)
    return SCHEMA_VERSION


def _upgrade_v2(conn, db_path):
    """v2 -> v3: tokens.revoked_ns is gone. Revoke deletes the row now, so a tombstone
    column would only ever hold NULL -- a legacy field kept for nothing."""
    snapshot(conn, f"{db_path}.from-v2-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    with tx(conn):
        conn.execute("DELETE FROM token_rooms WHERE token_id IN "
                     "(SELECT id FROM tokens WHERE revoked_ns IS NOT NULL)")
        conn.execute("UPDATE members SET token_id=NULL WHERE token_id IN "
                     "(SELECT id FROM tokens WHERE revoked_ns IS NOT NULL)")
        conn.execute("DELETE FROM tokens WHERE revoked_ns IS NOT NULL")  # already dead
        conn.execute("ALTER TABLE tokens DROP COLUMN revoked_ns")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _upgrade_v3(conn, db_path):
    """v3 -> v4: back-fill the files table from existing attachments.

    /files/<stored> is room-scoped now and refuses anything with no files row -- so every
    attachment that predates that table 404s, and its bytes are stranded on disk. The room
    is recoverable: an attachment belongs to a message, and the message names the room.
    """
    with tx(conn):
        n = conn.execute(
            "INSERT OR IGNORE INTO files(stored, room_id, uploaded_by, ts_ns) "
            "SELECT replace(a.url, rtrim(a.url, replace(a.url, '/', '')), ''), "
            "       m.room, m.sender, m.ts_ns "
            "  FROM attachments a JOIN messages m ON m.id = a.message_id").rowcount
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return n


def _upgrade_v4(conn, db_path):
    """v4 -> v5: full-text index over messages (DES-001 S1).

    Creates the external-content FTS table and backfills it from the existing log.
    The backfill is the whole point of the migration: a created-but-empty FTS table
    would make every historical message silently unsearchable -- the failure mode
    that LOOKS like a quiet bus."""
    with tx(conn):
        _exec_script(conn, """
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                subject, body,
                content='messages', content_rowid='id',
                tokenize="unicode61 tokenchars '-_'"
            )
        """)
        conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
        n = conn.execute("INSERT INTO messages_fts(rowid, subject, body) "
                         "SELECT id, subject, body FROM messages").rowcount
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return n


def _upgrade_v5(conn, db_path):
    """v5 -> v6: entity index over messages (DES-001 S2).

    Backfill runs the same extractor send() uses, over the whole log, in the
    migration's transaction -- a created-but-empty entity table would make
    entity= silently return nothing for all of history."""
    with tx(conn):
        _exec_script(conn, """
            CREATE TABLE IF NOT EXISTS message_entities (
                entity     TEXT NOT NULL,
                message_id INTEGER NOT NULL,
                PRIMARY KEY (entity, message_id)
            );
            CREATE INDEX IF NOT EXISTS idx_msgent_msg ON message_entities(message_id)
        """)
        conn.execute("DELETE FROM message_entities")
        rows = []
        for r in conn.execute("SELECT id, subject, body FROM messages"):
            rows += [(e, r["id"]) for e in
                     extract_entities(f"{r['subject']} {r['body']}")]
        conn.executemany(
            "INSERT OR IGNORE INTO message_entities(entity, message_id) VALUES(?,?)", rows)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return len(rows)


def _upgrade_v6(conn, db_path):
    """v6 -> v7: re-extract entities. The ID pattern generalized (ADR-only ->
    word-dash-number, found live: entity=des-001 was empty with the naming thread
    right there), and an extraction change without a re-extract would leave history
    indexed under the OLD rules -- two vocabularies pretending to be one index.
    Same body as v5: the backfill is already a delete-and-rebuild."""
    return _upgrade_v5(conn, db_path)


def _upgrade_v7(conn, db_path):
    """v7 -> v8: tokens gain a nullable bound agent name (per-agent tokens,
    DECISIONS open item 5, ruled in bus msg 8371). NULL keeps today's unbound
    behavior, so the fleet migrates token by token with no flag day."""
    snapshot(conn, f"{db_path}.pre-v8-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    with tx(conn):
        # Idempotent like every other step's IF NOT EXISTS: a chain replayed over a
        # db that already grew the column (fresh schema, or a re-run) must not die
        # on ALTER's lack of an IF NOT EXISTS clause.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
        if "agent_name" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN agent_name TEXT")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _upgrade_v8(conn, db_path):
    """v8 -> v9: the memories store (DES-001 S3) and the lessons fold-in.

    Lessons become memories rows (kind='lesson', structured columns ride along),
    then the lessons table is DROPPED -- clean cutover, one store, no dual path.
    lessons()/lesson_add() keep their exact signatures and return shape, rebacked.
    Destructive (a table dies), so it snapshots first like v0 and v2."""
    snapshot(conn, f"{db_path}.pre-v9-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
        if "mem_tier" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN mem_tier TEXT NOT NULL "
                         "DEFAULT 'state' CHECK (mem_tier IN ('state','write','ratify'))")
        _exec_script(conn, _MEMORIES_SCHEMA)
        if _table_exists(conn, "lessons"):
            for r in conn.execute("SELECT * FROM lessons ORDER BY created_ns"):
                _memory_insert(
                    conn, kind="lesson",
                    scope="global" if r["room_id"] is None else r["room_id"],
                    fact=r["rule"][:1000], author=r["author"], status="live",
                    slug=r["slug"], symptom=r["symptom"], root_cause=r["root_cause"],
                    rule=r["rule"], detection=r["detection"], created_ns=r["created_ns"])
            conn.execute("DROP TABLE lessons")
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def _upgrade_v0(conn, db_path):
    """v0 (room = the shared secret, stored as a string on every row) -> v2 (rooms are
    uuid rows owned by a user). Ownerless rooms are claimed by the first admin at
    /setup -- no user exists yet, so there is nobody to own them here."""
    snapshot(conn, f"{db_path}.from-v0-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    # AUTOINCREMENT lives in sqlite_sequence; DROP+RENAME resets it to max(id) of the
    # copied rows. Retract (delete_if_unseen) can leave max(id) BELOW the high-water
    # mark, so without restoring this the bus re-issues an id that already went out
    # on the wire. Capture before anything drops.
    row = conn.execute("SELECT seq FROM sqlite_sequence WHERE name='messages'").fetchone()
    seq = row["seq"] if row else 0
    conn.execute("PRAGMA foreign_keys=OFF")   # a no-op inside a tx, so it goes here
    try:
        with tx(conn):
            _exec_script(conn, _SCHEMA)        # adds the new tables; messages untouched
            now = time.time_ns()
            legacy = [r["room"] for r in conn.execute(
                "SELECT room FROM messages UNION SELECT room FROM agents")]
            room_map = {k: _uuid() for k in legacy}
            conn.executemany(
                "INSERT INTO rooms(id, name, owner_id, public, retention_ns, created_ns) "
                "VALUES(?,?,NULL,0,NULL,?)",
                [(rid, k or "open", now) for k, rid in room_map.items()])
            conn.execute("CREATE TEMP TABLE room_map(legacy TEXT PRIMARY KEY, room_id TEXT)")
            conn.executemany("INSERT INTO room_map VALUES(?,?)", list(room_map.items()))
            _exec_script(conn, """
                CREATE TABLE messages_new (
                    id        INTEGER PRIMARY KEY AUTOINCREMENT,
                    thread_id INTEGER,
                    parent_id INTEGER REFERENCES messages(id),
                    sender    TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    subject   TEXT NOT NULL DEFAULT '',
                    body      TEXT NOT NULL,
                    room      TEXT NOT NULL REFERENCES rooms(id),
                    ts_ns     INTEGER NOT NULL
                );
                INSERT INTO messages_new(id, thread_id, parent_id, sender, recipient,
                                         subject, body, room, ts_ns)
                  SELECT m.id, m.thread_id, m.parent_id, m.sender, m.recipient,
                         m.subject, m.body, rm.room_id, m.ts_ns
                    FROM messages m JOIN room_map rm ON rm.legacy = m.room;
                DROP TABLE messages;
                ALTER TABLE messages_new RENAME TO messages;
            """)
            conn.execute("UPDATE sqlite_sequence SET seq=? WHERE name='messages'", (seq,))
            # Presence is a 40-minute heartbeat cache, not a record: every agent
            # re-joins on its next turn and messages already carry the authorship.
            # Dropping beats porting -- and it discards the live corruption where
            # join()'s ON CONFLICT(name) yanked human-web out of its first room.
            conn.execute("DROP TABLE agents")
            _exec_script(conn, _SCHEMA)        # re-add indexes lost with the old table
            # The messages table was rebuilt (DROP + RENAME), so the external-content
            # FTS index is stale by definition -- rebuild it from the rows that exist
            # now. Any future rebuild-the-table migration owes this same step (DES-001).
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('delete-all')")
            conn.execute("INSERT INTO messages_fts(rowid, subject, body) "
                         "SELECT id, subject, body FROM messages")
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise BusError(f"migration left {len(bad)} FK violations")
            conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def snapshot(conn, path):
    """Consistent copy of the whole DB. Cheap insurance in front of every
    destructive op; with no audit log, this snapshot IS the undo."""
    conn.execute("VACUUM INTO ?", (path,))
    return path


def _is_live(seen_ns, now=None):
    return (now or time.time_ns()) - seen_ns < LIVE_TTL_NS


def _ph(items):
    return ",".join("?" * len(items))


# ---- passwords, tokens, sessions ---------------------------------------------

def hash_password(pw):
    if not pw or len(pw) < 8:
        raise BusError("password must be at least 8 characters")
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, **_SCRYPT)
    return f"scrypt${salt.hex()}${h.hex()}"


def verify_password(pw, stored):
    try:
        algo, salt_hex, hash_hex = (stored or "").split("$")
    except ValueError:
        return False
    if algo != "scrypt":
        return False
    h = hashlib.scrypt((pw or "").encode(), salt=bytes.fromhex(salt_hex), **_SCRYPT)
    return hmac.compare_digest(h.hex(), hash_hex)


def _sha(secret):
    return hashlib.sha256((secret or "").encode()).hexdigest()


# ---- users -------------------------------------------------------------------

def any_users(conn):
    """False only before first-run setup. Gates the bootstrap screen."""
    return conn.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None


def create_user(conn, name, password, role="user"):
    valid_name(name)
    if role not in ("admin", "user"):
        raise BusError(f"bad role {role!r}")
    pw_hash = hash_password(password)
    uid, now = _uuid(), time.time_ns()
    try:
        conn.execute(
            "INSERT INTO users(id, name, pw_hash, role, created_ns) VALUES(?,?,?,?,?)",
            (uid, name, pw_hash, role, now))
    except sqlite3.IntegrityError:
        raise BusError(f"user {name!r} already exists")
    return {"id": uid, "name": name, "role": role, "created_ns": now}


def setup_first_admin(conn, name, password):
    """First-run bootstrap: only ever succeeds while there are zero users, and
    adopts every ownerless room the migration left behind."""
    with tx(conn):
        if any_users(conn):
            raise BusError("setup already done")
        u = create_user(conn, name, password, role="admin")
        conn.execute("UPDATE rooms SET owner_id=? WHERE owner_id IS NULL", (u["id"],))
    return u


def authenticate(conn, name, password):
    r = conn.execute("SELECT * FROM users WHERE name=?", (name,)).fetchone()
    if not r or not verify_password(password, r["pw_hash"]):
        return None
    return {"id": r["id"], "name": r["name"], "role": r["role"]}


def set_password(conn, user_id, password):
    """Admin reset: no old password required -- that is the whole point of a reset, since
    the user has lost it. Every session of theirs dies, or a stolen session outlives the
    reset that was meant to end it."""
    pw_hash = hash_password(password)
    with tx(conn):
        n = conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, user_id)).rowcount
        if not n:
            raise BusError("no such user")
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def change_password(conn, user_id, old_password, new_password):
    """Self-service change. Verifying the OLD password is what stops a borrowed unlocked
    browser from taking the account outright. All sessions die; the caller gets a fresh
    one, so a change actually evicts anyone riding a stolen cookie."""
    r = conn.execute("SELECT pw_hash FROM users WHERE id=?", (user_id,)).fetchone()
    if not r:
        raise BusError("no such user")
    if not verify_password(old_password, r["pw_hash"]):
        raise AuthError("current password is wrong")
    pw_hash = hash_password(new_password)
    with tx(conn):
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (pw_hash, user_id))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))


def list_users(conn):
    return [{"id": r["id"], "name": r["name"], "role": r["role"],
             "created_ns": r["created_ns"]}
            for r in conn.execute("SELECT * FROM users ORDER BY name")]


def set_role(conn, user_id, role):
    if role not in ("admin", "user"):
        raise BusError(f"bad role {role!r}")
    # Guard INSIDE the transaction: BEGIN IMMEDIATE takes the write lock first, so two
    # concurrent demotions cannot both read "2 admins" and both proceed to zero.
    with tx(conn):
        if role == "user" and _admin_count(conn) == 1 and _is_admin(conn, user_id):
            raise BusError("cannot demote the last admin")
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))


def _admin_count(conn):
    return conn.execute("SELECT count(*) c FROM users WHERE role='admin'").fetchone()["c"]


def _is_admin(conn, user_id):
    r = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(r and r["role"] == "admin")


def delete_user(conn, user_id):
    """Drop a user, their sessions and tokens. Their ROOMS survive as ownerless --
    deleting a user must not silently take a room's whole message history with it;
    purge_room is the explicit way to do that."""
    with tx(conn):
        # Guard INSIDE the transaction. Read outside it and two admins deleting each
        # other at once both see "2 admins", both proceed, and the database is left with
        # ZERO admins -- unrecoverable, since only an admin can make an admin.
        # BEGIN IMMEDIATE serializes them: the loser re-reads 1 and is refused.
        if _is_admin(conn, user_id) and _admin_count(conn) == 1:
            raise BusError("cannot delete the last admin")
        conn.execute(
            "DELETE FROM token_rooms WHERE token_id IN (SELECT id FROM tokens WHERE owner_id=?)",
            (user_id,))
        # members.token_id REFERENCES tokens(id): orphan the memberships BEFORE dropping
        # the tokens, or any agent still joined under one makes this a FK violation and
        # the user can never be deleted at all. The membership row itself is presence and
        # reaps on its own; only the credential link dies here.
        conn.execute(
            "UPDATE members SET token_id=NULL WHERE token_id IN "
            "(SELECT id FROM tokens WHERE owner_id=?)", (user_id,))
        conn.execute("DELETE FROM tokens WHERE owner_id=?", (user_id,))
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
        conn.execute("UPDATE rooms SET owner_id=NULL WHERE owner_id=?", (user_id,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))


# ---- sessions ----------------------------------------------------------------

def create_session(conn, user_id):
    secret = secrets.token_urlsafe(32)
    now = time.time_ns()
    conn.execute(
        "INSERT INTO sessions(id_hash, user_id, created_ns, expires_ns) VALUES(?,?,?,?)",
        (_sha(secret), user_id, now, now + SESSION_TTL_NS))
    return secret


def resolve_session(conn, secret):
    if not secret:
        return None
    r = conn.execute(
        "SELECT s.expires_ns, u.id, u.name, u.role FROM sessions s "
        "JOIN users u ON u.id=s.user_id WHERE s.id_hash=?", (_sha(secret),)).fetchone()
    if not r:
        return None
    if r["expires_ns"] < time.time_ns():
        conn.execute("DELETE FROM sessions WHERE id_hash=?", (_sha(secret),))
        return None
    return {"id": r["id"], "name": r["name"], "role": r["role"]}


def delete_session(conn, secret):
    conn.execute("DELETE FROM sessions WHERE id_hash=?", (_sha(secret),))


# ---- tokens ------------------------------------------------------------------

def create_token(conn, owner_id, label="", agent_name=None, mem_tier="state"):
    """Mint a token. The secret is returned ONCE and never stored -- only its hash.
    Rooms are assigned afterwards (assign_room), never baked into the secret.

    agent_name binds the credential to one bus identity at mint, immutably: a
    presented X-Agent that disagrees is a 401, and rebinding is a new token (revoke
    the old one -- revoke-is-delete is already instant). None mints an unbound
    token with today's self-asserted-name behavior."""
    agent_name = (agent_name or "").strip() or None
    if agent_name:
        valid_name(agent_name)
    if mem_tier not in TIERS:
        raise BusError(f"bad mem_tier {mem_tier!r}: one of {TIERS}")
    secret = secrets.token_urlsafe(32)
    tid, now = _uuid(), time.time_ns()
    conn.execute(
        "INSERT INTO tokens(id, secret_hash, owner_id, label, agent_name, mem_tier, "
        "created_ns) VALUES(?,?,?,?,?,?,?)",
        (tid, _sha(secret), owner_id, label, agent_name, mem_tier, now))
    return {"id": tid, "secret": secret, "label": label, "agent_name": agent_name,
            "mem_tier": mem_tier, "created_ns": now}


def resolve_token(conn, secret):
    """Token row for a presented secret, or None. A revoked token is DELETED, so it
    resolves to None here -- which is what makes revocation instant."""
    if not secret:
        return None
    r = conn.execute("SELECT * FROM tokens WHERE secret_hash=?",
                     (_sha(secret),)).fetchone()
    if not r:
        return None
    conn.execute("UPDATE tokens SET last_used_ns=? WHERE id=?", (time.time_ns(), r["id"]))
    return {"id": r["id"], "owner_id": r["owner_id"], "label": r["label"],
            "agent_name": r["agent_name"], "mem_tier": r["mem_tier"]}


def list_tokens(conn, owner_id):
    out = []
    for r in conn.execute(
            "SELECT * FROM tokens WHERE owner_id=? ORDER BY created_ns", (owner_id,)):
        out.append({"id": r["id"], "label": r["label"], "agent_name": r["agent_name"],
                    "created_ns": r["created_ns"], "last_used_ns": r["last_used_ns"],
                    "rooms": rooms_for_token(conn, r["id"])})
    return out


def revoke_token(conn, token_id, owner_id):
    """Revoke = DELETE the token, not tombstone it.

    A soft-revoke would need an audit log to be worth anything, and there isn't one by
    design; the secret is already unrecoverable, so a revoked row carries no information
    and just accumulates forever -- the exact sprawl purge exists to kill. resolve_token()
    returns None for a missing row just as it did for a revoked one, so revocation stays
    instant either way.
    """
    with tx(conn):
        r = conn.execute("SELECT owner_id FROM tokens WHERE id=?", (token_id,)).fetchone()
        if not r:
            raise BusError("no such token")
        if r["owner_id"] != owner_id:
            raise AccessError("not your token")
        # members.token_id REFERENCES tokens(id): orphan the memberships first or any
        # agent still joined under this token turns the delete into a FK violation.
        conn.execute("UPDATE members SET token_id=NULL WHERE token_id=?", (token_id,))
        conn.execute("DELETE FROM token_rooms WHERE token_id=?", (token_id,))
        conn.execute("DELETE FROM tokens WHERE id=?", (token_id,))


def assign_room(conn, token_id, room_id, actor_id):
    """Put a room on a token. Allowed if the actor owns the room, or the room is
    public. Public is what lets other users' tokens join it."""
    room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        raise BusError("no such room")
    tok = conn.execute("SELECT owner_id FROM tokens WHERE id=?", (token_id,)).fetchone()
    if not tok or tok["owner_id"] != actor_id:
        raise AccessError("not your token")
    if room["owner_id"] != actor_id and not room["public"]:
        raise AccessError("room is private")
    conn.execute("INSERT OR IGNORE INTO token_rooms(token_id, room_id) VALUES(?,?)",
                 (token_id, room_id))


def unassign_room(conn, token_id, room_id, actor_id):
    tok = conn.execute("SELECT owner_id FROM tokens WHERE id=?", (token_id,)).fetchone()
    if not tok or tok["owner_id"] != actor_id:
        raise AccessError("not your token")
    conn.execute("DELETE FROM token_rooms WHERE token_id=? AND room_id=?", (token_id, room_id))


def rooms_for_token(conn, token_id):
    """{room_id: room_name} for a token. Read per request -- never cached, so a
    revoke or flip-to-private lands on the very next call."""
    return {r["id"]: r["name"] for r in conn.execute(
        "SELECT ro.id, ro.name FROM token_rooms tr JOIN rooms ro ON ro.id=tr.room_id "
        "WHERE tr.token_id=? ORDER BY ro.name", (token_id,))}


# ---- rooms -------------------------------------------------------------------

def create_room(conn, owner_id, name, public=False):
    valid_room_name(name)
    rid, now = _uuid(), time.time_ns()
    try:
        conn.execute(
            "INSERT INTO rooms(id, name, owner_id, public, created_ns) VALUES(?,?,?,?,?)",
            (rid, name, owner_id, 1 if public else 0, now))
    except sqlite3.IntegrityError:
        raise BusError(f"you already own a room named {name!r}")
    return {"id": rid, "name": name, "owner_id": owner_id, "public": bool(public),
            "retention_ns": None, "created_ns": now}


def _room_dict(r, owner_name=None):
    return {"id": r["id"], "name": r["name"], "owner_id": r["owner_id"],
            "owner": owner_name, "public": bool(r["public"]),
            "retention_ns": r["retention_ns"], "created_ns": r["created_ns"]}


def get_room(conn, room_id):
    r = conn.execute(
        "SELECT ro.*, u.name AS owner_name FROM rooms ro "
        "LEFT JOIN users u ON u.id=ro.owner_id WHERE ro.id=?", (room_id,)).fetchone()
    return _room_dict(r, r["owner_name"]) if r else None


def list_rooms(conn, owner_id):
    return [_room_dict(r, r["owner_name"]) for r in conn.execute(
        "SELECT ro.*, u.name AS owner_name FROM rooms ro "
        "LEFT JOIN users u ON u.id=ro.owner_id WHERE ro.owner_id=? ORDER BY ro.name",
        (owner_id,))]


def public_rooms(conn, exclude_owner=None):
    """Public rooms for the picker. Each carries its owner so the UI can render the
    "owner -> room" pairing that makes per-owner room names unambiguous."""
    rows = conn.execute(
        "SELECT ro.*, u.name AS owner_name FROM rooms ro "
        "LEFT JOIN users u ON u.id=ro.owner_id WHERE ro.public=1 "
        "ORDER BY u.name, ro.name")
    return [_room_dict(r, r["owner_name"]) for r in rows
            if not exclude_owner or r["owner_id"] != exclude_owner]


def rename_room(conn, room_id, owner_id, name):
    valid_room_name(name)
    r = conn.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise BusError("no such room")
    if r["owner_id"] != owner_id:
        raise AccessError("not your room")
    try:
        conn.execute("UPDATE rooms SET name=? WHERE id=?", (name, room_id))
    except sqlite3.IntegrityError:
        raise BusError(f"you already own a room named {name!r}")


def set_public(conn, room_id, owner_id, public):
    """Flip a room's visibility. Going private REVOKES it from every other user's
    tokens; the owner's own keep it. Their past MESSAGES stay -- authorship is
    history, and deleting it would silently rewrite threads others are reading."""
    r = conn.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise BusError("no such room")
    if r["owner_id"] != owner_id:
        raise AccessError("not your room")
    with tx(conn):
        conn.execute("UPDATE rooms SET public=? WHERE id=?", (1 if public else 0, room_id))
        if not public:
            conn.execute(
                "DELETE FROM token_rooms WHERE room_id=? AND token_id IN "
                "(SELECT id FROM tokens WHERE owner_id != ?)", (room_id, owner_id))


def set_retention(conn, room_id, owner_id, retention_ns):
    """Per-room TTL. NULL = keep forever (the default)."""
    r = conn.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise BusError("no such room")
    if r["owner_id"] != owner_id:
        raise AccessError("not your room")
    conn.execute("UPDATE rooms SET retention_ns=? WHERE id=?", (retention_ns, room_id))


def _delete_messages(conn, ids):
    """Drop messages and everything referencing them. Caller holds the transaction."""
    if not ids:
        return
    ids = list(ids)
    ph = _ph(ids)
    # FTS delete-sync FIRST, while the rows still exist: the 'delete' command must
    # carry exactly the values the index holds (FTS5 manual, External Content Tables).
    # Reading after the DELETE would feed it nothing and silently corrupt the index.
    doomed = conn.execute(
        f"SELECT id, subject, body FROM messages WHERE id IN ({ph})", ids).fetchall()
    conn.executemany(
        "INSERT INTO messages_fts(messages_fts, rowid, subject, body) "
        "VALUES('delete',?,?,?)",
        [(r["id"], r["subject"], r["body"]) for r in doomed])
    conn.execute(f"DELETE FROM message_entities WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM attachments WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM reads WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM links WHERE parent_id IN ({ph}) OR child_id IN ({ph})", ids + ids)
    conn.execute(f"DELETE FROM messages WHERE id IN ({ph})", ids)


def purge_room(conn, room_id, owner_id):
    """Erase a room and everything in it. Snapshot first -- there is no undo."""
    r = conn.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise BusError("no such room")
    if r["owner_id"] != owner_id:
        raise AccessError("not your room")
    with tx(conn):
        ids = [x["id"] for x in conn.execute("SELECT id FROM messages WHERE room=?", (room_id,))]
        _delete_messages(conn, ids)
        conn.execute("DELETE FROM files WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM members WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM token_rooms WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM rooms WHERE id=?", (room_id,))
    return len(ids)


def sweep_retention(conn):
    """Drop whole THREADS past their room's TTL. Rooms with retention_ns NULL (the
    default) keep everything forever.

    Threads, not messages: retention must not shred a live conversation (a thread
    active yesterday keeps its month-old root), and per-message deletion would strand
    surviving replies pointing at a deleted parent. A thread expires when its NEWEST
    message is past the cutoff.
    """
    now, dropped = time.time_ns(), 0
    for r in conn.execute("SELECT id, retention_ns FROM rooms WHERE retention_ns IS NOT NULL"):
        cutoff = now - r["retention_ns"]
        ids = [x["id"] for x in conn.execute(
            "SELECT id FROM messages WHERE room=? AND thread_id IN ("
            "  SELECT thread_id FROM messages WHERE room=? "
            "  GROUP BY thread_id HAVING MAX(ts_ns) < ?)",
            (r["id"], r["id"], cutoff))]
        if ids:
            with tx(conn):
                _delete_messages(conn, ids)
            dropped += len(ids)
    return dropped


def sweep_sessions(conn):
    """Drop expired web sessions."""
    return conn.execute("DELETE FROM sessions WHERE expires_ns < ?",
                        (time.time_ns(),)).rowcount


# ---- membership / presence ---------------------------------------------------

def _member(conn, room_id, name):
    return conn.execute("SELECT * FROM members WHERE room_id=? AND name=?",
                        (room_id, name)).fetchone()


def join(conn, name, tag, room_id, token_id=None, fresh=False, url=None):
    """Sign up under `name` in one room. Fails if a *live* agent holds that name in
    THIS room under a different tag -- names are per-room now, so the same name in
    another room is not a collision. Replays only the last CATCHUP_NS of the room's
    backlog; fresh=True skips it."""
    valid_name(name)
    now = time.time_ns()
    cur = _member(conn, room_id, name)
    if cur and cur["tag"] != tag and _is_live(cur["seen_ns"], now):
        raise BusError(f"name {name!r} is held by a live agent (tag {cur['tag']}). pick another.")
    conn.execute(
        "INSERT INTO members(room_id, name, tag, url, token_id, joined_ns, seen_ns) "
        "VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(room_id, name) DO UPDATE SET tag=excluded.tag, url=excluded.url, "
        "token_id=excluded.token_id, seen_ns=excluded.seen_ns",
        (room_id, name, tag, url, token_id, now, now))
    # Mark everything outside the catch-up window already-read: default joiners see
    # only recent traffic; fresh joiners start clean. history(since=...) recalls more.
    cutoff = now if fresh else now - CATCHUP_NS
    conn.execute(
        "INSERT OR IGNORE INTO reads(message_id, agent, read_ns) "
        "SELECT id, ?, ? FROM messages WHERE sender != ? AND ts_ns < ? AND room = ?",
        (name, now, name, cutoff, room_id))
    return name


def touch(conn, name, rooms):
    """Heartbeat across every room the agent is a member of."""
    valid_name(name)
    if not rooms:
        return
    rooms = list(rooms)
    conn.execute(
        f"UPDATE members SET seen_ns=? WHERE name=? AND room_id IN ({_ph(rooms)})",
        [time.time_ns(), name] + rooms)


def leave(conn, name, rooms):
    """Sign off. Membership only -- messages are never touched."""
    if not rooms:
        return
    rooms = list(rooms)
    conn.execute(f"DELETE FROM members WHERE name=? AND room_id IN ({_ph(rooms)})",
                 [name] + rooms)


def whoami(conn, tag):
    if not tag:
        return None
    r = conn.execute(
        "SELECT name FROM members WHERE tag=? ORDER BY seen_ns DESC LIMIT 1", (tag,)
    ).fetchone()
    return r["name"] if r else None


def presence(conn, rooms):
    """Everyone across the caller's rooms. Each entry carries its room: names are
    per-room now, so a flat list would be ambiguous."""
    if not rooms:
        return []
    rooms = list(rooms)
    now = time.time_ns()
    return [
        {"name": r["name"], "tag": r["tag"], "url": r["url"], "room": r["room_id"],
         "room_name": r["room_name"], "token_id": r["token_id"],
         "live": _is_live(r["seen_ns"], now),
         "seen_ns": r["seen_ns"], "joined_ns": r["joined_ns"]}
        for r in conn.execute(
            f"SELECT m.*, ro.name AS room_name FROM members m JOIN rooms ro ON ro.id=m.room_id "
            f"WHERE m.room_id IN ({_ph(rooms)}) ORDER BY m.name", rooms)
    ]


def reap_stale(conn):
    """Drop members whose heartbeat has gone stale. Named away from prune_agent on
    purpose: this reaps presence, that erases a trace. Two very different verbs."""
    now = time.time_ns()
    dead = [(r["room_id"], r["name"]) for r in
            conn.execute("SELECT room_id, name, seen_ns FROM members")
            if not _is_live(r["seen_ns"], now)]
    for room_id, name in dead:
        conn.execute("DELETE FROM members WHERE room_id=? AND name=?", (room_id, name))
    return [n for _, n in dead]


def known(conn, name, rooms):
    if not rooms:
        return False
    rooms = list(rooms)
    return conn.execute(
        f"SELECT 1 FROM members WHERE name=? AND room_id IN ({_ph(rooms)})",
        [name] + rooms).fetchone() is not None


# ---- messages ----------------------------------------------------------------

def _wake_targets(conn, sender, recipient, room_id):
    if recipient != BROADCAST:
        return [recipient]
    now = time.time_ns()
    return [r["name"] for r in conn.execute(
                "SELECT name, seen_ns FROM members WHERE room_id=?", (room_id,))
            if r["name"] != sender and _is_live(r["seen_ns"], now)]


def resolve_send_room(rooms, room=None, parent_room=None):
    """Which room a send lands in.

    A reply always lands in its parent's room -- inferred, never taken from the
    caller, so a stale token cannot re-route a reply into a room it has lost.
    For a new thread: one room in reach is used implicitly (the common case stays
    friction-free); 2+ and no explicit room raises rather than guessing, because a
    wrong guess discloses the message to the wrong room and cannot be undone.
    """
    if parent_room is not None:
        if room and room != parent_room:
            raise BusError("a reply lands in its parent's room; drop the room argument")
        return parent_room
    if room:
        if room not in rooms:
            raise AccessError(f"no access to room {room}")
        return room
    if len(rooms) == 1:
        return next(iter(rooms))
    if not rooms:
        raise AccessError("this token has no rooms")
    raise AmbiguousRoom([{"id": r, "name": n} for r, n in rooms.items()])


def send(conn, sender, recipient, body, subject="", reply_to=None, attachments=None,
         room=None):
    """Insert a message. recipient='*' broadcasts.

    reply_to threads this under a parent. Pass one id for a normal reply, or a
    list of ids to re-link/merge several branches (the message gets many parents).
    The first id is the PRIMARY parent: it sets parent_id and which thread this
    joins. Every id becomes an edge in `links`, so the full fork/merge web is kept.

    Every parent must live in the same room. Cross-room replies are refused: that
    edge would let trace()/graph() carry one room's content into another. Moving
    knowledge between rooms is a NEW root message in the target room.

    attachments: optional list of {"url", "name", "bytes"} dicts (1-n per message);
    the file bytes themselves live on disk under the broker's files/ dir.

    Returns {id, thread_id, parents, wake:[names]}.
    """
    valid_name(sender)
    if not room:
        raise BusError("room is required")
    if recipient != BROADCAST:
        valid_name(recipient)
        if not _member(conn, room, recipient):
            raise BusError(f"no such agent in this room: {recipient!r} (is it joined?)")
    parents = [] if reply_to is None else ([reply_to] if isinstance(reply_to, int) else list(reply_to))
    thread_id, parent_id = None, None
    if parents:
        found = {r["id"]: r for r in conn.execute(
            f"SELECT id, thread_id, room FROM messages WHERE id IN ({_ph(parents)})", parents)}
        for p in parents:
            if p not in found or found[p]["room"] != room:
                raise BusError(f"reply_to {p}: no such message in this room")
        parent_id = parents[0]                      # primary parent
        thread_id = found[parents[0]]["thread_id"]  # joins the primary parent's thread
    now = time.time_ns()
    with tx(conn):
        cur = conn.execute(
            "INSERT INTO messages(thread_id, parent_id, sender, recipient, subject, body, room, ts_ns) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (thread_id, parent_id, sender, recipient, subject, body, room, now))
        mid = cur.lastrowid
        # Manual FTS sync (DES-001 S1): external-content tables index nothing on their
        # own; every insert here must be mirrored or the message is unsearchable.
        conn.execute("INSERT INTO messages_fts(rowid, subject, body) VALUES(?,?,?)",
                     (mid, subject, body))
        conn.executemany(
            "INSERT OR IGNORE INTO message_entities(entity, message_id) VALUES(?,?)",
            [(e, mid) for e in extract_entities(f"{subject} {body}")])
        if thread_id is None:  # new thread roots on its own id
            conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
            thread_id = mid
        for p in parents:
            conn.execute("INSERT OR IGNORE INTO links(parent_id, child_id) VALUES(?,?)", (p, mid))
        for a in attachments or []:
            conn.execute("INSERT INTO attachments(message_id, url, name, bytes) VALUES(?,?,?,?)",
                         (mid, a["url"], a.get("name"), a.get("bytes")))
    return {"id": mid, "thread_id": thread_id, "parents": parents,
            "wake": _wake_targets(conn, sender, recipient, room)}


def _msg(r):
    m = {"id": r["id"], "thread_id": r["thread_id"], "parent_id": r["parent_id"],
         "from": r["sender"], "to": r["recipient"], "subject": r["subject"],
         "body": r["body"], "room": r["room"], "ts_ns": r["ts_ns"]}
    with contextlib.suppress(IndexError, KeyError):
        m["room_name"] = r["room_name"]
    return m


_SEL = "SELECT m.*, ro.name AS room_name FROM messages m JOIN rooms ro ON ro.id=m.room"


def _with_attachments(conn, msgs):
    """Stamp each message dict with its attachments list (batch, one query)."""
    if not msgs:
        return msgs
    ids = [m["id"] for m in msgs]
    by = {}
    for r in conn.execute(
            f"SELECT message_id, url, name, bytes FROM attachments "
            f"WHERE message_id IN ({_ph(ids)}) ORDER BY id", ids):
        by.setdefault(r["message_id"], []).append(
            {"url": r["url"], "name": r["name"], "bytes": r["bytes"]})
    for m in msgs:
        m["attachments"] = by.get(m["id"], [])
    return msgs


def inbox(conn, agent, rooms):
    """Unread messages addressed to `agent` (direct or broadcast) across ALL of the
    caller's rooms, oldest first. Each message carries its room, which is what lets
    an agent reply into the room a message came from."""
    valid_name(agent)
    if not rooms:
        return []
    rooms = list(rooms)
    rows = conn.execute(
        f"{_SEL} WHERE m.room IN ({_ph(rooms)}) AND (m.recipient=? OR m.recipient=?) "
        f"AND m.sender!=? "
        f"AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.agent=?) "
        f"ORDER BY m.id",
        rooms + [agent, BROADCAST, agent, agent]).fetchall()
    return _with_attachments(conn, [_msg(r) for r in rows])


def thread(conn, thread_id, rooms):
    if not rooms:
        return []
    rooms = list(rooms)
    rows = conn.execute(
        f"{_SEL} WHERE m.thread_id=? AND m.room IN ({_ph(rooms)}) ORDER BY m.id",
        [thread_id] + rooms).fetchall()
    return _with_attachments(conn, [_msg(r) for r in rows])


def tail(conn, since_id=0, limit=200, rooms=()):
    """Feed backlog: messages with id > since_id, oldest-first (or the most recent
    `limit` when since_id=0). The web feed uses this to fill reconnect gaps."""
    if not rooms:
        return []
    rooms = list(rooms)
    limit = max(1, min(int(limit), 1000))
    if since_id:
        rows = conn.execute(
            f"{_SEL} WHERE m.room IN ({_ph(rooms)}) AND m.id>? ORDER BY m.id LIMIT ?",
            rooms + [since_id, limit]).fetchall()
        return _with_attachments(conn, [_msg(r) for r in rows])
    rows = conn.execute(
        f"{_SEL} WHERE m.room IN ({_ph(rooms)}) ORDER BY m.id DESC LIMIT ?",
        rooms + [limit]).fetchall()
    return _with_attachments(conn, [_msg(r) for r in reversed(rows)])


def search(conn, *, keywords=None, since_ns=None, until_ns=None,
           involves=None, mine_agent=None, thread_id=None, limit=200, rooms=(),
           entity=None):
    """Search the message log (read or not) across the caller's rooms. Every filter
    ANDs together:
      keywords   list of words; a message matches if ANY word appears as a
                 case-insensitive TOKEN in subject OR body (FTS5, unicode61 with
                 tokenchars '-_': ADR-061 and run_id are single tokens). A word*
                 prefix query reaches right-extended compounds (run_id* finds
                 run_id_batch) -- left-fused ones (disposal_run_id) need their own
                 prefix; the S2 entities index owns that class. This replaced
                 substring match in 0.2.5: 'eboot' no longer matches "reboot"
      since_ns   ts_ns >= since_ns        until_ns  ts_ns <= until_ns
      involves   agent is sender OR recipient (the full convo touching that agent)
      mine_agent agent is sender OR recipient (the caller's own slice)
      thread_id  restrict to one thread
      entity     only messages whose text carries this extracted entity (ADR-061,
                 #263, RunStatus, run_id, disposal_run_id, repo names, proto-vX.Y.Z);
                 case-insensitive, matches the identifier class exactly -- this is
                 the recovery path for compounds FTS tokenization fuses
    involves + mine_agent together => the 1:1 conversation between the two.
    Returns the most recent <=limit matches. Without keywords: oldest-first. With
    keywords: ranked best-first by bm25, ties oldest-first. Each carries thread_id so
    the caller can expand the DAG with thread()/graph()/trace().
    """
    if not rooms:
        return []
    rooms = list(rooms)
    where, args = [f"m.room IN ({_ph(rooms)})"], list(rooms)
    join = ""
    if keywords:
        # Every keyword is quoted, ALWAYS (DES-001 R1-M1): bare fleet vocabulary
        # (ADR-061, NEED:) collides with the FTS5 query grammar where -, ", : and
        # NOT/OR/AND are operators -- unescaped, that is a syntax error, not a search.
        # Quote-and-double-internal-quotes is quote_fts() from sqlite-utils, cribbed
        # not imported. A trailing * survives quoting as a prefix query ("run_id"*).
        terms = []
        for k in keywords:
            prefix = k.endswith("*") and len(k) > 1
            base = k[:-1] if prefix else k
            terms.append('"' + base.replace('"', '""') + '"' + ("*" if prefix else ""))
        join = " JOIN messages_fts f ON f.rowid = m.id"
        where.append("messages_fts MATCH ?")
        args.append(" OR ".join(terms))
    if since_ns is not None:
        where.append("m.ts_ns >= ?")
        args.append(since_ns)
    if until_ns is not None:
        where.append("m.ts_ns <= ?")
        args.append(until_ns)
    if involves:
        where.append("(m.sender=? OR m.recipient=?)")
        args += [involves, involves]
    if mine_agent:
        where.append("(m.sender=? OR m.recipient=?)")
        args += [mine_agent, mine_agent]
    if thread_id:
        where.append("m.thread_id=?")
        args.append(thread_id)
    if entity:
        # Same normal form as extraction: lowercase. entity="RunStatus" and
        # entity="runstatus" are one key, exactly like the index itself.
        where.append("m.id IN (SELECT message_id FROM message_entities WHERE entity=?)")
        args.append(entity.lower())
    clause = " WHERE " + " AND ".join(where)
    limit = max(1, min(int(limit), 1000))
    sel = _SEL.replace("SELECT m.*", "SELECT m.*, f.rank AS _rank", 1) if keywords else _SEL
    rows = conn.execute(
        f"{sel}{join}{clause} ORDER BY m.id DESC LIMIT ?", args + [limit]).fetchall()
    msgs = _with_attachments(conn, [_msg(r) for r in reversed(rows)])
    if keywords:
        # ponytail: rank the most recent <=limit matches, not the whole log; raise
        # limit if a search needs deeper reach. FTS5 rank IS bm25 here: negative,
        # smaller = better (sign pinned in DES-001 so nobody re-derives it).
        by_rank = {r["id"]: r["_rank"] for r in rows}
        msgs.sort(key=lambda m: by_rank[m["id"]])  # stable: ties stay oldest-first
    return msgs


def _subgraph(conn, ids, rooms):
    """{messages, edges} for a set of message ids -- edges = links with both
    endpoints inside the set. messages sorted by id; each carries its parents.
    Filtered to `rooms`: the walk never leaves the caller's rooms even if a
    cross-room edge somehow exists."""
    if not ids or not rooms:
        return {"messages": [], "edges": []}
    ids, rooms = list(ids), list(rooms)
    iph, rph = _ph(ids), _ph(rooms)
    edges = [[r["parent_id"], r["child_id"]] for r in conn.execute(
        f"SELECT l.parent_id, l.child_id FROM links l "
        f"JOIN messages p ON p.id=l.parent_id JOIN messages c ON c.id=l.child_id "
        f"WHERE l.parent_id IN ({iph}) AND l.child_id IN ({iph}) "
        f"AND p.room IN ({rph}) AND c.room IN ({rph}) ORDER BY l.child_id, l.parent_id",
        ids + ids + rooms + rooms)]
    parents = {}
    for p, c in edges:
        parents.setdefault(c, []).append(p)
    msgs = []
    for r in conn.execute(
            f"{_SEL} WHERE m.id IN ({iph}) AND m.room IN ({rph}) ORDER BY m.id",
            ids + rooms):
        m = _msg(r)
        m["parents"] = parents.get(r["id"], [])
        msgs.append(m)
    return {"messages": _with_attachments(conn, msgs), "edges": edges}


def trace(conn, message_id, rooms):
    """Ancestor sub-DAG: every message that led to `message_id`, with the edges
    among them -- so you can track back exactly how we got here, including any
    forks and re-links upstream. Includes the node itself. Every hop is filtered to
    the caller's rooms: send() refuses cross-room replies, but this walk does not
    depend on that invariant holding elsewhere."""
    if not rooms:
        raise BusError(f"no such message in this room: {message_id}")
    rooms = list(rooms)
    if not conn.execute(
            f"SELECT 1 FROM messages WHERE id=? AND room IN ({_ph(rooms)})",
            [message_id] + rooms).fetchone():
        raise BusError(f"no such message in this room: {message_id}")
    seen, frontier = set(), [message_id]
    while frontier:
        nid = frontier.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for r in conn.execute(
                f"SELECT l.parent_id FROM links l JOIN messages p ON p.id=l.parent_id "
                f"WHERE l.child_id=? AND p.room IN ({_ph(rooms)})", [nid] + rooms):
            frontier.append(r["parent_id"])
    return _subgraph(conn, seen, rooms)


def graph(conn, thread_id, rooms):
    """The whole web of a thread: every message in it plus all fork/merge edges.
    Use this to render or walk the full conversation tree."""
    if not rooms:
        return {"messages": [], "edges": []}
    rooms = list(rooms)
    ids = [r["id"] for r in conn.execute(
        f"SELECT id FROM messages WHERE thread_id=? AND room IN ({_ph(rooms)})",
        [thread_id] + rooms)]
    return _subgraph(conn, set(ids), rooms)


def readers(conn, message_id, exclude=None):
    """Agents that have read (acked or auto-caught-up) a message, sorted.
    exclude drops one name -- a sender's own read never counts as being seen."""
    return sorted(r["agent"] for r in conn.execute(
        "SELECT agent FROM reads WHERE message_id=? AND agent!=?",
        (message_id, exclude or "")))


def delete_if_unseen(conn, message_id, sender, rooms):
    """Retract a message nobody has consumed yet: sender-only, and refused the moment
    any reads row or reply edge references it. Used by the web UI to pull back a
    mistaken broadcast before anyone has read it."""
    if not rooms:
        raise BusError(f"no such message in this room: {message_id}")
    rooms = list(rooms)
    with tx(conn):
        r = conn.execute(
            f"SELECT sender FROM messages WHERE id=? AND room IN ({_ph(rooms)})",
            [message_id] + rooms).fetchone()
        if not r:
            raise BusError(f"no such message in this room: {message_id}")
        if r["sender"] != sender:
            raise BusError("not your message")
        if conn.execute("SELECT 1 FROM reads WHERE message_id=? AND agent!=?",
                        (message_id, sender)).fetchone():
            raise BusError("already read by someone -- cannot retract")
        if conn.execute("SELECT 1 FROM links WHERE parent_id=?", (message_id,)).fetchone():
            raise BusError("already replied to -- cannot retract")
        _delete_messages(conn, [message_id])


def ack(conn, agent, message_ids, rooms):
    """Mark messages read for `agent`. Idempotent. Ids outside the caller's rooms,
    or not addressed to it, are IGNORED rather than raising -- an ack is a batch and
    one stale id must not fail the rest. Returns {acked, ignored}."""
    valid_name(agent)
    ids = [int(m) for m in message_ids]
    if not ids or not rooms:
        return {"acked": 0, "ignored": ids}
    rooms = list(rooms)
    ok = {r["id"] for r in conn.execute(
        f"SELECT id FROM messages WHERE id IN ({_ph(ids)}) AND room IN ({_ph(rooms)}) "
        f"AND (recipient=? OR recipient=?)",
        ids + rooms + [agent, BROADCAST])}
    now = time.time_ns()
    conn.executemany(
        "INSERT OR IGNORE INTO reads(message_id, agent, read_ns) VALUES(?,?,?)",
        [(mid, agent, now) for mid in ok])
    return {"acked": len(ok), "ignored": [i for i in ids if i not in ok]}


# ---- prune an agent ----------------------------------------------------------

def _rethread(conn, root_id):
    """Stamp thread_id=root_id on root_id and every PRIMARY-parent descendant.

    Walks parent_id, NOT links. thread_id is inherited from the primary parent only,
    so the primary-parent relation is the thread spine -- and being a forest, this
    terminates. links is the full DAG and its non-primary edges legitimately cross
    threads (108 do in the live DB); walking those would re-thread whole unrelated
    conversations. Needs idx_msg_parent to stay cheap.
    """
    seen, frontier = set(), [root_id]
    while frontier:
        n = frontier.pop()
        if n in seen:
            continue
        seen.add(n)
        conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (root_id, n))
        frontier += [r["id"] for r in
                     conn.execute("SELECT id FROM messages WHERE parent_id=?", (n,))]


def prune_agent(conn, name, room_id):
    """Erase an agent from a room: its membership and every message to or from it.

    Survivors that replied to a deleted message are REPARENTED to their thread root
    rather than cascade-deleted, so other agents' work is not collateral. Both the
    `links` edge and the denormalized `parent_id` are rewritten -- trace()/graph()
    read `links` exclusively, so fixing only parent_id would leave the walks
    pointing at a deleted node. If the thread root was itself the pruned agent's,
    the survivor becomes a new root and its descendants' thread_id is re-stamped
    (thread_id is copied at insert, never derived, so it does not follow on its own).
    """
    valid_name(name)
    with tx(conn):
        # recipient=name matches literally, never '*' (NAME_RE forbids it), so
        # broadcasts he RECEIVED survive -- deleting those would erase everyone's mail.
        # Broadcasts he SENT go, via sender=name: those are his trace.
        doomed = [r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE room=? AND (sender=? OR recipient=?)",
            (room_id, name, name))]
        dset, new_roots = set(doomed), []
        if doomed:
            # Survivors whose PRIMARY parent dies. Repair BEFORE the delete.
            for s in conn.execute(
                    f"SELECT id, thread_id FROM messages "
                    f"WHERE parent_id IN ({_ph(doomed)}) AND id NOT IN ({_ph(doomed)})",
                    doomed + doomed).fetchall():
                root = s["thread_id"]
                if root in dset or root == s["id"]:
                    # The root went with him: this survivor becomes its own root.
                    conn.execute("UPDATE messages SET parent_id=NULL, thread_id=? WHERE id=?",
                                 (s["id"], s["id"]))
                    new_roots.append(s["id"])   # re-thread AFTER the delete
                else:
                    # thread_id is untouched: a thread's root is by definition in that
                    # thread, so reparent-to-root cannot cross threads. That is exactly
                    # why root, and not grandparent, is the right target.
                    conn.execute("UPDATE messages SET parent_id=? WHERE id=?", (root, s["id"]))
                    conn.execute(
                        "INSERT OR IGNORE INTO links(parent_id, child_id) VALUES(?,?)",
                        (root, s["id"]))
            _delete_messages(conn, doomed)
        # After the delete: walking sooner could cross a doomed node into a subtree
        # belonging to a different repair and stamp it with the wrong thread_id.
        for r in new_roots:
            _rethread(conn, r)
        conn.execute("DELETE FROM reads WHERE agent=?", (name,))
        conn.execute("DELETE FROM members WHERE room_id=? AND name=?", (room_id, name))
    return {"messages": len(doomed), "reparented": len(new_roots)}


# ---- files -------------------------------------------------------------------

# ---- hive memory (DES-001 S3) --------------------------------------------------

KINDS = ("doctrine", "contract", "decision", "lesson", "state")
TIERS = ("state", "write", "ratify")
STATE_TTL_NS = 30 * 24 * 3600 * 10**9   # Q2 resolved: stale open_tasks mislead


def _memory_insert(conn, *, kind, scope, fact, author, status, entities="",
                   source_msg_id=None, supersedes_id=None, slug=None, symptom=None,
                   root_cause=None, rule=None, detection=None, occurred_ns=None,
                   created_ns=None, expires_ns=None):
    """Low-level insert + manual FTS/entity sync. Every memory write funnels here --
    the same one-choke-point discipline as messages. Caller holds the transaction
    when the write is part of a supersession pair."""
    uid, now = _uuid(), (created_ns or time.time_ns())
    ents = set((entities or "").split()) | extract_entities(fact)
    ents = {e.lower() for e in ents if e}
    cur = conn.execute(
        "INSERT INTO memories(uid, kind, scope, fact, entities, source_msg_id, "
        "supersedes_id, slug, symptom, root_cause, rule, detection, author, status, "
        "occurred_ns, created_ns, expires_ns) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, kind, scope, fact, " ".join(sorted(ents)), source_msg_id, supersedes_id,
         slug, symptom, root_cause, rule, detection, author, status,
         occurred_ns, now, expires_ns))
    mid = cur.lastrowid
    conn.execute("INSERT INTO memories_fts(rowid, fact, entities) VALUES(?,?,?)",
                 (mid, fact, " ".join(sorted(ents))))
    conn.executemany(
        "INSERT OR IGNORE INTO memory_entities(entity, memory_id) VALUES(?,?)",
        [(e, mid) for e in ents])
    return {"id": mid, "uid": uid}


def _mem_by_uid(conn, uid):
    r = conn.execute("SELECT * FROM memories WHERE uid=?", (uid,)).fetchone()
    if not r:
        raise BusError(f"no such memory: {uid}")
    return r


def memory_add(conn, *, author, token_id, agent_bound, tier, is_admin, rooms,
               owned_rooms, fact, kind, scope="", entities="", source=0,
               supersedes="", occurred_ns=None):
    """Add one memory, gated by the caller's tier (DES-001 section 6).

    Below-tier writes land as status='draft' -- invisible to recall()/brief() until
    ratified. kind='state' is allowed ONLY for a BOUND token (ruled in 8373): under
    a shared token every agent would share one state bucket, and serving alice's
    open_tasks to bob as his own is active misinformation, worse than no gate.
    kind='lesson' goes through lesson_add(), never here -- one path per behavior."""
    if kind not in KINDS:
        raise BusError(f"bad kind {kind!r}")
    if kind == "lesson":
        raise BusError("lessons go through lesson_add(), which owns their gate")
    if not (fact or "").strip():
        raise BusError("fact is required")
    if len(fact) > 1000:
        raise BusError("fact is over 1000 chars -- distill it or link a source message")

    if kind == "state":
        if not agent_bound:
            raise AccessError(
                "state memories need a BOUND token: under a shared token every agent "
                "shares one state bucket, and a wrong brief is worse than none")
        scope = f"agent:{token_id}"
        status = "live"                       # own state is always yours to write
        expires_ns = time.time_ns() + STATE_TTL_NS
    else:
        expires_ns = None
        if scope == "global":
            status = "live" if is_admin else "draft"
        else:
            if not scope:
                if len(rooms) == 1:
                    scope = next(iter(rooms))
                elif not rooms:
                    raise AccessError("this token has no rooms")
                else:
                    raise AmbiguousRoom(
                        [{"id": r, "name": n} for r, n in rooms.items()])
            if scope not in rooms:
                raise AccessError(f"no access to room {scope}")
            if kind == "doctrine":
                status = ("live" if is_admin or
                          (tier == "ratify" and scope in owned_rooms) else "draft")
            else:                              # contract / decision
                status = "live" if (is_admin or tier in ("write", "ratify")) else "draft"

    sup_row = None
    if supersedes:
        sup_row = _mem_by_uid(conn, supersedes)
        if sup_row["scope"] != scope or sup_row["kind"] != kind:
            raise BusError("a supersede targets the SAME scope and SAME kind only")
        if sup_row["status"] not in ("live", "superseded"):
            raise BusError(f"cannot supersede a {sup_row['status']} memory")

    with tx(conn):
        out = _memory_insert(
            conn, kind=kind, scope=scope, fact=fact, author=author, status=status,
            entities=entities, source_msg_id=source or None,
            supersedes_id=sup_row["id"] if sup_row else None,
            occurred_ns=occurred_ns, expires_ns=expires_ns)
        # Flip-on-live ONLY: a draft supersede must not kill its target, or a
        # below-tier writer deletes doctrine by drafting against it (R1-M6).
        if sup_row is not None and status == "live":
            conn.execute("UPDATE memories SET status='superseded' WHERE id=? "
                         "AND status='live'", (sup_row["id"],))
    return {"id": out["uid"], "status": status}


def _mem_dict(r, chain=None, fork=False):
    m = {"id": r["uid"], "kind": r["kind"], "scope": r["scope"], "fact": r["fact"],
         "entities": r["entities"].split() if r["entities"] else [],
         "source_msg_id": r["source_msg_id"], "author": r["author"],
         "status": r["status"], "occurred_ns": r["occurred_ns"],
         "created_ns": r["created_ns"]}
    if r["slug"]:
        m.update(slug=r["slug"], symptom=r["symptom"], root_cause=r["root_cause"],
                 rule=r["rule"], detection=r["detection"])
    if chain is not None:
        m["chain"] = chain                     # supersession depth behind this tip
    if fork:
        m["fork"] = True                       # a sibling also superseded the target
    return m


def _chain_info(conn, row):
    """(depth, fork) for a memory: how many ancestors it superseded, and whether any
    OTHER memory also claims its direct target (the section 10 fork case)."""
    depth, fork, cur = 0, False, row
    if row["supersedes_id"] is not None:
        n = conn.execute(
            "SELECT count(*) FROM memories WHERE supersedes_id=? AND status IN "
            "('live','draft')", (row["supersedes_id"],)).fetchone()[0]
        fork = n > 1
    while cur["supersedes_id"] is not None:
        cur = conn.execute("SELECT * FROM memories WHERE id=?",
                           (cur["supersedes_id"],)).fetchone()
        if cur is None:
            break
        depth += 1
        if depth > 100:                        # a cycle would be a bug, not a chain
            break
    return depth, fork


def recall(conn, *, rooms, token_id, caller="", tier="state", is_admin=False,
           owned_rooms=(), query="", kind="", scope="", entity="", author="",
           since_ns=None, until_ns=None, status="live", limit=10, explain=False):
    """Ranked live facts (DES-001 section 5). Read scoping is the invariant every
    read path obeys: global OR the caller's rooms OR the caller's OWN agent scope --
    other agents' state is never returned, at any status. Drafts are visible to
    their author, to ratify-eligible owners (owned_rooms), and to admins."""
    where = ["(m.scope='global' OR m.scope IN (%s) OR m.scope=?)" % _ph(list(rooms))]
    args = list(rooms) + [f"agent:{token_id}"]
    where.append("(m.expires_ns IS NULL OR m.expires_ns > ?)")
    args.append(time.time_ns())
    if status == "draft" and not is_admin:
        # Draft visibility is about the CALLER: own drafts always, PLUS the ratify
        # queue (owned-room drafts) only when the caller's tier can actually act on
        # it. Section 5: ratify-tier callers see the queue; a write/state caller that
        # merely owns the room sees only what it authored, never a queue it cannot
        # clear (msg 8400, disclosure side of the same missing parameter).
        owned = list(owned_rooms) if tier == "ratify" else []
        cond = "m.author=?"
        dargs = [caller]
        if owned:
            cond += f" OR m.scope IN ({_ph(owned)})"
            dargs += owned
        where.append(f"({cond})")
        args += dargs
    where.append("m.status=?")
    args.append(status)
    if kind:
        where.append("m.kind=?")
        args.append(kind)
    if scope:
        where.append("m.scope=?")
        args.append(scope)
    if entity:
        where.append("m.id IN (SELECT memory_id FROM memory_entities WHERE entity=?)")
        args.append(entity.lower())
    if author:
        where.append("m.author=?")
        args.append(author)
    if since_ns is not None:
        where.append("m.created_ns >= ?")
        args.append(since_ns)
    if until_ns is not None:
        where.append("m.created_ns <= ?")
        args.append(until_ns)
    join, sel_rank = "", ""
    if query:
        terms = []
        for k in query.split():
            prefix = k.endswith("*") and len(k) > 1
            base = k[:-1] if prefix else k
            terms.append('"' + base.replace('"', '""') + '"' + ("*" if prefix else ""))
        join = " JOIN memories_fts f ON f.rowid = m.id"
        sel_rank = ", f.rank AS _rank"
        where.append("memories_fts MATCH ?")
        args.append(" OR ".join(terms))
    limit = max(1, min(int(limit), 200))
    # Pool order must agree with what the scorer values most (S3 review F2): with a
    # query the 0.5-weighted bm25 term dominates, so the pool takes the BEST FTS
    # matches -- a recency-ordered pool would silently drop a perfect old match past
    # row limit*4. Without a query the top-weighted signal IS recency, so newest-first
    # is the honest pool; pool_truncated below tells the caller when it hit bottom.
    pool_order = "f.rank" if query else "m.created_ns DESC"
    rows = conn.execute(
        f"SELECT m.*{sel_rank} FROM memories m{join} WHERE " + " AND ".join(where) +
        f" ORDER BY {pool_order} LIMIT ?", args + [limit * 4]).fetchall()

    now = time.time_ns()
    ent_q = {e.lower() for e in (entity or query or "").replace("*", " ").split()}
    scored = []
    for r in rows:
        bm = -(r["_rank"] if query else 0.0)
        ents = set(r["entities"].split())
        overlap = len(ents & ent_q) / len(ent_q) if ent_q else 0.0
        kw = {"doctrine": 1.0, "contract": 0.9, "decision": 0.8,
              "lesson": 0.9, "state": 0.6}[r["kind"]]
        age_d = max(0.0, (now - r["created_ns"]) / 86400e9)
        rec = 1.0 / (1.0 + age_d / 30.0)
        final = 0.5 * bm + 0.2 * overlap + 0.15 * kw + 0.15 * rec
        scored.append((final, {"bm25_norm": bm, "entity_overlap": overlap,
                               "kind_weight": kw, "recency_decay": rec}, r))
    if query:
        mx = max((s for s, _, _ in scored), default=1.0) or 1.0
        scored = [(s / mx, d, r) for s, d, r in scored]
    scored.sort(key=lambda t: -t[0])
    out = []
    for final, comp, r in scored[:limit]:
        depth, fork = _chain_info(conn, r)
        m = _mem_dict(r, chain=depth, fork=fork)
        if explain:
            m["score"] = {"final": round(final, 4),
                          **{k: round(v, 4) for k, v in comp.items()}}
        out.append(m)
    return {"memories": out, "count": len(out),
            "pool_truncated": len(rows) == limit * 4}


def memory_retract(conn, uid, *, actor, is_admin):
    r = _mem_by_uid(conn, uid)
    if r["author"] != actor and not is_admin:
        raise AccessError("only the author or an admin retracts a memory")
    conn.execute("UPDATE memories SET status='retracted' WHERE id=? AND "
                 "status IN ('live','draft')", (r["id"],))
    return {"id": uid, "status": "retracted"}


def ratify_memory(conn, uid, *, tier="state", is_admin, owned_rooms):
    """draft -> live. The ratify TIER is the capability; owning the room is only its
    SCOPE -- both are required, never either (msg 8400: an ownership-only gate lets any
    owned-room token self-promote its own drafts, making the whole tier ladder
    cosmetic). scope='global' still requires an instance admin (R1-M3); tier does not
    grant global. Flipping live also completes any pending supersession (R1-M6).
    tier defaults to 'state' so a caller that forgets to thread it gets the LEAST
    privilege, never the most."""
    r = _mem_by_uid(conn, uid)
    if r["status"] != "draft":
        raise BusError(f"memory is {r['status']}, not draft")
    if r["scope"] == "global":
        if not is_admin:
            raise AccessError("global ratification requires an instance admin")
    elif not is_admin:
        if tier != "ratify":
            raise AccessError("ratify requires a ratify-tier token")
        if r["scope"] not in owned_rooms:
            raise AccessError("ratify is per-room: you must own this room")
    with tx(conn):
        conn.execute("UPDATE memories SET status='live' WHERE id=?", (r["id"],))
        if r["supersedes_id"] is not None:
            conn.execute("UPDATE memories SET status='superseded' WHERE id=? AND "
                         "status='live'", (r["supersedes_id"],))
    return {"id": uid, "status": "live"}


def sweep_expired_state(conn):
    """Hard-delete expired state memories (they are ephemeral by contract) with the
    FTS old-values delete sync. Joins the hourly sweep; reads already filter on
    expires_ns, so this is hygiene, not the correctness gate."""
    rows = conn.execute("SELECT id, fact, entities FROM memories WHERE kind='state' "
                        "AND expires_ns IS NOT NULL AND expires_ns <= ?",
                        (time.time_ns(),)).fetchall()
    if not rows:
        return 0
    with tx(conn):
        conn.executemany(
            "INSERT INTO memories_fts(memories_fts, rowid, fact, entities) "
            "VALUES('delete',?,?,?)",
            [(r["id"], r["fact"], r["entities"]) for r in rows])
        ids = [r["id"] for r in rows]
        ph = _ph(ids)
        conn.execute(f"DELETE FROM memory_entities WHERE memory_id IN ({ph})", ids)
        conn.execute(f"DELETE FROM memories WHERE id IN ({ph})", ids)
    return len(rows)


def brief(conn, *, rooms, token_id, role="", budget=28000):
    """The onboarding pack (DES-001 section 7): lessons, then doctrine ranked by
    entity overlap with the caller's role, then live contracts, then decisions
    (recent weighted up), then own state, then a presence digest. Budget is CHARS
    (~4/token, approximate by construction: the broker has no tokenizer, G4).
    Every truncation is MARKED -- a silent cap reads as "covered everything"."""
    budget = max(2000, int(budget))
    role_ents = {e.lower() for e in
                 (set((role or "").replace(",", " ").split()) |
                  extract_entities(role or ""))}
    parts, spent = [], 0
    truncated = []

    def emit(line):
        nonlocal spent
        parts.append(line)
        spent += len(line) + 1

    def room_scopes():
        return ["global"] + list(rooms)

    def mem_rows(kind, order="created_ns DESC"):
        scopes = room_scopes()
        return conn.execute(
            f"SELECT * FROM memories WHERE kind=? AND status='live' AND "
            f"(expires_ns IS NULL OR expires_ns > ?) AND scope IN ({_ph(scopes)}) "
            f"ORDER BY {order}", [kind, time.time_ns()] + scopes).fetchall()

    def overlap(r):
        ents = set(r["entities"].split())
        return len(ents & role_ents)

    def section(title, rows, render, cap_share):
        room_for = dict(rooms)
        cap = int(budget * cap_share)
        used, shown = 0, 0
        emit(f"== {title} ({len(rows)}) ==")
        for r in rows:
            line = render(r, room_for)
            if used + len(line) > cap or spent + len(line) > budget:
                break
            emit(line)
            used += len(line) + 1
            shown += 1
        if shown < len(rows):
            truncated.append(title)
            # the pointer must name the tool that actually serves this section:
            # lessons() for lessons, recall(kind=...) for everything else
            more = ("lessons()" if title == "lessons"
                    else f"recall(kind='{title.rstrip('s')}')")
            emit(f"[{shown} of {len(rows)} shown -- {more} for the rest]")

    # 1. lessons -- the rules the fleet already paid for, all of them if they fit
    lrows = conn.execute(
        f"SELECT * FROM memories WHERE kind='lesson' AND status='live' AND "
        f"scope IN ({_ph(room_scopes())}) ORDER BY created_ns DESC", room_scopes()
    ).fetchall()
    section("lessons", lrows,
            lambda r, _: f"- {r['slug']}: {r['rule']} [detect: {r['detection']}]", 0.30)
    # 2. doctrine, role-relevant first
    drows = sorted(mem_rows("doctrine"), key=lambda r: (-overlap(r), -r["created_ns"]))
    section("doctrine", drows, lambda r, _: f"- {r['fact']}", 0.25)
    # 3. live contracts (supersession already resolved by status='live')
    section("contracts", mem_rows("contract"),
            lambda r, _: f"- {r['fact']}"
                         + (f" [src msg {r['source_msg_id']}]" if r["source_msg_id"] else ""),
            0.20)
    # 4. decisions -- last 30d first, older by role relevance
    cutoff = time.time_ns() - 30 * 24 * 3600 * 10**9
    dec = mem_rows("decision")
    dec = sorted(dec, key=lambda r: (r["created_ns"] < cutoff, -overlap(r),
                                     -r["created_ns"]))
    section("decisions", dec,
            lambda r, _: f"- {r['fact']}"
                         + (f" [src msg {r['source_msg_id']}]" if r["source_msg_id"] else ""),
            0.20)
    # 5. own state (restart case) -- only ever the caller's own bucket
    srows = conn.execute(
        "SELECT * FROM memories WHERE kind='state' AND status='live' AND scope=? "
        "AND (expires_ns IS NULL OR expires_ns > ?) ORDER BY created_ns DESC",
        (f"agent:{token_id}", time.time_ns())).fetchall()
    if srows:
        section("state", srows, lambda r, _: f"- {r['fact']}", 0.15)
    # 6. presence digest
    emit("== presence ==")
    for rid, rname in rooms.items():
        live = [r["name"] for r in conn.execute(
            "SELECT name, seen_ns FROM members WHERE room_id=? ORDER BY seen_ns DESC",
            (rid,)) if _is_live(r["seen_ns"], time.time_ns())]
        emit(f"- {rname}: {', '.join(live) if live else '(nobody live)'}")

    return {"text": "\n".join(parts)[:budget], "chars": min(spent, budget),
            "sections": {"lessons": len(lrows), "doctrine": len(drows),
                         "contracts": len(mem_rows('contract')),
                         "decisions": len(dec), "state": len(srows)},
            "truncated": truncated}


# ---- lessons: same API, rebacked onto memories (DES-001 B1) ----------------------

def add_lesson(conn, *, author, slug, symptom, root_cause, rule, detection, room_id=None):
    """Record one lesson. Same signature and observable behavior as ever: re-using a
    slug in the same scope replaces it. Underneath, replacement is a SUPERSESSION
    (add-only, G5) -- and a replacement by a DIFFERENT author lands as draft, because
    lessons are the most-obeyed kind in the system and slug re-use was an unguarded
    overwrite of someone else's rule (R1-B5). Fresh lessons stay any-agent-live."""
    valid_name(slug)
    scope = "global" if room_id is None else room_id
    tip = conn.execute(
        "SELECT * FROM memories WHERE kind='lesson' AND scope=? AND slug=? AND "
        "status='live'", (scope, slug)).fetchone()
    status = "live"
    if tip is not None and tip["author"] != author:
        status = "draft"
    with tx(conn):
        out = _memory_insert(
            conn, kind="lesson", scope=scope, fact=rule[:1000], author=author,
            status=status, supersedes_id=tip["id"] if tip is not None else None,
            slug=slug, symptom=symptom, root_cause=root_cause, rule=rule,
            detection=detection)
        if tip is not None and status == "live":
            conn.execute("UPDATE memories SET status='superseded' WHERE id=? AND "
                         "status='live'", (tip["id"],))
    return {"id": out["uid"], "slug": slug, "room": room_id,
            **({"status": "draft"} if status == "draft" else {})}


def _lesson(r):
    room = None if r["scope"] == "global" else r["scope"]
    return {"slug": r["slug"], "room": room, "symptom": r["symptom"],
            "root_cause": r["root_cause"], "rule": r["rule"],
            "detection": r["detection"], "author": r["author"],
            "created_ns": r["created_ns"],
            "scope": "global" if room is None else "room"}


def lessons(conn, rooms=()):
    """Every global lesson plus the caller's rooms' lessons -- chain TIPS only
    (status='live'), newest first. Same return shape as always."""
    rooms = list(rooms or [])
    scopes = ["global"] + rooms
    rows = conn.execute(
        f"SELECT * FROM memories WHERE kind='lesson' AND status='live' AND "
        f"scope IN ({_ph(scopes)}) ORDER BY created_ns DESC", scopes)
    return [_lesson(r) for r in rows]


def promote_lesson(conn, slug, room_id, promoted_by="admin"):
    """Room lesson -> global. Promotion is a superseding row at scope='global'
    authored by the promoting admin (R1-B1) -- the room tip flips to superseded, so
    history keeps who wrote it and when it went global."""
    tip = conn.execute(
        "SELECT * FROM memories WHERE kind='lesson' AND scope=? AND slug=? AND "
        "status='live'", (room_id, slug)).fetchone()
    if tip is None:
        raise BusError(f"no such lesson in this room: {slug}")
    with tx(conn):
        # Promotion is the ONE sanctioned cross-scope supersede (S3 review F1): the
        # global row must carry the chain link to its room-scoped ancestor or the
        # promotion's provenance is dropped -- history would answer WHAT went global
        # but never WHERE it came from. memory_add's same-scope constraint stands for
        # every other path; only this function may cross, and only room -> global.
        _memory_insert(
            conn, kind="lesson", scope="global", fact=tip["rule"][:1000],
            author=promoted_by, status="live", supersedes_id=tip["id"],
            slug=tip["slug"], symptom=tip["symptom"], root_cause=tip["root_cause"],
            rule=tip["rule"], detection=tip["detection"])
        conn.execute("UPDATE memories SET status='superseded' WHERE id=?", (tip["id"],))


def record_file(conn, stored, room_id, uploaded_by):
    conn.execute(
        "INSERT OR REPLACE INTO files(stored, room_id, uploaded_by, ts_ns) VALUES(?,?,?,?)",
        (stored, room_id, uploaded_by, time.time_ns()))


def file_room(conn, stored):
    r = conn.execute("SELECT room_id FROM files WHERE stored=?", (stored,)).fetchone()
    return r["room_id"] if r else None
