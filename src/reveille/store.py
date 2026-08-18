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
import json
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
# AN ATTACHMENT URL IS FOREIGN INPUT THAT ENDS UP IN SOMEBODY ELSE'S BROWSER.
# The bus UI interpolates it into href, src and data-src, on the broker's origin
# -- the origin DES-006 deliberately shares with agent management. send() took it
# verbatim from any bus client, so any agent token or authenticated web user could
# store markup that runs in the operator's session.
#
# This is the AUTHORITY, not a convenience: /files/<stored> is the only url the
# broker ever mints, and upload already sanitises the stored name to exactly this
# character set (daemon._FNAME_RE). Anything else was never ours to serve, so
# refusing it here costs a legitimate caller nothing. The client-side escape is the
# other half and is not redundant -- it is what survives the day this constraint
# widens, which is the argument the esc() slice made one level down.
#
# The first character may not be a dot, which costs nothing real -- every stored
# name begins with an epoch-millis prefix -- and refuses `.` and `..` outright
# rather than relying on the serving side to 404 them.
FILE_URL_RE = re.compile(r"/files/[A-Za-z0-9_-][A-Za-z0-9._-]*")


def valid_file_url(url):
    if not FILE_URL_RE.fullmatch(url or ""):
        raise BusError(
            f"attachment url must be a broker file path (/files/<stored>), got {url!r}. "
            f"Upload the bytes first -- the url it returns is the only one that serves.")
BROADCAST = "*"
SCHEMA_VERSION = 33

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
    created_ns INTEGER NOT NULL,
    -- A USERS ROW IS NEVER DELETED (architect ruling, msg 8938): deletion is
    -- this stamp. agents.owner_id points here with no cascade and no nullable
    -- second shape, hive contributions stay attributed (DES-005 7.1), and the
    -- username stays TAKEN -- reusing it would re-attribute someone else's
    -- history to a new person. Credentials are wiped at deletion; this row is
    -- the referent, not the account.
    deleted_ns INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    id_hash    TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL REFERENCES users(id),
    created_ns INTEGER NOT NULL,
    expires_ns INTEGER NOT NULL
);
-- DES-018: a provider identity is a CREDENTIAL of a person, not a person.
-- (provider, subject) is the key; email is a hint kept for linking + display.
CREATE TABLE IF NOT EXISTS identities (
    provider       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    user_id        TEXT NOT NULL REFERENCES users(id),
    email          TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    display_name   TEXT,
    avatar_url     TEXT,
    raw_profile    TEXT,
    created_ns     INTEGER NOT NULL,
    last_login_ns  INTEGER,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_identities_user  ON identities(user_id);
CREATE INDEX IF NOT EXISTS idx_identities_email ON identities(email);
-- Authlib's state/nonce/code_verifier and our browser marker, server-side,
-- swept by TTL. Nothing of the login lives in a signed cookie.
CREATE TABLE IF NOT EXISTS oidc_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_ns INTEGER NOT NULL
);
-- Every link / unlink / federated signup, durable (DES-018 s5).
CREATE TABLE IF NOT EXISTS identity_audit (
    id       INTEGER PRIMARY KEY,
    action   TEXT NOT NULL CHECK (action IN ('signup','link','unlink',
                                             'request','approve','deny','invite')),
    provider TEXT NOT NULL,
    subject  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    actor    TEXT NOT NULL,
    ts_ns    INTEGER NOT NULL
);
-- DES-018 s6 amendment (ruling 11709): a stranger's REQUEST is not a user.
-- No user row exists until an admin approves, so a pending request reserves
-- no name, holds no session and cannot be a half-user anywhere else.
CREATE TABLE IF NOT EXISTS signup_requests (
    provider       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    email          TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    display_name   TEXT,
    avatar_url     TEXT,
    login          TEXT,
    note           TEXT,
    state          TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','denied')),
    requested_ns   INTEGER NOT NULL,
    decided_ns     INTEGER,
    decided_by     TEXT,
    PRIMARY KEY (provider, subject)
);
-- One-time invite codes. The code itself is shown ONCE at creation and stored
-- only as a hash -- same discipline as a token: a leaked database is not a
-- pile of working invitations.
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    used_by    TEXT,
    used_ns    INTEGER
);
-- EPIC-001 #6: how far a PERSON has read in each room. Agents have read
-- receipts per message (they must ack what was addressed to them); a person
-- reads a room, so one high-water mark per (principal, room) is the whole
-- story -- and it stays one row however long the room gets.
CREATE TABLE IF NOT EXISTS room_seen (
    principal   TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    last_msg_id INTEGER NOT NULL,
    ts_ns       INTEGER NOT NULL,
    PRIMARY KEY (principal, room_id)
);
-- DES-012: A VISIT IS A BODY SWAP. One row per visit, from the ask to the end
-- (s8): a visit is a feature, and a feature earns its schema. Nothing here is
-- a credential -- `token_id` names the mint the accept authorised, and the
-- secret it minted was answered to the host's screen once and never stored.
CREATE TABLE IF NOT EXISTS visits (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    owner_id     TEXT NOT NULL,          -- the agent's owner; NEVER moves (s4)
    host_id      TEXT NOT NULL,          -- the harbor: a host, not an owner
    host_machine TEXT NOT NULL DEFAULT '',
    rooms        TEXT NOT NULL,          -- json list of room ids: the visit's whole reach
    direction    TEXT NOT NULL CHECK (direction IN ('pull','push')),
    coordinate   TEXT NOT NULL DEFAULT '',   -- repo (+ SHA) the body checks out
    requested_by TEXT NOT NULL,          -- the human who asked; the OTHER one decides
    requested_ns INTEGER NOT NULL,
    expires_ns   INTEGER NOT NULL,       -- the REQUEST expires; the visit does not (s11.2)
    decided_ns   INTEGER,
    decision     TEXT CHECK (decision IN ('accept','reject')),
    body         TEXT CHECK (body IN ('container','native')),
    token_id     TEXT,                   -- the one mint this accept authorises
    arrived_ns   INTEGER,                -- stamped by the visiting body's first join()
    ended_ns     INTEGER,
    ended_by     TEXT CHECK (ended_by IN ('owner','host','agent'))
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
    -- Set = this credential IS that IDENTITY (DES-007 2.4): the agents row, not
    -- the label. The name still travels the wire (X-Agent, to=) and is resolved
    -- by join -- what the binding pins is WHICH instance of a label this
    -- credential speaks for, so a declined resurrect cannot inherit a
    -- predecessor's live token. Immutable after mint; rebinding is a new token.
    agent_id     TEXT REFERENCES agents(id),
    -- Memory write tier (DES-001 section 6): state < write < ratify. Every new token
    -- starts at 'state' -- randy day one reads the hive and writes only his own state.
    mem_tier     TEXT NOT NULL DEFAULT 'state'
                 CHECK (mem_tier IN ('state','write','ratify')),
    created_ns   INTEGER NOT NULL,
    last_used_ns INTEGER
);
-- S2 (ruling 10876): a SUPERSEDED credential leaves a tombstone so its refusal
-- can be a signpost -- the dormant body's one honest channel. EXACTLY these
-- four fields, nothing else: the holder of a superseded token is either the
-- former body or whoever stole a dead credential, and only one of those
-- deserves an inventory. last_refusal_ns doubles as S3's liveness reading
-- ("a human used that machine at T"). No FK: same discipline as token_audit,
-- the record outlives both the token and, if pruned, the agent it names.
-- Plain revoke does NOT tombstone -- revocation is deliberate and gets the
-- generic refusal; only supersession (the body moved) earns a signpost.
CREATE TABLE IF NOT EXISTS token_tombstones (
    secret_hash     TEXT PRIMARY KEY,
    agent_id        TEXT NOT NULL,
    superseded_ns   INTEGER NOT NULL,
    last_refusal_ns INTEGER
);
-- The token->rooms mapping lives here, NOT in the token. Read on every request so
-- assign/unassign/revoke/flip-to-private are all instant.
CREATE TABLE IF NOT EXISTS token_rooms (
    token_id TEXT NOT NULL REFERENCES tokens(id) ON DELETE CASCADE,
    room_id  TEXT NOT NULL REFERENCES rooms(id) ON DELETE CASCADE,
    PRIMARY KEY (token_id, room_id)
);
-- USER membership (DES-004): who may attach this room to a token, beyond the
-- owner and beyond public. Distinct from `members` below, which is AGENT
-- presence. Reach, never rule: ratification stays with room ownership.
CREATE TABLE IF NOT EXISTS room_members (
    room_id  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    added_ns INTEGER NOT NULL,
    added_by TEXT NOT NULL,
    PRIMARY KEY (room_id, user_id)
);
-- Membership changes are authority changes (the S6b tier-flip precedent):
-- no FKs, the record outlives the membership, the user, and the room.
CREATE TABLE IF NOT EXISTS room_audit (
    id      INTEGER PRIMARY KEY,
    room_id TEXT NOT NULL,
    action  TEXT NOT NULL CHECK (action IN ('invite','remove','adopt')),
    actor   TEXT NOT NULL,
    subject TEXT NOT NULL,
    ts_ns   INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_roomaudit_room ON room_audit(room_id);
-- One row per (room, IDENTITY) -- DES-011 s6.1(b): the key is who, the name is
-- what the room calls them. principal is the DES-013 s2 speaker key,
-- agent:<agents.id> for a body holding a bound token, user:<users.id> for a
-- person at the web page. name is the ROOM-NAME: the identity's own name, or
-- the alias <owner>-<name> assigned at join when the bare name was already held
-- live by another owner's agent in this room (s2). Unique per room among LIVE
-- rows (idx_members_roomname); a row marked left keeps its name but holds
-- nothing.
CREATE TABLE IF NOT EXISTS members (
    room_id   TEXT NOT NULL REFERENCES rooms(id),
    principal TEXT NOT NULL,
    name      TEXT NOT NULL,
    tag       TEXT,
    url       TEXT,
    token_id  TEXT REFERENCES tokens(id),
    joined_ns INTEGER NOT NULL,
    seen_ns   INTEGER NOT NULL,
    -- Set when the agent LEFT deliberately (DIRECTIVE:LEAVE). The row stays so
    -- that a departure is distinguishable from a reap: the reaper DELETES, and
    -- readmit() only fills a gap where no row exists. Without this column the
    -- two are the same absence, and re-admitting on the next call silently
    -- undoes leave() -- ratified doctrine, voided by an implementation detail.
    left_ns   INTEGER,
    PRIMARY KEY (room_id, principal)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_members_roomname
    ON members(room_id, name) WHERE left_ns IS NULL;
-- WHO AN AGENT IS (DES-007, ruling 8665). The identity is a UUID; (owner_id,
-- name) is a LABEL on it, not a key. The operator's own requirement proves the
-- composite cannot be the key: declining a resurrect mints a NEW identity under
-- the SAME name, so one (user, name) maps to several histories over time and a
-- composite key would collide with itself the first time somebody declined.
--
-- Lives here rather than in the launcher because the hive holds the history and
-- the whole point of an identity is to segment history -- the launcher's
-- container rows are ephemeral by design and deleting one is what lost the
-- ownership fact in the first place.
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    created_ns  INTEGER NOT NULL,
    retired_ns  INTEGER,
    released_ns INTEGER,
    released_by TEXT,
    -- DES-011 s7/s10: the survivor this identity was FOLDED into (one-time
    -- merge). The row stays retired-intact; readers follow the chain to its head.
    merged_into TEXT REFERENCES agents(id)
);
-- THIS INDEX IS THE RULE "one live instance per name per user", enforced by the
-- database rather than by anyone remembering it -- which is this week's whole
-- thesis applied to a schema. A second live mint under the same label fails at
-- the constraint, in the path the mistake takes, with no code to route around.
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_live
    ON agents(owner_id, name) WHERE retired_ns IS NULL;
CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name);
-- THE RENAME LOG (DES-011 s6/s10): which label an identity wore, and when. One
-- open row (to_ns NULL) per agent at any time; a rename closes it and opens the
-- next. Seeded from agents.name at v27; every INSERT INTO agents writes its row
-- through record_agent_name(). "Who was `architect` in July" is a query here.
CREATE TABLE IF NOT EXISTS agent_names (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    name     TEXT NOT NULL,
    from_ns  INTEGER NOT NULL,
    to_ns    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_names_agent ON agent_names(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_names_name  ON agent_names(name);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER,
    parent_id INTEGER REFERENCES messages(id),
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL,
    room      TEXT NOT NULL REFERENCES rooms(id),
    -- WHICH INSTANCE sent it (DES-007 2.3). The NAME stays and stays load-bearing:
    -- agents address each other by name, `to=` is a name on the wire, and humans
    -- read names. The id is what segments instances of one label over time, which
    -- is the whole reason purge-by-name is unsafe the day a name has two
    -- histories. Neither column replaces the other.
    sender_agent_id TEXT,
    -- WHICH IDENTITY it was addressed to (DES-011 s3/s10): resolved from the
    -- room-name at send, backfilled by (room, name, time) at v27. NULL = a
    -- broadcast, a human, or a historical address nobody could resolve (listed
    -- by the migration, never silent). `recipient` stays: it is the only record
    -- of what a message was addressed to before the rename log existed.
    recipient_agent_id TEXT,
    ts_ns     INTEGER NOT NULL
);
-- Receipts key on the IDENTITY (DES-011 s6.1(b); DES-007 4.3 said why a name
-- cannot: a resurrected identity under a reused name would inherit the previous
-- instance's read state, and a DECLINED resurrect would mint an agent that
-- appears to have already read mail it has never seen -- inbox() would skip it
-- silently). principal as in members.
CREATE TABLE IF NOT EXISTS reads (
    message_id INTEGER NOT NULL REFERENCES messages(id),
    principal  TEXT NOT NULL,
    read_ns    INTEGER NOT NULL,
    PRIMARY KEY (message_id, principal)
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
    bytes      INTEGER,
    -- DES-017: a converted clip (the message's voice), and its length.
    clip       INTEGER NOT NULL DEFAULT 0,
    duration_s REAL
);
-- DES-017 s7: the ledger of every ORIGINAL audio upload ever taken. The raw
-- bytes live RAW_HOLD_S in <files>/raw, then absoluteZeroStorage.put records
-- the intent here and the local raw goes; location fills when a cold tier
-- exists. Nothing is ever deleted from this table.
CREATE TABLE IF NOT EXISTS raw_archive (
    key         TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    bytes       INTEGER NOT NULL,
    mime        TEXT,
    message_id  INTEGER,
    uploader    TEXT,
    archived_ns INTEGER NOT NULL,
    tier        TEXT NOT NULL DEFAULT 'absolute-zero',
    location    TEXT
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
CREATE INDEX IF NOT EXISTS idx_msg_recipient_id ON messages(recipient_agent_id);
CREATE INDEX IF NOT EXISTS idx_msg_thread    ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_msg_sender    ON messages(sender);
CREATE INDEX IF NOT EXISTS idx_msg_parent    ON messages(parent_id);
CREATE INDEX IF NOT EXISTS idx_links_child   ON links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent  ON links(parent_id);
CREATE INDEX IF NOT EXISTS idx_reads_principal ON reads(principal);
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

# DES-013: THE BANK reveille owns (section 3), WHO SPEAKS WITH WHAT in a room
# (section 4), and the script beside a message (section 6). Separate constant so
# _upgrade_v22 lays exactly this; the fresh-db path gets it via _SCHEMA below.
_VOICES_SCHEMA = """
-- A bank voice: the row is the identity, the clip at <voices dir>/bank-<id>.wav
-- is the bytes (replaceable in place -- identity is the id, not the bytes).
CREATE TABLE IF NOT EXISTS voices (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    persona     TEXT NOT NULL DEFAULT '',
    sample      TEXT NOT NULL DEFAULT '',   -- a line this voice reads on audition
    personal    INTEGER NOT NULL DEFAULT 0, -- 1: exists only for its uploader (11155)
    uploaded_by TEXT NOT NULL REFERENCES users(id),
    seconds     REAL NOT NULL,
    bytes       INTEGER NOT NULL,
    created_ns  INTEGER NOT NULL,
    updated_ns  INTEGER NOT NULL
);
-- speaker = 'agent:<agents.id>' | 'user:<users.id>' -- keyed by id, never by
-- name (DES-011). One voice per speaker per room; no two speakers share a voice
-- in a room. THE UNIQUE INDEX IS THE RULE; assign_refusal's message is the
-- courtesy that names the holder.
CREATE TABLE IF NOT EXISTS voice_assignments (
    room_id  TEXT NOT NULL REFERENCES rooms(id),
    speaker  TEXT NOT NULL,
    voice_id TEXT NOT NULL REFERENCES voices(id),
    set_by   TEXT NOT NULL CHECK (set_by IN ('owner', 'room', 'default')),
    ts_ns    INTEGER NOT NULL,
    PRIMARY KEY (room_id, speaker),
    UNIQUE (room_id, voice_id)
);
-- The character script the writer produced for one message. Derived text: never
-- in messages_fts, never in the hive. Dies with the message at _delete_messages.
CREATE TABLE IF NOT EXISTS scripts (
    message_id INTEGER PRIMARY KEY REFERENCES messages(id),
    text       TEXT NOT NULL,
    voice_id   TEXT,
    model      TEXT NOT NULL,
    ms         INTEGER NOT NULL,
    ts_ns      INTEGER NOT NULL
);
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
    -- A CITATION IS A HISTORICAL FACT, NOT A LIVE REFERENCE (architect, msg 8857).
    -- No FK action, deliberately: ON DELETE SET NULL made "cited message N" and
    -- "never cited anything" the same value, so every message delete -- prune,
    -- purge_room, retract, and the retention sweep that runs on a timer with
    -- nobody watching -- silently turned a sourced fact into an unsourceable one.
    -- The id survives the message. messages.id is INTEGER PRIMARY KEY AUTOINCREMENT,
    -- so sqlite never reuses one and a dangling citation can never re-bind to a
    -- different message. Readers get three states from two columns and no new
    -- status: NULL = never cited; id with no row = the source was DELETED; id with
    -- a row = trace() works.
    source_msg_id INTEGER,
    -- WHICH INSTANCE wrote it. author stays a name because history reads the way
    -- the world read at the time (the S6b precedent); author_agent_id is what
    -- segments two identities that shared one label.
    author_agent_id TEXT,
    supersedes_id INTEGER REFERENCES memories(id),
    slug          TEXT,
    symptom       TEXT, root_cause TEXT, rule TEXT, detection TEXT,
    author        TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'live'
                  CHECK (status IN ('live','draft','superseded','retracted',
                                    'rejected')),
    occurred_ns   INTEGER,
    created_ns    INTEGER NOT NULL,
    expires_ns    INTEGER
);
-- S6 (14.4): one row per ratification/rejection -- who approved the org's law.
-- No foreign keys ON PURPOSE: ratification transfers ownership to the org, so
-- this record must survive prune_agent and any future row lifecycle. reason is
-- required for reject (14.2: declined and undecided are different states) and
-- absent for ratify.
CREATE TABLE IF NOT EXISTS memory_audit (
    id         INTEGER PRIMARY KEY,
    memory_uid TEXT NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('ratify','reject')),
    actor      TEXT NOT NULL,
    scope      TEXT NOT NULL,
    reason     TEXT,
    ts_ns      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memaudit_uid ON memory_audit(memory_uid);
-- S6b (msg 8448): a tier flip is an AUTHORITY change and is audited like the
-- verdicts -- who flipped whose token, from what to what, when. Same no-FK
-- discipline as memory_audit: the record outlives the token it describes.
CREATE TABLE IF NOT EXISTS token_audit (
    id         INTEGER PRIMARY KEY,
    token_id   TEXT NOT NULL,
    -- The NAME stays because audit is history and reads the way the world read
    -- at the time (S6b precedent); agent_id says WHICH instance, no FK for the
    -- same reason.
    agent_name TEXT,
    agent_id   TEXT,
    action     TEXT NOT NULL CHECK (action IN ('tier')),
    actor      TEXT NOT NULL,
    old_value  TEXT,
    new_value  TEXT,
    ts_ns      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tokaudit_tok ON token_audit(token_id);
CREATE INDEX IF NOT EXISTS idx_mem_scope ON memories(scope, kind, status);
CREATE INDEX IF NOT EXISTS idx_mem_super ON memories(supersedes_id);
CREATE INDEX IF NOT EXISTS idx_mem_slug  ON memories(scope, slug) WHERE slug IS NOT NULL;
-- v10: the index covers EVERY text field a memory row carries. It shipped as
-- fact+entities while lessons rode along with four more text columns, which made
-- three of a lesson's five fields -- including the symptom you can see and the
-- detection command you would run -- unreachable by search (lesson
-- search-index-narrower-than-the-data-model). A table that gains columns widens
-- its search index in the same change.
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
    fact, entities, symptom, root_cause, rule, detection,
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
_SCHEMA += _VOICES_SCHEMA
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


class NotFound(BusError):
    """A thing that does not exist FOR THIS CALLER -- the same answer whether it
    never existed or is out of their reach (a personal voice, 11155). 404."""


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
    so any multi-statement mutation MUST run inside this or it can half-apply.

    NESTABLE BY JOINING, not by savepoints: an inner tx() inside an outer one is
    the composition case (create_token superseding inside its own mint), and the
    inner block's failure must roll back the WHOLE outer transaction -- a
    partially-applied mint is exactly what the outer tx exists to prevent. So
    the inner call is a no-op wrapper and the outermost owns COMMIT/ROLLBACK.
    """
    if conn.in_transaction:
        yield conn
        return
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
    real failure, and swallowed all three.

    THE CHAIN IS A LOOP OVER THE VERSION WE ARE AT, not a branch on the version we
    found. It used to be a ladder of arms, one per start version, each listing the
    steps to run; and every step stamped SCHEMA_VERSION rather than its own target.
    Those two together are the defect: an arm that was short by a step still ended
    with the database claiming to be current, so a truncated chain reported itself
    complete and nothing could ever notice. The v9 through v13 arms really were
    short -- they stopped at _upgrade_v14 and never called _upgrade_v15 -- and what
    hid it was luck, because _upgrade_v13 replays the whole _SCHEMA and a full
    replay heals ADDITIVE drift. The first non-additive step turns that luck into
    data loss.

    Here there is no arm to be short. Each step advances user_version to ITS OWN
    target inside its own transaction, and the loop re-reads the version and runs
    whatever is next until there is nothing left. A step that fails leaves the
    version at the last one that COMPLETED, so the next start resumes exactly
    there instead of skipping what never ran.
    """
    v = _version(conn)
    if v > SCHEMA_VERSION:
        raise BusError(f"db is newer than this build (user_version={v})")
    if v == SCHEMA_VERSION:
        return v
    if not _table_exists(conn, "messages"):        # fresh db: just lay the schema down
        _exec_script(conn, _SCHEMA)
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        return SCHEMA_VERSION
    while True:
        v = _version(conn)
        if v >= SCHEMA_VERSION:
            return SCHEMA_VERSION
        step = _UPGRADES.get(v)
        if step is not None:
            # Resolved by NAME at call time, never held as a reference. A table
            # holding function objects binds them at import, so patching
            # store._upgrade_vN -- which is how the interrupt gates inject a
            # failure -- would no longer reach dispatch, and those gates would
            # pass by measuring nothing. A gate that cannot fail is worse than
            # the defect it was written for.
            step = globals()[step]
        if step is None:
            # No step for this version: it is one the chain jumps over (v1, v6 on a
            # path that came through v5's rebuild). Stamping forward one is what the
            # old ladder did by omission; doing it explicitly keeps the loop honest
            # and terminating.
            conn.execute(f"PRAGMA user_version={v + 1}")
            continue
        step(conn, db_path)
        if _version(conn) <= v:                    # a step that did not advance
            raise BusError(
                f"migration step for user_version={v} did not advance the version. "
                f"Every step stamps its own target; one that does not would loop "
                f"forever, and a database mid-chain is worse than a refusal.")

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
        conn.execute("PRAGMA user_version=3")


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
        conn.execute("PRAGMA user_version=4")
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
        conn.execute("PRAGMA user_version=5")
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
        conn.execute("PRAGMA user_version=6")
    return len(rows)


def _upgrade_v6(conn, db_path):
    """v6 -> v7: re-extract entities. The ID pattern generalized (ADR-only ->
    word-dash-number, found live: entity=des-001 was empty with the naming thread
    right there), and an extraction change without a re-extract would leave history
    indexed under the OLD rules -- two vocabularies pretending to be one index.
    Same body as v5: the backfill is already a delete-and-rebuild -- but the STAMP
    is this step's own, because v5's says 6 and finishing here means 7. A step that
    borrows another's body must not borrow its version (msg 8876)."""
    n = _upgrade_v5(conn, db_path)
    conn.execute("PRAGMA user_version=7")
    return n


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
        conn.execute("PRAGMA user_version=8")


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
        conn.execute("PRAGMA user_version=9")


def _upgrade_v9(conn, db_path):
    """v9 -> v10: widen memories_fts to every text column the row carries.

    The v9 index covered fact+entities while lessons rode along with symptom,
    root_cause, rule and detection -- so a completeness sweep for a term sitting
    in root_cause returned zero against a row that demonstrably contained it
    (lesson search-index-narrower-than-the-data-model). An external-content FTS
    table cannot ALTER its column set: drop, recreate at the new shape, rebuild
    from the rows that exist now -- same obligation the v0 rebuild states."""
    snapshot(conn, f"{db_path}.pre-v10-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    with tx(conn):
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        _exec_script(conn, """
            CREATE VIRTUAL TABLE memories_fts USING fts5(
                fact, entities, symptom, root_cause, rule, detection,
                content='memories', content_rowid='id',
                tokenize="unicode61 tokenchars '-_'"
            )
        """)
        conn.execute(
            "INSERT INTO memories_fts(rowid, fact, entities, symptom, root_cause, "
            "rule, detection) SELECT id, fact, entities, symptom, root_cause, "
            "rule, detection FROM memories")
        conn.execute("PRAGMA user_version=10")


def _upgrade_v10(conn, db_path):
    """v10 -> v11 (S6 store plane): status gains 'rejected' and the ratify/reject
    audit table arrives.

    Rejection is a real outcome with a reason, distinct from still-queued (14.2)
    -- draft rot is only diagnosable if declined and undecided are different
    states. SQLite cannot ALTER a CHECK constraint, so the memories table is
    rebuilt (copy, drop, rename); ids are preserved, but the external-content
    FTS rides the table's identity, so it is dropped first and rebuilt after --
    the v0 obligation again. memory_audit carries no foreign keys on purpose:
    the record of who approved the org's law must survive prune_agent and every
    row lifecycle (14.4)."""
    snapshot(conn, f"{db_path}.pre-v11-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    with tx(conn):
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        conn.execute("ALTER TABLE memories RENAME TO memories_old")
        _exec_script(conn, _MEMORIES_SCHEMA)   # new table, indexes, fts, audit
        cols = ("id, uid, kind, scope, fact, entities, source_msg_id, "
                "supersedes_id, slug, symptom, root_cause, rule, detection, "
                "author, status, occurred_ns, created_ns, expires_ns")
        conn.execute(f"INSERT INTO memories({cols}) "
                     f"SELECT {cols} FROM memories_old")
        conn.execute("DROP TABLE memories_old")
        conn.execute(
            "INSERT INTO memories_fts(rowid, fact, entities, symptom, root_cause, "
            "rule, detection) SELECT id, fact, entities, symptom, root_cause, "
            "rule, detection FROM memories")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise BusError(f"v11 migration left {len(bad)} FK violations")
        conn.execute("PRAGMA user_version=11")


def _upgrade_v11(conn, db_path):
    """v11 -> v12 (S6b): token_audit arrives. A tier flip is an authority change
    (msg 8448) and gets the same audit discipline as the memory verdicts.
    Additive only: _MEMORIES_SCHEMA is idempotent and creates just what is
    missing."""
    with tx(conn):
        _exec_script(conn, _MEMORIES_SCHEMA)
        conn.execute("PRAGMA user_version=12")


def _upgrade_v12(conn, db_path):
    """v12 -> v13: data-only. promote_lesson used to leave a live same-slug
    global predecessor standing (msg 8461), so a db may carry several live
    rows per (scope, slug). Keep the newest per pair (created_ns, id as the
    tiebreak -- promotion inserts later), supersede the rest. Matches nothing
    on a db the interim store-side fix already cleaned."""
    snapshot(conn, f"{db_path}.pre-v13-{time.strftime('%Y%m%dT%H%M%S')}.bak")
    with tx(conn):
        conn.execute(
            "UPDATE memories SET status='superseded' WHERE kind='lesson' AND "
            "slug IS NOT NULL AND status='live' AND id NOT IN ("
            "  SELECT id FROM memories m WHERE kind='lesson' AND slug IS NOT NULL "
            "  AND status='live' AND NOT EXISTS ("
            "    SELECT 1 FROM memories n WHERE n.kind='lesson' AND n.status='live' "
            "    AND n.scope=m.scope AND n.slug=m.slug AND "
            "    (n.created_ns>m.created_ns OR "
            "     (n.created_ns=m.created_ns AND n.id>m.id))))")
        conn.execute("PRAGMA user_version=13")


def _upgrade_v13(conn, db_path):
    """v13 -> v14 (DES-004 M1): room_members + room_audit arrive. Additive only:
    _SCHEMA's CREATE IF NOT EXISTS lays down exactly what is missing."""
    with tx(conn):
        _exec_script(conn, _SCHEMA)
        conn.execute("PRAGMA user_version=14")


def _upgrade_v14(conn, db_path):
    """v14 -> v15: members.left_ns. CREATE IF NOT EXISTS cannot add a column to an
    existing table, so this is an explicit ALTER. Every existing row gets NULL,
    which is exactly right: nobody who is in a room today left it."""
    with tx(conn):
        # sqlite has no ADD COLUMN IF NOT EXISTS, and _SCHEMA (used by the fresh
        # path and by every additive upgrade) already carries the column -- so
        # check, the same way the token upgrades do. An upgrade step that cannot
        # run twice cannot be re-run after a partial chain.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
        if "left_ns" not in cols:
            conn.execute("ALTER TABLE members ADD COLUMN left_ns INTEGER")
        conn.execute("PRAGMA user_version=15")


def _upgrade_v15(conn, db_path):
    """v15 -> v16: the agents table (DES-007). Purely additive -- no existing row
    moves and nothing reads it yet, which is deliberate: the record has to start
    being written before the enforcement that reads it exists, or every agent
    provisioned in between has an ownership fact that cannot be recovered later
    (ruling 8660, recording is urgent and enforcing is not)."""
    with tx(conn):
        _exec_script(conn, """
CREATE TABLE IF NOT EXISTS agents (
    id          TEXT PRIMARY KEY,
    owner_id    TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    created_ns  INTEGER NOT NULL,
    retired_ns  INTEGER,
    released_ns INTEGER,
    released_by TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_live
    ON agents(owner_id, name) WHERE retired_ns IS NULL;
CREATE INDEX IF NOT EXISTS idx_agents_name ON agents(name);
""")
        conn.execute("PRAGMA user_version=16")


def _upgrade_v16(conn, db_path):
    """v16 -> v17: memories.source_msg_id stops being a live reference (msg 8857).

    It was INTEGER REFERENCES messages(id) ON DELETE SET NULL, so deleting a message
    rewrote every fact distilled from it to look like a fact that never had a source.
    Four paths delete messages -- prune_agent, purge_room, retract_message and
    sweep_retention -- and the last runs on a timer with nobody present, so guarding
    the callers would have left the only automatic one intact. The column is the fix.

    NON-ADDITIVE, which is why it stamps its own target rather than SCHEMA_VERSION:
    the full-schema replay that has been quietly healing additive drift on truncated
    chains cannot heal this one -- CREATE TABLE IF NOT EXISTS sees the old table and
    leaves its FK action alone (msg 8804). Rebuild, the same way v11 did: sqlite
    cannot ALTER a column's foreign-key action, and the external-content FTS rides
    the table's identity, so it is dropped first and rebuilt after.

    Citations already nulled by a delete before this landed are NOT recoverable --
    the value is gone, not hidden. This stops the next one.
    """
    # Nanoseconds, not seconds: snapshot() refuses an existing path, and a step that
    # cannot run twice in the same second cannot be re-run after a partial chain --
    # which is exactly when a rebuild is most likely to be re-run. The second-
    # resolution names on the older steps are a latent version of this and are not
    # mine to change in this slice.
    snapshot(conn, f"{db_path}.pre-v17-{time.time_ns()}.bak")
    with tx(conn):
        conn.execute("DROP TABLE IF EXISTS memories_fts")
        conn.execute("ALTER TABLE memories RENAME TO memories_old")
        _exec_script(conn, _MEMORIES_SCHEMA)   # new table, indexes, fts, audit
        cols = ("id, uid, kind, scope, fact, entities, source_msg_id, "
                "supersedes_id, slug, symptom, root_cause, rule, detection, "
                "author, status, occurred_ns, created_ns, expires_ns")
        conn.execute(f"INSERT INTO memories({cols}) "
                     f"SELECT {cols} FROM memories_old")
        conn.execute("DROP TABLE memories_old")
        conn.execute(
            "INSERT INTO memories_fts(rowid, fact, entities, symptom, root_cause, "
            "rule, detection) SELECT id, fact, entities, symptom, root_cause, "
            "rule, detection FROM memories")
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            raise BusError(f"v17 migration left {len(bad)} FK violations")
        conn.execute("PRAGMA user_version=17")


def _rescope_state_notes(conn):
    """Move state notes from agent:<token_id> to agent:<agent_id>. Returns
    (moved, ambiguous) counts.

    RESOLVED THROUGH THE AUTHOR, AND ONLY WHEN THE AUTHOR IS UNAMBIGUOUS --
    exactly one agents row for that name. The first version resolved through the
    minting token, which rotates on every re-mint: 47 of 50 live notes sat at
    dead scopes. The second version resolved through the author with an ORDER BY
    preferring the live identity -- which, the day a declined resurrect gives a
    name two identities, moves the RETIRED agent's memory to the LIVE one.
    One agent's history handed to another, silently, by a migration (architect,
    msg 9000). A row that cannot be resolved without guessing is LEFT PUT and
    COUNTED: it is the operator's to assign, which is 6.1's own rule one plane
    over. No timestamp windows -- the identities on the only live database were
    seeded after every historical row, so a time window would resolve nothing.
    """
    moved = conn.execute("""
        UPDATE memories SET scope = 'agent:' || (
            SELECT a.id FROM agents a WHERE a.name = memories.author)
         WHERE kind='state' AND scope LIKE 'agent:%'
           AND scope NOT IN (SELECT 'agent:' || id FROM agents)
           AND (SELECT count(*) FROM agents a
                 WHERE a.name = memories.author) = 1""").rowcount
    ambiguous = conn.execute("""
        SELECT count(*) FROM memories
         WHERE kind='state' AND scope LIKE 'agent:%'
           AND scope NOT IN (SELECT 'agent:' || id FROM agents)
           AND (SELECT count(*) FROM agents a
                 WHERE a.name = memories.author) > 1""").fetchone()[0]
    if ambiguous:
        print(f"state rescope: left {ambiguous} note(s) whose author has more "
              f"than one identity -- assign them explicitly rather than let a "
              f"migration guess which agent's memory this is")
    return moved, ambiguous


def _upgrade_v17(conn, db_path):
    """v17 -> v18: history carries the IDENTITY, not only the label (DES-007 6).

    THE FIRST MIGRATION IN THIS CODEBASE THAT REFUSES. Every column here is
    filled by resolving a historical NAME against the agents table, and a name
    with no row cannot be resolved. The two ways to get past that are both
    forbidden: inventing an owner, and leaving the id permanently NULL -- the
    second is the two-shapes-in-one-column problem the no-legacy rule exists to
    stop, and DES-007 6.2 says nullable only for the window between the column
    landing and the backfill completing, which here is one transaction. So an
    unresolved name raises, the migration rolls back, and the operator is handed
    the list and the one-shot that seeds it (scripts/seed_agent_identities.py).

    It refuses BEFORE it writes anything, so a database that cannot be migrated
    is left exactly as it was rather than half-converted.

    THE NAME STAYS EVERYWHERE. sender, author, reads.agent and members.name are
    what routing, `to=` and every human reader use; the id is what segments two
    instances of one label. A design that dropped the name would break every
    `to=` on the wire (DES-007 2.3).
    """
    pending = unresolved_agent_names(conn)
    if pending:
        listed = "\n  ".join(
            f"{e['name']}: {e['messages']} messages, {e['memories']} memories, "
            f"{e['lessons']} lessons" for e in pending)
        raise BusError(
            f"identity backfill REFUSES: {len(pending)} agent name(s) in this "
            f"database have no row in `agents`, so their history cannot be "
            f"attributed to an identity without inventing one.\n  {listed}\n"
            f"Nothing was changed. Assign them once, deliberately:\n"
            f"  uv run python scripts/seed_agent_identities.py <db> --assign <user>")
    snapshot(conn, f"{db_path}.pre-v18-{time.time_ns()}.bak")
    with tx(conn):
        for table, col in (("messages", "sender_agent_id"), ("memories", "author_agent_id"),
                           ("reads", "agent_id"), ("members", "agent_id")):
            cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            if col not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} TEXT")
        # One identity per name is what pre-migration history IS (DES-007 6.3),
        # and it is enforced here rather than assumed: a name that somehow
        # carries two rows already is resolved for NEITHER of them. The first
        # version said MIN(id) "makes the choice deterministic", which is true
        # and is exactly the problem -- a deterministic guess is still a guess,
        # and this one would hand a retired identity's history to whichever id
        # sorted first (architect, msg 9000). Ambiguous rows keep NULL, which
        # is "not yet attributed" and recoverable; a wrong id is a false record.
        for table, col, namecol in (("messages", "sender_agent_id", "sender"),
                                    ("memories", "author_agent_id", "author"),
                                    ("reads", "agent_id", "agent"),
                                    ("members", "agent_id", "name")):
            # A table already at a later shape (v28 keys reads/members on the
            # identity and has no name column) has nothing here to resolve.
            if namecol not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
                continue
            conn.execute(f"""
                UPDATE {table} SET {col} =
                  (SELECT a.id FROM agents a WHERE a.name = {table}.{namecol})
                 WHERE (SELECT count(*) FROM agents a
                         WHERE a.name = {table}.{namecol}) = 1""")
        # STATE NOTES MOVE FROM THE TOKEN TO THE IDENTITY (DES-007 4.2), and this
        # is the migration's most user-visible payoff rather than a detail: a note
        # scoped agent:<token_id> is orphaned the moment an agent is recreated,
        # because recreating mints a new token, so "recreate resumes its old
        # state" has been a claim rather than a promise. Rewritten through the
        # token's bound name; a note whose token is gone keeps its old scope,
        # because there is nothing left to resolve it through and guessing would
        # attach one agent's memory to another.
        _rescope_state_notes(conn)
        # THE RECOUNT CALLS THE REFUSAL, it does not re-spell it. The first
        # version asked the same question in fresh SQL and got a different
        # answer: the refusal excludes humans (a person is not an agent
        # identity) and the recount did not, so on the operator's database the
        # preflight passed, this counted 74 of their own messages as
        # unattributed, and the broker restart-looped on a database its own
        # preflight had just blessed. The comment sitting here claimed the two
        # read the same tables "so if they ever disagree the disagreement is the
        # bug" -- and it was, because a comment asserting agreement is not
        # agreement. One definition, called twice.
        still = unresolved_agent_names(conn)
        if still:
            raise BusError(
                f"identity backfill left {len(still)} name(s) unresolved AFTER "
                f"resolving every name it was given: "
                f"{', '.join(e['name'] for e in still)} -- refusing to stamp this "
                f"database as migrated")
        conn.execute("PRAGMA user_version=18")



def _upgrade_v18(conn, db_path):
    """v18 -> v19: rescope the state notes written into the GAP.

    v17->v18 moved state notes to agent:<agent_id> while memory_add still wrote
    agent:<token_id>. So between that deploy and this fix, every state note a
    live agent wrote landed at the OLD scope -- and the moment the readers move
    to the identity (this same commit), those notes become invisible in their
    turn. Fixing the readers without this step fixes the incident by recreating
    it one hour younger, which is the half that is easy to miss.

    Same UPDATE as v17's, deliberately: one statement, run twice, rather than a
    second spelling of the same move.
    """
    with tx(conn):
        _rescope_state_notes(conn)
        conn.execute("PRAGMA user_version=19")



def _upgrade_v19(conn, db_path):
    """v19 -> v20: repair the two halves the writers never learned.

    Both are the same shape and both were measured on the live database rather
    than reasoned about:

    STATE NOTES: v17 and v18 could only rescope a note whose minting token still
    existed, and tokens are superseded on every re-mint. 47 of 50 notes stayed at
    a dead token's scope, unreachable by anyone once the readers moved to the
    identity. Resolving through the AUTHOR fixes them, because a name outlives
    its tokens.

    SENDERS: v18 filled messages.sender_agent_id for all of history while send()
    went on inserting NULL, so every message written after that deploy was
    unattributed -- 39 within the hour. The write path is fixed in this same
    commit; this catches the gap it already produced.
    """
    with tx(conn):
        _rescope_state_notes(conn)
        # Same rule as the rescope: only an unambiguous name is resolved. A
        # message whose sender has two identities stays NULL and is counted --
        # NULL is "not yet attributed", which is recoverable; a wrong id is a
        # false record, which is not.
        conn.execute("""
            UPDATE messages SET sender_agent_id = (
                SELECT a.id FROM agents a WHERE a.name = messages.sender)
             WHERE sender_agent_id IS NULL
               AND (SELECT count(*) FROM agents a
                     WHERE a.name = messages.sender) = 1""")
        left = conn.execute("""
            SELECT count(*) FROM messages
             WHERE sender_agent_id IS NULL
               AND (SELECT count(*) FROM agents a
                     WHERE a.name = messages.sender) > 1""").fetchone()[0]
        if left:
            print(f"sender backfill: left {left} message(s) whose sender has "
                  f"more than one identity -- unattributed, for the operator")
        conn.execute("PRAGMA user_version=20")



def _upgrade_v20(conn, db_path):
    """v20 -> v21: tokens bind to the IDENTITY (DES-007 2.4 cutover).

    tokens.agent_name becomes tokens.agent_id, resolved owner-scoped: the live
    identity for (owner, name) first -- a token is a live credential, so the
    live instance is what it speaks for -- else the only identity, else REFUSE.
    Refusal rather than NULL because a bound token that loses its binding
    becomes an UNBOUND one, and that is a security downgrade performed silently
    by a migration: an unbound token's X-Agent is self-asserted. The name column
    is DROPPED in the same step -- clean cutover, no dual-name check for the
    next reader to find. token_audit keeps its historical name and gains
    agent_id alongside, because audit reads the way the world read at the time.
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(tokens)")}
    if "agent_name" not in cols:
        # Already the new shape: a fresh database, or a re-run after a partial
        # chain -- the same idempotence rule every other step follows. Only the
        # audit column and the stamp can still be owed.
        with tx(conn):
            acols = {r["name"] for r in conn.execute("PRAGMA table_info(token_audit)")}
            if "agent_id" not in acols:
                conn.execute("ALTER TABLE token_audit ADD COLUMN agent_id TEXT")
            conn.execute("PRAGMA user_version=21")
        return
    bad = conn.execute("""
        SELECT t.id, t.agent_name FROM tokens t
         WHERE t.agent_name IS NOT NULL
           AND (SELECT count(*) FROM agents a
                 WHERE a.owner_id = t.owner_id AND a.name = t.agent_name
                   AND a.retired_ns IS NULL) = 0
           AND (SELECT count(*) FROM agents a
                 WHERE a.owner_id = t.owner_id AND a.name = t.agent_name) != 1
        """).fetchall()
    if bad:
        listed = ", ".join(f"{r['id'][:8]}->{r['agent_name']}" for r in bad)
        raise BusError(
            f"tokens cutover REFUSES: {len(bad)} bound token(s) whose name "
            f"resolves to no identity or to several ({listed}). Binding them to "
            f"nothing would silently unbind a credential; seed or assign the "
            f"identities first.")
    snapshot(conn, f"{db_path}.pre-v21-{time.time_ns()}.bak")
    with tx(conn):
        if "agent_id" not in cols:
            conn.execute("ALTER TABLE tokens ADD COLUMN agent_id TEXT REFERENCES agents(id)")
        conn.execute("""
            UPDATE tokens SET agent_id = COALESCE(
                (SELECT a.id FROM agents a
                  WHERE a.owner_id = tokens.owner_id AND a.name = tokens.agent_name
                    AND a.retired_ns IS NULL),
                (SELECT a.id FROM agents a
                  WHERE a.owner_id = tokens.owner_id AND a.name = tokens.agent_name
                    AND (SELECT count(*) FROM agents b
                          WHERE b.owner_id = a.owner_id AND b.name = a.name) = 1))
             WHERE agent_name IS NOT NULL""")
        if "agent_name" in cols:
            conn.execute("ALTER TABLE tokens DROP COLUMN agent_name")
        acols = {r["name"] for r in conn.execute("PRAGMA table_info(token_audit)")}
        if "agent_id" not in acols:
            conn.execute("ALTER TABLE token_audit ADD COLUMN agent_id TEXT")
        ucols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        if "deleted_ns" not in ucols:
            conn.execute("ALTER TABLE users ADD COLUMN deleted_ns INTEGER")
        conn.execute("PRAGMA user_version=21")


def _upgrade_v21(conn, db_path):
    """v21 -> v22: token_tombstones (S2, ruling 10876). Additive -- a superseded
    credential's refusal becomes a signpost instead of a shrug. Existing rows
    cannot be backfilled (supersession deleted the hashes), so migration is the
    empty table and the signpost starts with the next supersede."""
    with tx(conn):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS token_tombstones (
                secret_hash     TEXT PRIMARY KEY,
                agent_id        TEXT NOT NULL,
                superseded_ns   INTEGER NOT NULL,
                last_refusal_ns INTEGER
            )""")
        conn.execute("PRAGMA user_version=22")


def _upgrade_v25(conn, db_path):
    """v25 -> v26 (DES-017): attachments.clip / duration_s -- a converted audio
    clip that is the message's voice; and raw_archive, the durable ledger of
    every original audio upload (s7). Additive."""
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(attachments)")}
        if "clip" not in cols:
            conn.execute("ALTER TABLE attachments ADD COLUMN clip INTEGER NOT NULL DEFAULT 0")
        if "duration_s" not in cols:
            conn.execute("ALTER TABLE attachments ADD COLUMN duration_s REAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS raw_archive (
                key         TEXT PRIMARY KEY,
                sha256      TEXT NOT NULL,
                bytes       INTEGER NOT NULL,
                mime        TEXT,
                message_id  INTEGER,
                uploader    TEXT,
                archived_ns INTEGER NOT NULL,
                tier        TEXT NOT NULL DEFAULT 'absolute-zero',
                location    TEXT
            )""")
        conn.execute("PRAGMA user_version=26")


def _lineage_head(merged, aid):
    """Follow agents.merged_into to the survivor. A fold is a chain, never a cycle
    (the tool refuses one); the bound is belt against a hand-edited row."""
    seen = 0
    while aid in merged and seen < 32:
        aid = merged[aid]
        seen += 1
    return aid


def resolve_recipient_ids(conn):
    """DES-011 s6.1(a): for every direct message, the identity its recipient
    NAME denoted at that time. Returns a report dict:
    {"resolved": {mid: aid}, "how": {rule: n}, "humans": n,
     "unresolvable": [(mid, room, name, ts_ns, why)]}.

    THE SUCCESSION CLOCK. Among the identities that ever wore the name (folded
    to their lineage heads through merged_into), the one LIVE at ts -- created
    at or before it, not yet retired -- is the holder; one owner cannot have two
    (idx_agents_live), and before the room alias existed the members key
    (room_id, name) forbade two owners' agents from sharing a bare name in a
    room, so pre-v27 history has at most one. Nobody live at ts: the last one
    created before ts (mail addressed to a name after its holder retired and
    before a successor was minted -- the successor did not exist, so it cannot
    have been meant). Nobody created yet: the earliest ever (the seeded rows
    were minted after the history they own). Two live at once, or no identity
    at all: unresolvable, LISTED with (room, name, ts), left NULL. A person is
    not an identity: a recipient naming a user (tombstones included, their name
    stays theirs by ruling 11611) is NULL and counted as a human, not a failure.

    Measured against the sender plane on the live database (2026-08-18): every
    message this clock resolves, the "who spoke as that name in that room around
    then" evidence resolves to the same id; the clock also settles the 22 the
    spans left in a gap (later holder not yet minted). One rule, from recorded
    facts, no nearest-neighbour guess.
    """
    heads = _name_lineages(conn)
    people = {r["name"] for r in conn.execute("SELECT name FROM users")}
    report = {"resolved": {}, "how": {}, "humans": 0, "unresolvable": []}
    for m in conn.execute("SELECT id, room, recipient, ts_ns FROM messages "
                          "WHERE recipient != ? ORDER BY id", (BROADCAST,)):
        name, ts = m["recipient"], m["ts_ns"]
        if name not in heads and name in people:
            report["humans"] += 1
            continue
        aid, how = _holder_at(heads.get(name, []), ts)
        if aid is None:
            report["unresolvable"].append((m["id"], m["room"], name, ts, how))
            continue
        report["resolved"][m["id"]] = aid
        report["how"][how] = report["how"].get(how, 0) + 1
    return report


def _name_lineages(conn):
    """{name: [lineage-head agents rows]} for every name an identity ever wore.
    A folded source under a DIFFERENT name (X "architect" -> Y
    "reveille-architect") still answers for mail addressed to X's name: it is
    Y's mail."""
    merged = {r["id"]: r["merged_into"] for r in
              conn.execute("SELECT id, merged_into FROM agents WHERE merged_into IS NOT NULL")}
    agents = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM agents")}
    heads = {}
    for a in agents.values():
        head = agents.get(_lineage_head(merged, a["id"]), a)
        heads.setdefault(a["name"], {})[head["id"]] = head
    return {name: list(by_id.values()) for name, by_id in heads.items()}


def _holder_at(rows, ts):
    """The succession clock (see resolve_recipient_ids): which of `rows` (lineage
    heads of one name) held the name at `ts`. Returns (agent_id, rule) or
    (None, why)."""
    if not rows:
        return None, "no identity ever held this name"
    born = [a for a in rows if a["created_ns"] <= ts]
    live = [a for a in born if a["retired_ns"] is None or a["retired_ns"] > ts]
    if len(live) == 1:
        return live[0]["id"], "live-at-ts"
    if live:
        return None, (f"{len(live)} identities live under this name at once: "
                      + ", ".join(a["id"] for a in live))
    if born:
        return max(born, key=lambda a: a["created_ns"])["id"], "last-holder"
    return min(rows, key=lambda a: a["created_ns"])["id"], "earliest"


# ---- the principal: who a row is about (DES-011 s6.1(b)) --------------------
# The DES-013 s2 speaker key IS the identity key everywhere the store keys on
# who: agent:<agents.id> for a body holding a bound token, user:<users.id> for
# a person. One form, derived from the credential by the daemon (speaker_key)
# and by join() from the token/tag -- never from a name.

def agent_principal(agent_id):
    return f"agent:{agent_id}"


def user_principal(user_id):
    return f"user:{user_id}"


def agent_of(principal):
    """The agents.id behind a principal, or None for a person / nothing."""
    return principal[6:] if principal and principal.startswith("agent:") else None


def user_of(principal):
    return principal[5:] if principal and principal.startswith("user:") else None


def _identity_name(conn, principal):
    """The identity's OWN name (agents.name / users.name), or None."""
    aid, uid = agent_of(principal), user_of(principal)
    r = None
    if aid:
        r = conn.execute("SELECT name FROM agents WHERE id=?", (aid,)).fetchone()
    elif uid:
        r = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    return r["name"] if r else None


def _owner_name(conn, principal):
    """The account name that prefixes an alias: the agent's owner, or the person."""
    aid, uid = agent_of(principal), user_of(principal)
    if aid:
        r = conn.execute("SELECT u.name FROM agents a JOIN users u ON u.id=a.owner_id "
                         "WHERE a.id=?", (aid,)).fetchone()
    else:
        r = conn.execute("SELECT name FROM users WHERE id=?", (uid,)).fetchone()
    return r["name"] if r else None


def _join_principal(conn, tag, token_id):
    """Who is joining, from the credential: a BOUND token's identity, or the
    person behind a web:<name> tag. Anything else has no identity to key a
    membership on and is refused -- an unbound token cannot act (11252)."""
    if token_id:
        r = conn.execute("SELECT agent_id FROM tokens WHERE id=?", (token_id,)).fetchone()
        if r and r["agent_id"]:
            return agent_principal(r["agent_id"])
    if (tag or "").startswith("web:"):
        u = conn.execute("SELECT id FROM users WHERE name=?", (tag[4:],)).fetchone()
        if u:
            return user_principal(u["id"])
    raise BusError("join needs an identity: a token bound to an agent, or a signed-in "
                   "person -- a name alone is not a member (DES-011 s6.1(b))")


def record_agent_name(conn, agent_id, name, from_ns):
    """One line of the rename log: `agent_id` wears `name` from `from_ns`. Every
    INSERT INTO agents calls this so the log never lags the table (the v18
    lesson: a column the writer does not fill is a backfill that rots)."""
    conn.execute("INSERT INTO agent_names(agent_id, name, from_ns) VALUES(?,?,?)",
                 (agent_id, name, from_ns))


def _upgrade_v26(conn, db_path):
    """v26 -> v27 (DES-011 s6.1(a), ruling 10983): the recipient plane learns
    the identity; the rename log and the fold record become schema.

    Three additive writes, one transaction, nothing read differently afterwards:
      - messages.recipient_agent_id, backfilled by (room, name, time) through
        resolve_recipient_ids(); what cannot be resolved is LISTED here with its
        (room, name, ts) and left NULL -- printed, never silent, never invented.
      - agent_names(agent_id, name, from_ns, to_ns) seeded from agents.name.
      - agents.merged_into set from identity-merges.jsonl beside the database
        (s7.2: the JSON line is the merge record; this column is its queryable
        home). No file = no folds = nothing to set.
    Readers cut over in (b), all in one commit; this step is rehearsed on a
    copy first: scripts/rehearse_migration.py <db>.
    """
    snapshot(conn, f"{db_path}.pre-v27-{time.time_ns()}.bak")
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(agents)")}
        if "merged_into" not in cols:
            conn.execute("ALTER TABLE agents ADD COLUMN merged_into TEXT REFERENCES agents(id)")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(messages)")}
        if "recipient_agent_id" not in cols:
            conn.execute("ALTER TABLE messages ADD COLUMN recipient_agent_id TEXT")
        _exec_script(conn, """
CREATE TABLE IF NOT EXISTS agent_names (
    agent_id TEXT NOT NULL REFERENCES agents(id),
    name     TEXT NOT NULL,
    from_ns  INTEGER NOT NULL,
    to_ns    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_agent_names_agent ON agent_names(agent_id);
CREATE INDEX IF NOT EXISTS idx_agent_names_name  ON agent_names(name);
CREATE INDEX IF NOT EXISTS idx_msg_recipient_id ON messages(recipient_agent_id);
""")
        folds = 0
        merges = os.path.join(os.path.dirname(os.path.abspath(db_path)), "identity-merges.jsonl")
        if os.path.exists(merges):
            with open(merges) as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    rec = json.loads(line)
                    for src in rec["from"]:
                        folds += conn.execute(
                            "UPDATE agents SET merged_into=? WHERE id=? AND merged_into IS NULL",
                            (rec["to"]["id"], src["id"])).rowcount
        conn.execute("DELETE FROM agent_names")
        conn.execute("INSERT INTO agent_names(agent_id, name, from_ns) "
                     "SELECT id, name, created_ns FROM agents")
        rep = resolve_recipient_ids(conn)
        conn.executemany("UPDATE messages SET recipient_agent_id=? WHERE id=?",
                         [(aid, mid) for mid, aid in rep["resolved"].items()])
        print(f"v27 recipient backfill: {len(rep['resolved'])} resolved "
              f"({', '.join(f'{k} {v}' for k, v in sorted(rep['how'].items()))}); "
              f"{rep['humans']} addressed to a person; {folds} fold(s) recorded; "
              f"{len(rep['unresolvable'])} UNRESOLVABLE")
        for mid, room, name, ts, why in rep["unresolvable"]:
            print(f"  unresolvable: message {mid} room {room} to {name!r} at {ts}: {why}")
        conn.execute("PRAGMA user_version=27")


def _upgrade_v27(conn, db_path):
    """v27 -> v28 (DES-011 s6.1(b), ruling 10983): membership and receipts key on
    the IDENTITY. members re-keyed (room_id, principal) with the room-name beside
    it, unique per room among live rows; reads re-keyed (message_id, principal).
    Both tables are rebuilt (a PRIMARY KEY cannot be altered in place).

    principal for a members row: agent:<agent_id> when the row (or its token)
    carries one; user:<id> for a web:<name> tag whose person exists; otherwise
    the succession clock at seen_ns over the name's lineage; a row that still
    resolves to nothing is DROPPED and printed (presence is a cache and the
    reaper would have taken it). Two rows landing on one (room, principal) keep
    the most recently seen; the other is printed. reads: agent_id (folded to
    its head) -> agent:; else a person's name -> user:; else the clock at
    read_ns; unresolvable rows are dropped and COUNTED -- a receipt for a name
    nobody can attribute is not a receipt. Rerunnable: a table already keyed on
    principal is left alone.
    """
    snapshot(conn, f"{db_path}.pre-v28-{time.time_ns()}.bak")
    heads = _name_lineages(conn)
    merged = {r["id"]: r["merged_into"] for r in
              conn.execute("SELECT id, merged_into FROM agents WHERE merged_into IS NOT NULL")}
    people = {r["name"]: r["id"] for r in conn.execute("SELECT name, id FROM users")}
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with tx(conn):
            mcols = {r["name"] for r in conn.execute("PRAGMA table_info(members)")}
            rcols = {r["name"] for r in conn.execute("PRAGMA table_info(reads)")}
            # Rename BOTH old tables aside before the one schema replay: the
            # replay indexes each new table's principal column, so neither may
            # still be the old shape when it runs.
            if "principal" not in mcols:
                rows = conn.execute(
                    "SELECT m.*, t.agent_id AS tok_agent FROM members m "
                    "LEFT JOIN tokens t ON t.id=m.token_id").fetchall()
                conn.execute("ALTER TABLE members RENAME TO members_v27")
            if "principal" not in rcols:
                conn.execute("ALTER TABLE reads RENAME TO reads_v27")
            _exec_script(conn, _SCHEMA)
            if "principal" not in mcols:
                kept, dropped = {}, []
                for m in rows:
                    aid = m["agent_id"] if "agent_id" in m.keys() else None
                    aid = aid or m["tok_agent"]
                    if aid:
                        pr = agent_principal(_lineage_head(merged, aid))
                    elif (m["tag"] or "").startswith("web:") and m["tag"][4:] in people:
                        pr = user_principal(people[m["tag"][4:]])
                    else:
                        hid, _why = _holder_at(heads.get(m["name"], []), m["seen_ns"])
                        pr = agent_principal(hid) if hid else None
                    if pr is None:
                        dropped.append((m["room_id"], m["name"], m["tag"]))
                        continue
                    key = (m["room_id"], pr)
                    if key in kept and kept[key]["seen_ns"] >= m["seen_ns"]:
                        dropped.append((m["room_id"], m["name"], "duplicate of "
                                        + kept[key]["name"]))
                        continue
                    kept[key] = dict(m, principal=pr)
                for (rid, pr), m in kept.items():
                    conn.execute(
                        "INSERT INTO members(room_id, principal, name, tag, url, token_id, "
                        "joined_ns, seen_ns, left_ns) VALUES(?,?,?,?,?,?,?,?,?)",
                        (rid, pr, m["name"], m["tag"], m["url"], m["token_id"],
                         m["joined_ns"], m["seen_ns"], m["left_ns"]))
                conn.execute("DROP TABLE members_v27")
                print(f"v28 members: {len(kept)} re-keyed on the identity, "
                      f"{len(dropped)} dropped")
                for rid, name, why in dropped:
                    print(f"  dropped member: room {rid} name {name!r} ({why})")
            if "principal" not in rcols:
                has_aid = "agent_id" in rcols
                sel = ("SELECT message_id, agent, read_ns" + (", agent_id" if has_aid else "")
                       + " FROM reads_v27")
                n_ok = n_drop = 0
                batch = []
                for r in conn.execute(sel):
                    aid = r["agent_id"] if has_aid else None
                    if aid:
                        pr = agent_principal(_lineage_head(merged, aid))
                    elif r["agent"] in people and r["agent"] not in heads:
                        pr = user_principal(people[r["agent"]])
                    else:
                        hid, _why = _holder_at(heads.get(r["agent"], []), r["read_ns"])
                        pr = agent_principal(hid) if hid else None
                    if pr is None:
                        n_drop += 1
                        continue
                    batch.append((r["message_id"], pr, r["read_ns"]))
                    n_ok += 1
                conn.executemany(
                    "INSERT OR IGNORE INTO reads(message_id, principal, read_ns) VALUES(?,?,?)",
                    batch)
                conn.execute("DROP TABLE reads_v27")
                print(f"v28 reads: {n_ok} receipts re-keyed on the identity, "
                      f"{n_drop} dropped (name attributable to no identity)")
                # THE SUCCESSOR'S CATCH-UP. A re-minted identity joined under a
                # name whose earlier holder had already read the room's backlog;
                # join()'s catch-up marks (INSERT OR IGNORE by name) therefore
                # wrote nothing, and now that those receipts belong to the
                # earlier holder the successor would find the whole backlog
                # unread on its next inbox() (measured on the live copy: 321 ->
                # 1617 for three re-minted agents). Give every agent membership
                # the receipts join() would have written at its own join --
                # everything in the room older than joined_ns minus the
                # catch-up window -- and nothing newer.
                caught = conn.execute("""
                    INSERT OR IGNORE INTO reads(message_id, principal, read_ns)
                    SELECT m.id, mem.principal, mem.joined_ns FROM members mem
                      JOIN messages m ON m.room = mem.room_id
                     WHERE mem.principal LIKE 'agent:%'
                       AND m.ts_ns < mem.joined_ns - ?""", (CATCHUP_NS,)).rowcount
                print(f"v28 reads: {caught} catch-up receipt(s) written for re-keyed "
                      f"memberships (what join() marks at arrival)")
            bad = conn.execute("PRAGMA foreign_key_check").fetchall()
            if bad:
                raise BusError(f"v28 migration left {len(bad)} FK violations")
            conn.execute("PRAGMA user_version=28")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _upgrade_v28(conn, db_path):
    """v28 -> v29 (DES-018 slice 1): identities, oidc_state, identity_audit.
    Additive: three empty tables; nothing existing is read or rewritten."""
    with tx(conn):
        _exec_script(conn, """
CREATE TABLE IF NOT EXISTS identities (
    provider       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    user_id        TEXT NOT NULL REFERENCES users(id),
    email          TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    display_name   TEXT,
    avatar_url     TEXT,
    raw_profile    TEXT,
    created_ns     INTEGER NOT NULL,
    last_login_ns  INTEGER,
    PRIMARY KEY (provider, subject)
);
CREATE INDEX IF NOT EXISTS idx_identities_user  ON identities(user_id);
CREATE INDEX IF NOT EXISTS idx_identities_email ON identities(email);
CREATE TABLE IF NOT EXISTS oidc_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    expires_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS identity_audit (
    id       INTEGER PRIMARY KEY,
    action   TEXT NOT NULL CHECK (action IN ('signup','link','unlink',
                                             'request','approve','deny','invite')),
    provider TEXT NOT NULL,
    subject  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    actor    TEXT NOT NULL,
    ts_ns    INTEGER NOT NULL
);
""")
        conn.execute("PRAGMA user_version=29")


def _upgrade_v29(conn, db_path):
    """v29 -> v30 (DES-018 s6 amendment): signup_requests + invites, and
    identity_audit's action CHECK widened to the four new verbs (request,
    approve, deny, invite). The CHECK is the one NON-additive part: sqlite
    cannot alter a constraint, so the table is rebuilt in the same
    transaction -- rows copied verbatim, ids preserved."""
    with tx(conn):
        _exec_script(conn, """
ALTER TABLE identity_audit RENAME TO identity_audit_old;
CREATE TABLE identity_audit (
    id       INTEGER PRIMARY KEY,
    action   TEXT NOT NULL CHECK (action IN ('signup','link','unlink',
                                             'request','approve','deny','invite')),
    provider TEXT NOT NULL,
    subject  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    actor    TEXT NOT NULL,
    ts_ns    INTEGER NOT NULL
);
INSERT INTO identity_audit(id, action, provider, subject, user_id, actor, ts_ns)
  SELECT id, action, provider, subject, user_id, actor, ts_ns FROM identity_audit_old;
DROP TABLE identity_audit_old;
-- DES-018 s6 amendment (ruling 11709): a stranger's REQUEST is not a user.
-- No user row exists until an admin approves, so a pending request reserves
-- no name, holds no session and cannot be a half-user anywhere else.
CREATE TABLE IF NOT EXISTS signup_requests (
    provider       TEXT NOT NULL,
    subject        TEXT NOT NULL,
    email          TEXT,
    email_verified INTEGER NOT NULL DEFAULT 0,
    display_name   TEXT,
    avatar_url     TEXT,
    login          TEXT,
    note           TEXT,
    state          TEXT NOT NULL DEFAULT 'pending'
                   CHECK (state IN ('pending','denied')),
    requested_ns   INTEGER NOT NULL,
    decided_ns     INTEGER,
    decided_by     TEXT,
    PRIMARY KEY (provider, subject)
);
-- One-time invite codes. The code itself is shown ONCE at creation and stored
-- only as a hash -- same discipline as a token: a leaked database is not a
-- pile of working invitations.
CREATE TABLE IF NOT EXISTS invites (
    code_hash  TEXT PRIMARY KEY,
    created_by TEXT NOT NULL,
    created_ns INTEGER NOT NULL,
    note       TEXT NOT NULL DEFAULT '',
    used_by    TEXT,
    used_ns    INTEGER
);
""")
        conn.execute("PRAGMA user_version=30")


def _upgrade_v30(conn, db_path):
    """v30 -> v31 (EPIC-001 #4): room_audit's action CHECK takes 'adopt'.
    sqlite cannot alter a constraint, so the table is rebuilt in the same
    transaction -- rows copied verbatim, ids preserved."""
    with tx(conn):
        _exec_script(conn, """
ALTER TABLE room_audit RENAME TO room_audit_old;
CREATE TABLE room_audit (
    id      INTEGER PRIMARY KEY,
    room_id TEXT NOT NULL,
    action  TEXT NOT NULL CHECK (action IN ('invite','remove','adopt')),
    actor   TEXT NOT NULL,
    subject TEXT NOT NULL,
    ts_ns   INTEGER NOT NULL
);
INSERT INTO room_audit(id, room_id, action, actor, subject, ts_ns)
  SELECT id, room_id, action, actor, subject, ts_ns FROM room_audit_old;
DROP TABLE room_audit_old;
CREATE INDEX IF NOT EXISTS idx_roomaudit_room ON room_audit(room_id);
""")
        conn.execute("PRAGMA user_version=31")


def _upgrade_v32(conn, db_path):
    """v32 -> v33 (DES-012 item 8): visits. Additive: one empty table, nothing
    existing is read or rewritten. No broker has ever held a visit, so there is
    nothing to backfill -- the first row is written by the first request."""
    with tx(conn):
        _exec_script(conn, """-- DES-012: A VISIT IS A BODY SWAP. One row per visit, from the ask to the end
-- (s8): a visit is a feature, and a feature earns its schema. Nothing here is
-- a credential -- `token_id` names the mint the accept authorised, and the
-- secret it minted was answered to the host's screen once and never stored.
CREATE TABLE IF NOT EXISTS visits (
    id           TEXT PRIMARY KEY,
    agent_id     TEXT NOT NULL,
    owner_id     TEXT NOT NULL,          -- the agent's owner; NEVER moves (s4)
    host_id      TEXT NOT NULL,          -- the harbor: a host, not an owner
    host_machine TEXT NOT NULL DEFAULT '',
    rooms        TEXT NOT NULL,          -- json list of room ids: the visit's whole reach
    direction    TEXT NOT NULL CHECK (direction IN ('pull','push')),
    coordinate   TEXT NOT NULL DEFAULT '',   -- repo (+ SHA) the body checks out
    requested_by TEXT NOT NULL,          -- the human who asked; the OTHER one decides
    requested_ns INTEGER NOT NULL,
    expires_ns   INTEGER NOT NULL,       -- the REQUEST expires; the visit does not (s11.2)
    decided_ns   INTEGER,
    decision     TEXT CHECK (decision IN ('accept','reject')),
    body         TEXT CHECK (body IN ('container','native')),
    token_id     TEXT,                   -- the one mint this accept authorises
    arrived_ns   INTEGER,                -- stamped by the visiting body's first join()
    ended_ns     INTEGER,
    ended_by     TEXT CHECK (ended_by IN ('owner','host','agent'))
);
""")
        conn.execute("PRAGMA user_version=33")


def _upgrade_v31(conn, db_path):
    """v31 -> v32 (EPIC-001 #6): room_seen, one high-water mark per person and
    room. Additive: one empty table, nothing existing is read or rewritten. An
    absent row means "never opened", and the count starts from the room's
    oldest message -- which is what a new person sees."""
    with tx(conn):
        _exec_script(conn, """
-- EPIC-001 #6: how far a PERSON has read in each room. Agents have read
-- receipts per message (they must ack what was addressed to them); a person
-- reads a room, so one high-water mark per (principal, room) is the whole
-- story -- and it stays one row however long the room gets.
CREATE TABLE IF NOT EXISTS room_seen (
    principal   TEXT NOT NULL,
    room_id     TEXT NOT NULL,
    last_msg_id INTEGER NOT NULL,
    ts_ns       INTEGER NOT NULL,
    PRIMARY KEY (principal, room_id)
);
""")
        conn.execute("PRAGMA user_version=32")


def _upgrade_v24(conn, db_path):
    """v24 -> v25: voices.personal -- a voice that exists only for its uploader
    (ruling 11155). Additive column; existing voices are bank voices."""
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(voices)")}
        if "personal" not in cols:
            conn.execute("ALTER TABLE voices ADD COLUMN personal INTEGER NOT NULL DEFAULT 0")
        conn.execute("PRAGMA user_version=25")


def _upgrade_v23(conn, db_path):
    """v23 -> v24: voices.sample -- the line a bank voice reads on audition,
    kept beside the persona (operator, eval box). Additive column."""
    with tx(conn):
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(voices)")}
        if "sample" not in cols:            # no ADD COLUMN IF NOT EXISTS in sqlite
            conn.execute("ALTER TABLE voices ADD COLUMN sample TEXT NOT NULL DEFAULT ''")
        conn.execute("PRAGMA user_version=24")


def _upgrade_v22(conn, db_path):
    """v22 -> v23: voices, voice_assignments, scripts (DES-013 slice 1). Additive:
    three empty tables; nothing existing is read or rewritten."""
    with tx(conn):
        _exec_script(conn, _VOICES_SCHEMA)
        conn.execute("PRAGMA user_version=23")


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
            # NAME COLLISION ACROSS ERAS: v0's `agents` was the presence/member
            # cache; DES-007's `agents` is the identity table. CREATE IF NOT
            # EXISTS would silently keep the v0 shape and then fail building an
            # index on a column it does not have. Rename the old one out of the
            # way first -- it is read twice below and dropped, and it must not be
            # mistaken for the new table by anything in between.
            conn.execute("ALTER TABLE agents RENAME TO agents_v0")
            # v0 receipts are catch-up marks keyed on a NAME; there is no identity
            # in a v0 database to key them to, and nothing reads them (v28 keys
            # reads on the principal). Dropped; the replay lays the new table.
            conn.execute("DROP TABLE reads")
            # _SCHEMA indexes the identity columns of `messages`; the v0 table
            # predates them, so they are added here (empty) before the replay and
            # carried by messages_new below.
            for col in ("sender_agent_id", "recipient_agent_id"):
                conn.execute(f"ALTER TABLE messages ADD COLUMN {col} TEXT")
            _exec_script(conn, _SCHEMA)        # adds the new tables; messages untouched
            now = time.time_ns()
            legacy = [r["room"] for r in conn.execute(
                "SELECT room FROM messages UNION SELECT room FROM agents_v0")]
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
                    sender_agent_id    TEXT,
                    recipient_agent_id TEXT,
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
            conn.execute("DROP TABLE agents_v0")
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


# THE CHAIN, as data. One entry per version that has a step; migrate() runs the one
# for the version the database is AT and re-reads. Adding a step is adding a line
# here and stamping its own target inside it -- there is no list of arms to also
# remember to extend, which is what made the v9-v13 arms short of _upgrade_v15 with
# nothing able to say so. v1 has no entry (it never shipped) and v6 is reachable
# only from v5's rebuild; the loop steps over a gap by stamping forward one.
_UPGRADES = {v: f"_upgrade_v{v}" for v in
             (0, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
              21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31, 32)}

# The versions with NO step, named rather than implied. The loop steps over a
# missing entry by stamping forward one, which is correct for a version that
# never had work to do and WRONG for one whose step was forgotten -- and the two
# are indistinguishable from inside the loop. So the intended gaps are written
# down here and gated against the table: a SCHEMA_VERSION bump that forgets its
# entry fails a test instead of silently skipping a migration on a real database.
# v1 is the only one: it never shipped.
_UPGRADE_GAPS = frozenset({1})


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
        # The name may be held by a TOMBSTONE (ruling 8938 / 11607): a deleted
        # account keeps its row and its name for the history it owns, so the
        # refusal says which it is rather than a bare "already exists".
        held = conn.execute("SELECT deleted_ns FROM users WHERE name=?", (name,)).fetchone()
        if held is not None and held["deleted_ns"]:
            raise BusError(f"name {name!r} is taken by a deleted account -- the tombstone "
                           f"keeps it for the history it owns")
        raise BusError(f"user {name!r} already exists")
    return {"id": uid, "name": name, "role": role, "created_ns": now}


def live_user(conn, user_id):
    """The users row for an ACCOUNT, or NotFound: a tombstone (deleted_ns set)
    is not a user anyone can act on (11607) -- delete, role, password reset all
    answer 404 "user deleted" through this one check."""
    r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if r is None:
        raise NotFound(f"no such user {user_id}")
    if r["deleted_ns"]:
        raise NotFound("user deleted")
    return r


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
    if not r:
        return None
    if r["deleted_ns"]:
        # Named refusal, not "bad password": telling a deleted account its
        # password is wrong sends a person to reset a credential that no longer
        # exists (ruling 8938's own consequence list).
        raise AuthError("this account was deleted")
    if not verify_password(password, r["pw_hash"]):
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
    """The ACCOUNTS -- not the tombstones (operator 11606: "bill" deleted and
    confirmed, still listed with make-admin/reset/delete beside him). A deleted
    user is a tombstone by ruling 8938: the row stays as the referent for the
    history it owns, credentials wiped, and it is not an account anyone can act
    on. The Users tab lists what can be acted on."""
    return [{"id": r["id"], "name": r["name"], "role": r["role"],
             "created_ns": r["created_ns"]}
            for r in conn.execute("SELECT * FROM users WHERE deleted_ns IS NULL ORDER BY name")]


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
    # Tombstoned admins do not count: a deleted account cannot log in, so an
    # "admin" that exists only as a referent would satisfy the last-admin guard
    # while leaving nobody who can actually administer.
    return conn.execute("SELECT count(*) c FROM users WHERE role='admin' "
                        "AND deleted_ns IS NULL").fetchone()["c"]


def _is_admin(conn, user_id):
    r = conn.execute("SELECT role FROM users WHERE id=?", (user_id,)).fetchone()
    return bool(r and r["role"] == "admin")


def user_history(conn, user_id):
    """What CITES this person. A users row is a REFERENT only while something
    points at it (ruling 11732), so this is the exact list the delete verb
    branches on -- counted, never guessed.

    Deliberately NOT counted, because the SYSTEM wrote them, not the person:
    read receipts (join() stamps a catch-up receipt for every message older
    than the join -- two accounts that only ever signed in carried 10145 each,
    measured on live, and were tombstoned for it), membership and presence rows
    (derived from the credential, reaped on their own), room invitations, and
    identities (a door is a credential, the same class as pw_hash). All of
    those are deleted with the row; none of them is a citation of the person."""
    def q(sql, *args):
        return conn.execute(sql, args or (user_id,)).fetchone()[0]

    row = conn.execute("SELECT name FROM users WHERE id=?", (user_id,)).fetchone()
    name = row["name"] if row else ""
    return {
        # A person's send writes sender = the ROOM-NAME they wear and leaves
        # sender_agent_id NULL; their agents' sends carry the agent id.
        "messages": q("SELECT count(*) FROM messages WHERE sender_agent_id IN "
                      "(SELECT id FROM agents WHERE owner_id=?)")
                  + q("SELECT count(*) FROM messages WHERE sender=? "
                      "AND sender_agent_id IS NULL", name),
        # BEING ADDRESSED IS A CITATION (11611, architect on #108): a message
        # that says "to: dmorse" refers to this person as surely as one they
        # wrote, and freeing the name would re-point it at someone else.
        "addressed": q("SELECT count(*) FROM messages WHERE recipient_agent_id IN "
                       "(SELECT id FROM agents WHERE owner_id=?)")
                   + q("SELECT count(*) FROM messages WHERE recipient=? "
                       "AND recipient_agent_id IS NULL", name),
        "agents": q("SELECT count(*) FROM agents WHERE owner_id=?"),
        "tokens": q("SELECT count(*) FROM tokens WHERE owner_id=?"),
        "rooms": q("SELECT count(*) FROM rooms WHERE owner_id=?"),
        "memories": q("SELECT count(*) FROM memories WHERE author=?", name),
    }


def delete_user(conn, user_id):
    """Delete a person. Two outcomes, decided by whether anything REFERS to
    them (ruling 11732) -- returns "removed" or "tombstoned".

    ZERO history (no messages, agents, tokens, rooms, memberships, receipts,
    doors, memories): the row is HARD deleted and the name is free again.
    Nothing cites it, so nothing is left dangling -- this is the account that
    was created and never used, and reserving its name forever is the bug.

    ANY history: TOMBSTONE, unchanged (rulings 8938 / 11611). The users row
    stays with deleted_ns stamped, credentials wiped, sessions destroyed,
    tokens revoked, agents released, rooms ownerless. Hard delete there would
    destroy the referent while every message still carried the claim -- the
    citation defect one plane up. The row is the referent, not the account."""
    # Counted BEFORE anything is wiped: the delete itself drops tokens,
    # memberships and room_members, so reading after would call every account
    # historyless.
    history = user_history(conn, user_id)
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
        # Their memberships die with them (the token_rooms rows those memberships
        # justified are already gone above); room_audit keeps the history.
        conn.execute("DELETE FROM room_members WHERE user_id=?", (user_id,))
        now = time.time_ns()
        # Agents they owned are RELEASED, not orphaned: the name is freeable by
        # an admin, the history stays attributed to the identity, and the owner
        # lookup still resolves -- which is the gate's red line for hard delete.
        conn.execute("UPDATE agents SET retired_ns=COALESCE(retired_ns, ?), "
                     "released_ns=?, released_by='account-deletion' "
                     "WHERE owner_id=?", (now, now, user_id))
        if not any(history.values()):
            # Nothing cites this person: the row goes, the name is free, and
            # the bookkeeping the system wrote for them goes with it.
            principal = user_principal(user_id)
            conn.execute("DELETE FROM identities WHERE user_id=?", (user_id,))
            conn.execute("DELETE FROM reads WHERE principal=?", (principal,))
            conn.execute("DELETE FROM members WHERE principal=?", (principal,))
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            return "removed"
        # The hash is REPLACED, not emptied: an empty pw_hash is one bad
        # verify_password edge from matching an empty password.
        conn.execute("UPDATE users SET deleted_ns=?, pw_hash='!deleted' WHERE id=?",
                     (now, user_id))
    return "tombstoned"


# ---- sessions ----------------------------------------------------------------

def password_only_users(conn):
    """Live people whose ONLY way in is a password. Closing the password door
    (DES-018 s10 slice 2) locks exactly these out, so the close names them
    instead of discovering them at their next sign-in."""
    return [r["name"] for r in conn.execute(
        "SELECT u.name FROM users u WHERE u.deleted_ns IS NULL "
        "AND u.pw_hash LIKE 'scrypt$%' "
        "AND NOT EXISTS (SELECT 1 FROM identities i WHERE i.user_id=u.id) "
        "ORDER BY u.name")]


def free_tombstone(conn, user_id, actor="admin"):
    """An admin frees a RESERVED name (ruling 11611 follow-on). Only a
    tombstone with nothing citing it may go -- one that any message, agent,
    token, room or memory refers to keeps its referent, always."""
    row = conn.execute("SELECT id, name, deleted_ns FROM users WHERE id=?", (user_id,)).fetchone()
    if not row:
        raise NotFound("no such user")
    if not row["deleted_ns"]:
        raise BusError("that account is live -- delete it first")
    history = user_history(conn, user_id)
    if any(history.values()):
        cited = ", ".join(f"{k}={v}" for k, v in history.items() if v)
        raise BusError(f"{row['name']} is cited by {cited} -- the name stays reserved")
    principal = user_principal(user_id)
    with tx(conn):
        conn.execute("DELETE FROM identities WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM reads WHERE principal=?", (principal,))
        conn.execute("DELETE FROM members WHERE principal=?", (principal,))
        conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    return row["name"]


def tombstones(conn):
    """Deleted accounts whose names are still reserved, with what cites each --
    an empty `cited` means an admin may free the name."""
    out = []
    for r in conn.execute("SELECT id, name, deleted_ns FROM users WHERE deleted_ns IS NOT NULL "
                          "ORDER BY deleted_ns DESC"):
        h = user_history(conn, r["id"])
        out.append({"id": r["id"], "name": r["name"], "deleted_ns": r["deleted_ns"],
                    "cited": {k: v for k, v in h.items() if v}})
    return out


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


def rotate_session(conn, old_secret, user_id):
    """A login issues a FRESH session id and kills the one the browser carried
    (fixation, DES-018 s7): whatever cookie a page held before authenticating
    never becomes the authenticated one."""
    if old_secret:
        delete_session(conn, old_secret)
    return create_session(conn, user_id)


# ---- DES-018: doors (federated identities) -----------------------------------

OIDC_PW = "!oidc"          # never a valid hash: a federated-only account has no password door


def identity_get(conn, provider, subject):
    r = conn.execute("SELECT * FROM identities WHERE provider=? AND subject=?",
                     (provider, str(subject))).fetchone()
    return dict(r) if r else None


def identities_of(conn, user_id):
    return [dict(r) for r in conn.execute(
        "SELECT provider, subject, email, email_verified, display_name, avatar_url, "
        "created_ns, last_login_ns FROM identities WHERE user_id=? ORDER BY provider",
        (user_id,))]


def _identity_audit(conn, action, provider, subject, user_id, actor):
    conn.execute("INSERT INTO identity_audit(action, provider, subject, user_id, actor, ts_ns) "
                 "VALUES(?,?,?,?,?,?)", (action, provider, str(subject), user_id, actor,
                                         time.time_ns()))


def _email(value):
    """One case for every email the store keeps or compares: providers assert
    Ada@Example.com and ada@example.com for the same mailbox, and the s5.2
    match must not depend on which spelling came first."""
    e = (value or "").strip().lower()
    return e or None


def _identity_upsert(conn, provider, profile, user_id, now):
    conn.execute(
        "INSERT INTO identities(provider, subject, user_id, email, email_verified, "
        "display_name, avatar_url, raw_profile, created_ns, last_login_ns) "
        "VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(provider, subject) DO UPDATE SET email=excluded.email, "
        "email_verified=excluded.email_verified, display_name=excluded.display_name, "
        "avatar_url=excluded.avatar_url, raw_profile=excluded.raw_profile, "
        "last_login_ns=excluded.last_login_ns",
        (provider, str(profile["subject"]), user_id, _email(profile.get("email")),
         1 if profile.get("email_verified") else 0, profile.get("display_name"),
         profile.get("avatar_url"), json.dumps(profile.get("raw") or {}), now, now))


def link_identity(conn, provider, profile, user_id, actor):
    """s5.1: a SIGNED-IN person adds a door. Both sides proven (session +
    provider). A door already attached to ANOTHER account is refused: it is
    that person's credential, and moving it is how one person takes another's
    login."""
    cur = identity_get(conn, provider, profile["subject"])
    if cur and cur["user_id"] != user_id:
        raise BusError(f"this {provider} account is already the door of another user here")
    now = time.time_ns()
    with tx(conn):
        _identity_upsert(conn, provider, profile, user_id, now)
        if not cur:
            _identity_audit(conn, "link", provider, profile["subject"], user_id, actor)
    return identity_get(conn, provider, profile["subject"])


def unlink_identity(conn, user_id, provider, subject, actor, admin=False):
    """A person may remove a door if another way in remains (another door, or a
    working password); nobody may lock themselves out. Admin may unlink any."""
    cur = identity_get(conn, provider, subject)
    if not cur or (cur["user_id"] != user_id and not admin):
        raise NotFound("no such door")
    owner = cur["user_id"]
    if not admin:
        others = conn.execute("SELECT count(*) FROM identities WHERE user_id=? "
                              "AND NOT (provider=? AND subject=?)",
                              (owner, provider, str(subject))).fetchone()[0]
        pw = conn.execute("SELECT pw_hash FROM users WHERE id=?", (owner,)).fetchone()["pw_hash"]
        if not others and not (pw or "").startswith("scrypt$"):
            raise BusError("this is your only way in -- add another door before removing it")
    with tx(conn):
        conn.execute("DELETE FROM identities WHERE provider=? AND subject=?",
                     (provider, str(subject)))
        _identity_audit(conn, "unlink", provider, subject, owner, actor)


def users_with_verified_email(conn, email):
    """Live users holding a door whose VERIFIED email is exactly this one -- the
    only kind of email match that may attach a door at login (s5.2)."""
    email = _email(email)
    if not email:
        return []
    return [r["user_id"] for r in conn.execute(
        "SELECT DISTINCT i.user_id FROM identities i JOIN users u ON u.id=i.user_id "
        "WHERE i.email=? AND i.email_verified=1 AND u.deleted_ns IS NULL", (email,))]


def derive_user_name(conn, hint):
    """The zero-screen name (s6): the provider's handle / email local-part,
    lowercased, mapped through NAME_RE (illegal runs -> '-'), collision -> -2, -3."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "-", (hint or "").split("@")[0].lower()).strip("-_")[:60]
    base = base or "user"
    name, n = base, 1
    while conn.execute("SELECT 1 FROM users WHERE name=?", (name,)).fetchone():
        n += 1
        name = f"{base}-{n}"
    return name


SIGNUP_POLICIES = ("open", "request", "closed")   # or a domain list; see below


def signup_allowed(policy, email, verified):
    """REVEILLE_SIGNUP: 'open' | 'request' | 'closed' | 'dom1,dom2' (only
    VERIFIED emails in those domains may create accounts). Returns (ok, why).

    `request` never creates an account here: federated_login turns it into a
    signup_requests row before this is consulted, so reaching it means the
    caller asked whether a bare signup may proceed -- it may not."""
    policy = (policy or "open").strip().lower()
    if policy == "open":
        return True, ""
    if policy == "request":
        return False, "REQUEST"
    if policy == "closed":
        return False, "signup is closed on this broker -- ask an admin for an invite"
    domains = {d.strip().lstrip("@") for d in policy.split(",") if d.strip()}
    if not verified or not email or email.rsplit("@", 1)[-1].lower() not in domains:
        return False, ("signup here needs a verified email in: " + ", ".join(sorted(domains)))
    return True, ""


# ---- DES-018 s6 amendment (ruling 11709): request pool + one-time invites -----

REQUESTED = "REQUESTED"      # federated_login's answer when a request was filed
NOTE_MAX = 280


def request_get(conn, provider, subject):
    r = conn.execute("SELECT * FROM signup_requests WHERE provider=? AND subject=?",
                     (provider, str(subject))).fetchone()
    return dict(r) if r else None


def requests_list(conn, state="pending"):
    """The admin's queue. `state` None lists every request, decided included."""
    sql = "SELECT * FROM signup_requests"
    args = []
    if state:
        sql += " WHERE state=?"
        args = [state]
    return [dict(r) for r in conn.execute(sql + " ORDER BY requested_ns DESC", args)]


def request_file(conn, provider, profile, note="", actor="door"):
    """Record a stranger's ask. Idempotent per door: a second visit refreshes
    the profile and leaves the state alone -- a pending row stays pending, a
    denied row stays denied (the page reads identically either way, so a
    stranger cannot learn which they are)."""
    subject = str(profile["subject"])
    now = time.time_ns()
    cur = request_get(conn, provider, subject)
    with tx(conn):
        if cur:
            conn.execute(
                "UPDATE signup_requests SET email=?, email_verified=?, display_name=?, "
                "avatar_url=?, login=? WHERE provider=? AND subject=?",
                (_email(profile.get("email")), 1 if profile.get("email_verified") else 0,
                 profile.get("display_name"), profile.get("avatar_url"),
                 profile.get("login"), provider, subject))
        else:
            conn.execute(
                "INSERT INTO signup_requests(provider, subject, email, email_verified, "
                "display_name, avatar_url, login, note, state, requested_ns) "
                "VALUES(?,?,?,?,?,?,?,?, 'pending', ?)",
                (provider, subject, _email(profile.get("email")),
                 1 if profile.get("email_verified") else 0, profile.get("display_name"),
                 profile.get("avatar_url"), profile.get("login"),
                 (note or "")[:NOTE_MAX], now))
            _identity_audit(conn, "request", provider, subject, "", actor)
    return request_get(conn, provider, subject)


def request_approve(conn, provider, subject, actor):
    """One transaction: the account, its first door, the audit line, and the
    request row gone. Nothing half-made if any step fails."""
    req = request_get(conn, provider, subject)
    if not req:
        raise NotFound("no such request")
    if identity_get(conn, provider, subject):
        raise BusError("that door already belongs to an account")
    profile = {"subject": subject, "email": req["email"],
               "email_verified": req["email_verified"], "display_name": req["display_name"],
               "avatar_url": req["avatar_url"], "login": req["login"], "raw": {}}
    hint = req["login"] or req["email"] or req["display_name"] or provider
    name = derive_user_name(conn, hint)
    uid = _uuid()
    now = time.time_ns()
    with tx(conn):
        conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) VALUES(?,?,?,?,?)",
                     (uid, name, OIDC_PW, "user", now))
        _identity_upsert(conn, provider, profile, uid, now)
        _identity_audit(conn, "approve", provider, subject, uid, actor)
        conn.execute("DELETE FROM signup_requests WHERE provider=? AND subject=?",
                     (provider, str(subject)))
    return {"id": uid, "name": name, "role": "user"}


def request_deny(conn, provider, subject, actor):
    """Denied rows are KEPT: the next visit through that door shows the same
    neutral page instead of filing a fresh ask, so a denial is quiet and does
    not become a queue the admin has to re-decide. request_forget() erases."""
    if not request_get(conn, provider, subject):
        raise NotFound("no such request")
    with tx(conn):
        conn.execute("UPDATE signup_requests SET state='denied', decided_ns=?, decided_by=? "
                     "WHERE provider=? AND subject=?",
                     (time.time_ns(), actor, provider, str(subject)))
        _identity_audit(conn, "deny", provider, subject, "", actor)


def request_undeny(conn, provider, subject, actor):
    """A denial reversed: back into the pending queue, to be approved."""
    if not request_get(conn, provider, subject):
        raise NotFound("no such request")
    conn.execute("UPDATE signup_requests SET state='pending', decided_ns=NULL, "
                 "decided_by=? WHERE provider=? AND subject=?",
                 (actor, provider, str(subject)))


def request_forget(conn, provider, subject, actor):
    """Erase the row. The same door may then ask again from scratch."""
    if not request_get(conn, provider, subject):
        raise NotFound("no such request")
    conn.execute("DELETE FROM signup_requests WHERE provider=? AND subject=?",
                 (provider, str(subject)))


def invite_create(conn, created_by, note=""):
    """Mint a one-time code. The CODE is returned once and never stored -- only
    its hash -- so this answer is the only copy that will ever exist."""
    code = secrets.token_urlsafe(16)
    conn.execute("INSERT INTO invites(code_hash, created_by, created_ns, note) VALUES(?,?,?,?)",
                 (_sha(code), created_by, time.time_ns(), (note or "")[:NOTE_MAX]))
    return {"code": code, "note": (note or "")[:NOTE_MAX]}


def invite_list(conn):
    """Open codes first, then used ones. The code itself is not here to show."""
    rows = [dict(r) for r in conn.execute(
        "SELECT i.code_hash, i.created_by, i.created_ns, i.note, i.used_by, i.used_ns, "
        "cb.name AS created_by_name, ub.name AS used_by_name FROM invites i "
        "LEFT JOIN users cb ON cb.id=i.created_by LEFT JOIN users ub ON ub.id=i.used_by "
        "ORDER BY (i.used_ns IS NOT NULL), i.created_ns DESC")]
    return rows


def invite_valid(conn, code):
    """Is this code an UNUSED invite? Returns the row or None. Never raises on a
    wrong code: a bad code and no code are the same thing to a stranger."""
    if not code:
        return None
    r = conn.execute("SELECT * FROM invites WHERE code_hash=? AND used_ns IS NULL",
                     (_sha(code),)).fetchone()
    return dict(r) if r else None


def invite_revoke(conn, code_hash, actor):
    """Withdraw an unused code. A used one stays as the record of who came in."""
    n = conn.execute("DELETE FROM invites WHERE code_hash=? AND used_ns IS NULL",
                     (code_hash,)).rowcount
    if not n:
        raise NotFound("no such open invite")


def _invite_consume(conn, code, user_id, now):
    """Burn the code inside the caller's transaction. The UPDATE ... WHERE
    used_ns IS NULL is the race gate: two callbacks redeeming one code at the
    same instant, only one row changes and the loser is refused."""
    n = conn.execute("UPDATE invites SET used_by=?, used_ns=? WHERE code_hash=? "
                     "AND used_ns IS NULL", (user_id, now, _sha(code))).rowcount
    if not n:
        raise AuthError("that invite code has already been used")


def federated_login(conn, provider, profile, signup_policy="open", actor="web",
                    invite=None, note=""):
    """s5/s6, the one rule for a NOT-signed-in callback. Returns
    {"user": {...}, "how": known|linked|signup|invite, "banner": name-or-None},
    or the string REQUESTED when the policy is `request` and the stranger's ask
    was filed instead (the caller shows the neutral page; no session is made).

    known: (provider, subject) is a door -> that person (a tombstoned person is
    refused: their doors do not become someone else's). linked: unknown door,
    provider asserts a VERIFIED email, exactly ONE live user holds a door with
    that verified email -> attach + sign in (Ory rule; defeats pre-hijack).
    Otherwise a NEW account under the signup policy -- unless a user with that
    (even unverified) email exists, in which case "use your other door" (a link,
    not a dead end): merging on an unverified or ambiguous email is the hijack.
    """
    subject = str(profile["subject"])
    now = time.time_ns()
    cur = identity_get(conn, provider, subject)
    if cur:
        u = conn.execute("SELECT id, name, role, deleted_ns FROM users WHERE id=?",
                         (cur["user_id"],)).fetchone()
        if u["deleted_ns"]:
            raise AuthError("this account was deleted")
        with tx(conn):
            _identity_upsert(conn, provider, profile, u["id"], now)
        return {"user": {"id": u["id"], "name": u["name"], "role": u["role"]},
                "how": "known", "banner": None}
    email, verified = _email(profile.get("email")), bool(profile.get("email_verified"))
    matches = users_with_verified_email(conn, email) if verified else []
    if len(matches) == 1:
        u = conn.execute("SELECT id, name, role FROM users WHERE id=?", (matches[0],)).fetchone()
        with tx(conn):
            _identity_upsert(conn, provider, profile, u["id"], now)
            _identity_audit(conn, "link", provider, subject, u["id"], actor)
        return {"user": dict(u), "how": "linked", "banner": None}
    other = conn.execute(
        "SELECT count(DISTINCT i.user_id) FROM identities i JOIN users u ON u.id=i.user_id "
        "WHERE i.email=? AND u.deleted_ns IS NULL", (email,)).fetchone()[0] if email else 0
    if len(matches) > 1 or other:
        raise AuthError(f"an account with {email} already exists here -- sign in with the "
                        f"door you used before, then add {provider} in your profile")
    # A valid invite is an admin's decision made in advance: it admits the
    # bearer wherever the policy would refuse, and is burned in the same
    # transaction as the account it creates. Where signup is already allowed
    # the code is not consulted at all -- under `open` nobody should spend a
    # code they did not need.
    ok, why = signup_allowed(signup_policy, email, verified)
    inv = None if ok else invite_valid(conn, invite)
    if not ok and not inv:
        if why == "REQUEST":
            request_file(conn, provider, profile, note=note, actor=actor)
            return REQUESTED
        raise AuthError(why)
    hint = profile.get("login") or email or profile.get("display_name") or provider
    name = derive_user_name(conn, hint)
    uid = _uuid()
    first = not any_users(conn)
    with tx(conn):
        conn.execute("INSERT INTO users(id, name, pw_hash, role, created_ns) VALUES(?,?,?,?,?)",
                     (uid, name, OIDC_PW, "admin" if first else "user", now))
        if first:
            conn.execute("UPDATE rooms SET owner_id=? WHERE owner_id IS NULL", (uid,))
        _identity_upsert(conn, provider, profile, uid, now)
        if inv:
            _invite_consume(conn, invite, uid, now)
        _identity_audit(conn, "invite" if inv else "signup", provider, subject, uid, actor)
        # An accepted invite settles any earlier ask through the same door.
        conn.execute("DELETE FROM signup_requests WHERE provider=? AND subject=?",
                     (provider, subject))
    return {"user": {"id": uid, "name": name, "role": "admin" if first else "user"},
            "how": "invite" if inv else "signup", "banner": name, "first_admin": first,
            "invited_by": inv["created_by"] if inv else None}


def oidc_state_get(conn, key):
    r = conn.execute("SELECT value, expires_ns FROM oidc_state WHERE key=?", (key,)).fetchone()
    if not r:
        return None
    if r["expires_ns"] < time.time_ns():
        conn.execute("DELETE FROM oidc_state WHERE key=?", (key,))
        return None
    return r["value"]


def oidc_state_set(conn, key, value, ttl_s):
    conn.execute("INSERT OR REPLACE INTO oidc_state(key, value, expires_ns) VALUES(?,?,?)",
                 (key, value, time.time_ns() + int(ttl_s * 1e9)))


def oidc_state_delete(conn, key):
    conn.execute("DELETE FROM oidc_state WHERE key=?", (key,))


def sweep_oidc_state(conn):
    return conn.execute("DELETE FROM oidc_state WHERE expires_ns < ?", (time.time_ns(),)).rowcount


# ---- tokens ------------------------------------------------------------------

def live_agent_names(conn, owner_id):
    """The owner's live agent names, sorted. The suggestion list a creation
    refusal shows -- it is the owner's own inventory, shown to the owner."""
    return [r["name"] for r in conn.execute(
        "SELECT name FROM agents WHERE owner_id=? AND retired_ns IS NULL "
        "ORDER BY name", (owner_id,))]


def create_token(conn, owner_id, label="", agent_name=None, mem_tier="state",
                 rooms=None, create=False):
    """Mint a credential; a bound one names an agent and THE MINT IS THE
    PROVISIONING EVENT (DES-008 ruling 1). A native agent has no container and
    the launcher never sees it, so minting a bound token for a name inserts that
    name's agents row -- owner = the minting user -- when no live one exists.
    One identity path, container or not.

    The caller's vocabulary stays the NAME (it is what travels the wire); what
    the token stores is the IDENTITY (DES-007 2.4), so a declined resurrect
    cannot inherit its predecessor's live credential. Binding supersedes the
    owner's previous tokens for that identity in the same transaction: one
    identity, one live credential, and the superseded ids ride the return so a
    rotation is reported rather than silent.

    ROOMS ATTACH AT MINT, in the same transaction, or the mint does not happen
    (architect ruling 9010, from the operator's failed install): a mint that
    attaches in a second call has a window where a token exists and reaches
    nothing, and that window ate a real install -- the second call POSTed to a
    route that takes PATCH, so it could never succeed on any box, and the last
    step of the flow left a credential to hand-revoke. assign_room inside the
    tx is the same reach check every route uses; a room that fails it rolls
    back the token too.
    """
    agent_name = (agent_name or "").strip() or None
    tid, secret = uuid.uuid4().hex, secrets.token_urlsafe(32)
    now = time.time_ns()
    agent_id, superseded = None, []
    with tx(conn):
        if agent_name:
            valid_name(agent_name)
            row = conn.execute(
                "SELECT id FROM agents WHERE owner_id=? AND name=? "
                "AND retired_ns IS NULL", (owner_id, agent_name)).fetchone()
            if row and create:
                # A HELD NAME IS NOT A NEW AGENT (DES-011 section 2, ruling
                # 10969). create=True declares "bring a NEW being into the
                # world"; landing on an existing live agent and rotating its
                # credential would let a human who meant a new agent silently
                # hijack an old one's token. Refuse, name the existing agent
                # and where it lives, offer both remedies, touch nothing. The
                # existing credential is untouched BY CONSTRUCTION: this raise
                # sits before supersede_bound_tokens.
                rooms = [r["name"] for r in conn.execute(
                    "SELECT r.name FROM members m JOIN rooms r ON r.id=m.room_id "
                    "WHERE m.principal=? AND m.left_ns IS NULL ORDER BY r.name",
                    (agent_principal(row["id"]),))]
                raise BusError(
                    f"you already have a live agent named {agent_name!r} "
                    f"(in: {', '.join(rooms) if rooms else 'no rooms'}). "
                    f"Creating a duplicate is refused. Either choose a unique "
                    f"name for a separate agent, or add the existing "
                    f"{agent_name!r} to the room you meant. To move that "
                    f"agent to a new body instead, mint WITHOUT create.")
            if row:
                # BARE ATTACH IS THE BODY-SWAP VERB (DES-011 section 2.1): the
                # owner re-minting a held name attaches a new body to the
                # EXISTING identity and supersedes the previous body's
                # credential in this same transaction. This branch must stay
                # open or migration dies with it -- gated as such.
                agent_id = row["id"]
            elif create:
                agent_id = str(uuid.uuid4())
                conn.execute(
                    "INSERT INTO agents(id, owner_id, name, created_ns) "
                    "VALUES(?,?,?,?)", (agent_id, owner_id, agent_name, now))
                record_agent_name(conn, agent_id, agent_name, now)
            else:
                # THE GUARD AGAINST SILENT FORKS (ruling 10896, measured live
                # 2026-08-15: 'architect' vs 'reveille-architect', mail split
                # per name while every transport signal stayed green). Minting
                # a bound token ATTACHES a body to an existing identity;
                # bringing a NEW identity into the world is a separate,
                # deliberate act. A name with no live identity used to mint
                # one silently, so any variant spelling forked the agent and
                # every control reported the fork healthy. The refusal names
                # the owner's own live agents so a near-miss is visible at the
                # moment it can still be corrected.
                live = live_agent_names(conn, owner_id)
                raise BusError(
                    f"no live agent of yours is named {agent_name!r}. A bound "
                    f"mint attaches to an existing identity; creating a new "
                    f"agent is deliberate -- pass create=true. Your live "
                    f"agents: {', '.join(live) if live else '(none)'}")
            superseded = supersede_bound_tokens(conn, owner_id, agent_id)
        conn.execute(
            "INSERT INTO tokens(id, secret_hash, owner_id, label, agent_id, mem_tier, "
            "created_ns) VALUES(?,?,?,?,?,?,?)",
            (tid, _sha(secret), owner_id, label, agent_id, mem_tier, now))
        for rid in rooms or []:
            assign_room(conn, tid, rid, owner_id)
    return {"id": tid, "secret": secret, "label": label, "agent_name": agent_name,
            "agent_id": agent_id, "mem_tier": mem_tier, "superseded": superseded,
            "rooms": list(rooms or [])}


def resolve_token(conn, secret):
    """Token row for a presented secret, or None. A revoked token is DELETED, so it
    resolves to None here -- which is what makes revocation instant."""
    if not secret:
        return None
    r = conn.execute(
        "SELECT t.*, a.name AS agent_name FROM tokens t "
        "LEFT JOIN agents a ON a.id = t.agent_id WHERE t.secret_hash=?",
        (_sha(secret),)).fetchone()
    if not r:
        return None
    conn.execute("UPDATE tokens SET last_used_ns=? WHERE id=?", (time.time_ns(), r["id"]))
    return {"id": r["id"], "owner_id": r["owner_id"], "label": r["label"],
            "agent_name": r["agent_name"], "agent_id": r["agent_id"],
            "mem_tier": r["mem_tier"]}


def list_tokens(conn, owner_id):
    out = []
    for r in conn.execute(
            "SELECT t.*, a.name AS agent_name FROM tokens t "
            "LEFT JOIN agents a ON a.id = t.agent_id "
            "WHERE t.owner_id=? ORDER BY t.created_ns", (owner_id,)):
        out.append({"id": r["id"], "label": r["label"], "agent_name": r["agent_name"],
                    "agent_id": r["agent_id"],
                    "mem_tier": r["mem_tier"],  # visible AND mutable (DES-001 sec 6)
                    "created_ns": r["created_ns"], "last_used_ns": r["last_used_ns"],
                    "rooms": rooms_for_token(conn, r["id"])})
    return out


def set_token_tier(conn, token_id, actor_user_id, tier, *, actor, is_admin=False):
    """Flip a token's memory tier. A tier flip is an AUTHORITY change (S6b,
    msg 8448) and is audited like the verdicts: who flipped whose token, from
    what to what, when. Owner-or-admin, the same scoping as revoke. A flip to
    the tier the token already holds is a no-op and writes no row -- no change,
    no event."""
    if tier not in TIERS:
        raise BusError(f"bad mem_tier {tier!r}: one of {TIERS}")
    r = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
    if r is None:
        raise BusError(f"no such token: {token_id}")
    if r["owner_id"] != actor_user_id and not is_admin:
        raise AccessError("only the token's owner or an admin sets its tier")
    old = r["mem_tier"]
    if old != tier:
        with tx(conn):
            conn.execute("UPDATE tokens SET mem_tier=? WHERE id=?",
                         (tier, token_id))
            aname = conn.execute("SELECT name FROM agents WHERE id=?",
                                 (r["agent_id"],)).fetchone() if r["agent_id"] else None
            conn.execute(
                "INSERT INTO token_audit(token_id, agent_name, agent_id, action, "
                "actor, old_value, new_value, ts_ns) VALUES(?,?,?,'tier',?,?,?,?)",
                (token_id, aname["name"] if aname else None, r["agent_id"],
                 actor, old, tier, time.time_ns()))
    return {"id": token_id, "mem_tier": tier}


def token_audit_rows(conn, token_id=None, limit=200):
    q, args = "SELECT * FROM token_audit", []
    if token_id:
        q += " WHERE token_id=?"
        args.append(token_id)
    rows = conn.execute(q + " ORDER BY ts_ns DESC LIMIT ?",
                        args + [max(1, min(int(limit), 1000))]).fetchall()
    return [dict(r) for r in rows]


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
        # agent still joined under this token turns the delete into a FK violation --
        # AND MARK THEM LEFT (operator relay 11337): a revoke that only orphaned the
        # row left a GHOST MEMBER, listed as joined, deaf, and unable to leave()
        # because leaving needed the access the revoke had just removed.
        conn.execute("UPDATE members SET token_id=NULL, left_ns=COALESCE(left_ns, ?) "
                     "WHERE token_id=?", (time.time_ns(), token_id))
        conn.execute("DELETE FROM token_rooms WHERE token_id=?", (token_id,))
        conn.execute("DELETE FROM tokens WHERE id=?", (token_id,))


def reconcile_reach(conn):
    """A membership whose token no longer reaches its room is a departure
    (11337): mark it left, wherever the reach went -- a room unassigned from a
    token, a room flipped private, a member removed, an account deleted. ONE
    statement, called at every site that deletes token_rooms rows, and
    idempotent, so it also heals ghosts left before this rule existed."""
    conn.execute(
        "UPDATE members SET left_ns=? WHERE left_ns IS NULL AND token_id IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM token_rooms tr WHERE tr.token_id=members.token_id "
        "AND tr.room_id=members.room_id)", (time.time_ns(),))


def leave_listed(conn, principal, token_id, room_id):
    """leave() for a room the caller is LISTED in but no longer reaches (11337):
    a verb whose only effect is to reduce access must not require access. Marks
    only this token's own row for that identity and room."""
    conn.execute("UPDATE members SET left_ns=? WHERE principal=? AND token_id=? AND room_id=? "
                 "AND left_ns IS NULL", (time.time_ns(), principal, token_id, room_id))


def supersede_bound_tokens(conn, owner_id, agent_id):
    """Revoke the owner's live tokens bound to this identity; returns their ids.

    Called at bound-mint time: ONE bus identity, ONE live credential (operator
    ruling 2026-07-30 -- only one agent with a name is ever active, so a second
    live credential for the name is never intent). Without this, every
    re-provision minted a fresh token and left the predecessor alive: the
    operator found FOUR live credentials all answering to one agent. Same
    shape as the wake-attach rule (DES-003 2.3): a newer attachment supersedes
    the old one instead of coexisting with it. Owner-scoped on purpose --
    superseding IS revocation and carries exactly revocation's authority; it
    must never let minting a name become a lever on another owner's tokens.

    BY IDENTITY, NOT BY STRING (DES-007 2.4): what is being superseded is the
    credential for one INSTANCE of a label, so a declined resurrect minting a
    new identity under a reused name does not revoke the retired instance's
    history of credentials by accident of spelling."""
    rows = conn.execute(
        "SELECT id, secret_hash FROM tokens WHERE owner_id=? AND agent_id=?",
        (owner_id, agent_id)).fetchall()
    now = time.time_ns()
    for r in rows:
        # The tombstone rides the same transaction as the supersede (S2, ruling
        # 10876): the displaced credential's refusal becomes a signpost naming
        # the way back, and its last_refusal_ns is S3's liveness reading. Only
        # supersession tombstones -- plain revoke_token stays a bare delete.
        conn.execute(
            "INSERT OR REPLACE INTO token_tombstones"
            "(secret_hash, agent_id, superseded_ns) VALUES(?,?,?)",
            (r["secret_hash"], agent_id, now))
        revoke_token(conn, r["id"], owner_id)
    return [r["id"] for r in rows]


# Fixed age (ruling 10876: "prune tombstones on a fixed age; past that the
# refusal goes generic"). Prune rides the read -- an expired row is deleted the
# next time its hash knocks, so there is no timer to lapse and the table is
# bounded by the supersede count within the window.
TOMBSTONE_TTL_NS = 30 * 86_400 * 1_000_000_000


def tombstone_for(conn, secret):
    """The signpost payload for a superseded credential, or None.

    Reading refreshes last_refusal_ns: a refused call carrying this hash is
    evidence that something RAN on the dormant body -- S3's honest liveness
    reading ("a human used that machine at T", never "a daemon is up"). Past
    TOMBSTONE_TTL_NS the row is deleted on sight and the caller falls back to
    the generic refusal. Returns agent name via the agents row; an agent that
    was pruned since leaves the join empty, which is the generic refusal too.
    """
    if not secret:
        return None
    h = _sha(secret)
    r = conn.execute(
        "SELECT t.agent_id, t.superseded_ns, a.name FROM token_tombstones t "
        "JOIN agents a ON a.id = t.agent_id WHERE t.secret_hash=?", (h,)).fetchone()
    if not r:
        return None
    now = time.time_ns()
    if now - r["superseded_ns"] > TOMBSTONE_TTL_NS:
        conn.execute("DELETE FROM token_tombstones WHERE secret_hash=?", (h,))
        return None
    conn.execute("UPDATE token_tombstones SET last_refusal_ns=? WHERE secret_hash=?",
                 (now, h))
    return {"agent_id": r["agent_id"], "agent_name": r["name"],
            "superseded_ns": r["superseded_ns"]}


def assign_room(conn, token_id, room_id, actor_id):
    """Put a room on a token. The ONE reach check (DES-004 I1): allowed if the
    actor owns the room, the room is public, or the actor is an invited member.
    No route re-derives this."""
    room = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not room:
        raise BusError("no such room")
    tok = conn.execute("SELECT owner_id FROM tokens WHERE id=?", (token_id,)).fetchone()
    if not tok or tok["owner_id"] != actor_id:
        raise AccessError("not your token")
    if room["owner_id"] != actor_id and not room["public"] \
            and not is_member(conn, room_id, actor_id):
        raise AccessError("room is private")
    conn.execute("INSERT OR IGNORE INTO token_rooms(token_id, room_id) VALUES(?,?)",
                 (token_id, room_id))


def unassign_room(conn, token_id, room_id, actor_id):
    tok = conn.execute("SELECT owner_id FROM tokens WHERE id=?", (token_id,)).fetchone()
    if not tok or tok["owner_id"] != actor_id:
        raise AccessError("not your token")
    with tx(conn):
        conn.execute("DELETE FROM token_rooms WHERE token_id=? AND room_id=?", (token_id, room_id))
        reconcile_reach(conn)


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


def ownerless_rooms(conn):
    """Rooms whose owner is gone (deleting a person leaves their rooms
    standing -- the history is not theirs to take). Nobody can change their
    retention, publicity or name until someone adopts them, so they are listed
    for an admin with what is at stake: how many messages, how many members."""
    return [{"id": r["id"], "name": r["name"], "public": bool(r["public"]),
             "created_ns": r["created_ns"],
             "messages": conn.execute("SELECT count(*) FROM messages WHERE room=?",
                                      (r["id"],)).fetchone()[0],
             "members": conn.execute("SELECT count(*) FROM members WHERE room_id=? "
                                     "AND left_ns IS NULL", (r["id"],)).fetchone()[0]}
            for r in conn.execute("SELECT * FROM rooms WHERE owner_id IS NULL "
                                  "ORDER BY name")]


def adopt_room(conn, room_id, new_owner_id, actor_name):
    """An admin gives an OWNERLESS room an owner (EPIC-001 #4, ruling 11604
    gap). Only ownerless: taking a room that has an owner would be seizure,
    and there is no such verb here. The audit row is the record -- ownership
    moved by an act, never silently."""
    r = conn.execute("SELECT id, name, owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise NotFound("no such room")
    if r["owner_id"]:
        raise BusError("that room already has an owner")
    u = conn.execute("SELECT id, name FROM users WHERE id=? AND deleted_ns IS NULL",
                     (new_owner_id,)).fetchone()
    if not u:
        raise NotFound("no such user")
    # Room names are unique per owner: an adopter who already has one by that
    # name is told, rather than the UPDATE failing as an opaque integrity error.
    if conn.execute("SELECT 1 FROM rooms WHERE owner_id=? AND name=?",
                    (new_owner_id, r["name"])).fetchone():
        raise BusError(f"{u['name']} already owns a room named {r['name']!r} -- "
                       f"rename one of them first")
    with tx(conn):
        conn.execute("UPDATE rooms SET owner_id=? WHERE id=?", (new_owner_id, room_id))
        _audit_room(conn, room_id, "adopt", actor_name, u["name"])
    return {"id": room_id, "name": r["name"], "owner": u["name"]}


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
    """Flip a room's visibility. Going non-public REVOKES it from every token
    whose owner is neither the owner nor an invited member (DES-004: members
    lose nothing on the flip -- to make a shared room fully private, remove the
    members). Past MESSAGES stay -- authorship is history, and deleting it
    would silently rewrite threads others are reading."""
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
                "(SELECT id FROM tokens WHERE owner_id != ? AND owner_id NOT IN "
                "(SELECT user_id FROM room_members WHERE room_id=?))",
                (room_id, owner_id, room_id))
            reconcile_reach(conn)


# ---- membership (DES-004: reach, never rule) ---------------------------------

def is_member(conn, room_id, user_id):
    return conn.execute("SELECT 1 FROM room_members WHERE room_id=? AND user_id=?",
                        (room_id, user_id)).fetchone() is not None


def _audit_room(conn, room_id, action, actor_name, subject_name):
    conn.execute("INSERT INTO room_audit(room_id, action, actor, subject, ts_ns) "
                 "VALUES(?,?,?,?,?)",
                 (room_id, action, actor_name, subject_name, time.time_ns()))


def _owned_room(conn, room_id, owner_id):
    r = conn.execute("SELECT * FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise BusError("no such room")
    if r["owner_id"] != owner_id:
        raise AccessError("not your room")
    return r


def invite_member(conn, room_id, owner_id, user_name, actor_name):
    """Owner invites a user BY EXACT NAME (DES-004 Q2: name entry, no picker --
    a picker leaks the instance's user list to every room owner). Unknown name,
    the owner themselves, and an existing member all fail with the SAME text,
    so a failed invite confirms nothing about who exists."""
    _owned_room(conn, room_id, owner_id)
    u = conn.execute("SELECT id, name FROM users WHERE name=?",
                     (user_name,)).fetchone()
    if u is None or u["id"] == owner_id or is_member(conn, room_id, u["id"]):
        raise BusError("nothing to invite for that name")
    with tx(conn):
        conn.execute("INSERT INTO room_members(room_id, user_id, added_ns, added_by) "
                     "VALUES(?,?,?,?)", (room_id, u["id"], time.time_ns(), owner_id))
        _audit_room(conn, room_id, "invite", actor_name, u["name"])
    return {"room_id": room_id, "user": u["name"]}


def remove_member(conn, room_id, owner_id, user_name, actor_name):
    """Removal is a revoke, not a hide (DES-004 G2/I2): the member's token_rooms
    rows for this room die in the SAME transaction, so reach ends on their very
    next call. Their past messages stay. Non-member and unknown name fail with
    the same text as invite's failures."""
    _owned_room(conn, room_id, owner_id)
    u = conn.execute("SELECT id, name FROM users WHERE name=?",
                     (user_name,)).fetchone()
    if u is None or not is_member(conn, room_id, u["id"]):
        raise BusError("nothing to remove for that name")
    with tx(conn):
        conn.execute("DELETE FROM room_members WHERE room_id=? AND user_id=?",
                     (room_id, u["id"]))
        conn.execute("DELETE FROM token_rooms WHERE room_id=? AND token_id IN "
                     "(SELECT id FROM tokens WHERE owner_id=?)", (room_id, u["id"]))
        reconcile_reach(conn)
        _audit_room(conn, room_id, "remove", actor_name, u["name"])
    return {"room_id": room_id, "user": u["name"]}


def member_list(conn, room_id, owner_id):
    """Who is invited, owner's eyes only. Names resolved live; a deleted user's
    membership dies with them, so every row here names a real user."""
    _owned_room(conn, room_id, owner_id)
    return [{"name": r["name"], "added_ns": r["added_ns"]} for r in conn.execute(
        "SELECT u.name, m.added_ns FROM room_members m JOIN users u ON u.id=m.user_id "
        "WHERE m.room_id=? ORDER BY m.added_ns", (room_id,))]


def member_rooms(conn, user_id):
    """Rooms shared WITH this user. Kept separate from list_rooms on purpose:
    list_rooms is the authority scope for ratification (owned rooms), and
    folding member rooms into it would hand every member the owner's decide
    power one call site at a time (I3)."""
    return [_room_dict(r, r["owner_name"]) for r in conn.execute(
        "SELECT ro.*, u.name AS owner_name FROM room_members m "
        "JOIN rooms ro ON ro.id=m.room_id LEFT JOIN users u ON u.id=ro.owner_id "
        "WHERE m.user_id=? ORDER BY ro.name", (user_id,))]


def member_count(conn, room_id):
    return conn.execute("SELECT count(*) FROM room_members WHERE room_id=?",
                        (room_id,)).fetchone()[0]


def room_audit_rows(conn, room_id, limit=200):
    rows = conn.execute("SELECT * FROM room_audit WHERE room_id=? "
                        "ORDER BY ts_ns DESC LIMIT ?",
                        (room_id, max(1, min(int(limit), 1000)))).fetchall()
    return [dict(r) for r in rows]


def set_retention(conn, room_id, owner_id, retention_ns):
    """Per-room TTL. NULL = keep forever (the default)."""
    r = conn.execute("SELECT owner_id FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise BusError("no such room")
    if r["owner_id"] != owner_id:
        raise AccessError("not your room")
    conn.execute("UPDATE rooms SET retention_ns=? WHERE id=?", (retention_ns, room_id))


# ---- DES-013: the bank, who speaks with what, and the script row -----------------

VOICE_BANK_MAX = 64   # every distinct clip costs the synthesizer ~175 MB of conds VRAM


def voice_put(conn, voice_id, *, name, uploaded_by, seconds, nbytes, personal=False):
    """Create or replace a bank voice's ROW (the caller wrote the clip). Replace
    keeps created_ns, uploaded_by AND personal (immutable after creation, 11155:
    bank->personal would strip a voice other speakers hold, personal->bank
    would publish a human's voice on a click; change of mind = delete + re-add)
    and moves updated_ns; identity is the id, not the bytes."""
    valid_name(voice_id)
    now = time.time_ns()
    with tx(conn):
        have = conn.execute("SELECT 1 FROM voices WHERE id=?", (voice_id,)).fetchone()
        if not have and conn.execute("SELECT count(*) FROM voices").fetchone()[0] >= VOICE_BANK_MAX:
            raise BusError(f"the bank holds VOICE_BANK_MAX={VOICE_BANK_MAX} voices; "
                           f"replace one, or raise the cap (and CONDS_CACHE_MAX on the GPU host)")
        conn.execute(
            "INSERT INTO voices(id, name, uploaded_by, seconds, bytes, created_ns, updated_ns, "
            "personal) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
            "seconds=excluded.seconds, bytes=excluded.bytes, updated_ns=excluded.updated_ns",
            (voice_id, name, uploaded_by, float(seconds), int(nbytes), now, now, int(bool(personal))))
    return voice_get(conn, voice_id)


def voice_delete(conn, voice_id):
    """Delete a voice and every assignment of it (those speakers re-default on
    their next voice_for); scripts keep their voice_id label -- a delete is
    history. Returns rows deleted (0 = no such voice). The caller unlinks the
    clip AFTER this commits."""
    with tx(conn):
        conn.execute("DELETE FROM voice_assignments WHERE voice_id=?", (voice_id,))
        return conn.execute("DELETE FROM voices WHERE id=?", (voice_id,)).rowcount


def voice_rename(conn, old, new):
    """Move a voice to a new id in ONE transaction: the row (updated_ns now),
    its assignments, and its scripts label -- a rename is the same voice, so
    history follows it (11155). Refuses a taken id. The caller has ALREADY
    moved the clip file (and moves it back if this raises)."""
    valid_name(new)
    v = voice_get(conn, old)
    if v is None:
        raise BusError(f"no such bank voice: {old}")
    if old == new:
        return v
    with tx(conn):
        if conn.execute("SELECT 1 FROM voices WHERE id=?", (new,)).fetchone():
            raise BusError(f"the id {new} is taken")
        conn.execute(
            "INSERT INTO voices(id, name, persona, sample, personal, uploaded_by, seconds, bytes, "
            "created_ns, updated_ns) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (new, v["name"], v["persona"], v["sample"], v["personal"], v["uploaded_by"],
             v["seconds"], v["bytes"], v["created_ns"], time.time_ns()))
        conn.execute("UPDATE voice_assignments SET voice_id=? WHERE voice_id=?", (new, old))
        conn.execute("UPDATE scripts SET voice_id=? WHERE voice_id=?", (new, old))
        conn.execute("DELETE FROM voices WHERE id=?", (old,))
    return voice_get(conn, new)


def voice_patch(conn, voice_id, *, name=None, persona=None, sample=None):
    """Edit the label, the persona or the sample line. Returns rows changed
    (0 = no such voice)."""
    fields = {k: v for k, v in (("name", name), ("persona", persona), ("sample", sample))
              if v is not None}
    if not fields:
        return 0
    fields["updated_ns"] = time.time_ns()
    sets = [f"{k}=?" for k in fields]
    args = list(fields.values())
    with tx(conn):
        return conn.execute(f"UPDATE voices SET {', '.join(sets)} WHERE id=?",
                            args + [voice_id]).rowcount


def voice_get(conn, voice_id):
    r = conn.execute("SELECT * FROM voices WHERE id=?", (voice_id,)).fetchone()
    return dict(r) if r else None


def voices(conn, viewer_uid=None):
    """The bank as ONE viewer sees it: every bank voice plus the viewer's own
    personal voices (11155: a personal voice exists only for its uploader).
    viewer_uid None = bank voices only (a default pick for an agent)."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM voices WHERE personal=0 OR uploaded_by=? ORDER BY id", (viewer_uid,))]


def all_voices(conn):
    """Every row, personal included -- the synthesizer's reconcile (a personal
    voice is still spoken, by its uploader) and nothing user-facing."""
    return [dict(r) for r in conn.execute("SELECT * FROM voices ORDER BY id")]


def voice_reachable(conn, voice_id, viewer_uid):
    """The row if THIS viewer may know it exists: any bank voice, or a personal
    voice the viewer uploaded. None otherwise -- the same answer as a
    nonexistent id, on purpose (11155)."""
    v = voice_get(conn, voice_id)
    if v is None or (v["personal"] and v["uploaded_by"] != viewer_uid):
        return None
    return v


def assign_refusal(actor_uid, is_admin, room_owner_id, speaker_owner_id, current, holder):
    """DES-013 section 4, pure. Returns the set_by the write should carry, or raises.

    Owner over room over default: the speaker's owner may set/unset over anything;
    the room owner only over nothing/'default'/'room'; nobody else -- admin
    included, rooms are the owner's (DES-004: reach, never rule). A voice held by
    another speaker in this room is refused naming the holder, whoever asks."""
    if speaker_owner_id is None:
        raise BusError("this speaker has no owner (unbound token) and cannot be assigned "
                       "a voice; it keeps the digest pick")
    if holder:
        raise BusError(f"that voice is held by {holder} in this room")
    if actor_uid == speaker_owner_id:
        return "owner"
    if actor_uid == room_owner_id:
        if current == "owner":
            raise AccessError("the speaker's owner set this voice; only they can change it")
        return "room"
    raise AccessError("only the speaker's owner or the room's owner assigns voices"
                      + (" -- admin has no reach here" if is_admin else ""))


def voice_default(*, elsewhere, taken, bank, name=None):
    """DES-013 section 4 default, pure (ruling 11121). THE INVARIANT: explicit
    choices travel; the NAME beats anything derived. `elsewhere` is
    [(voice_id, set_by), ...] newest first, this speaker's rows in other rooms.
      1. a voice held elsewhere with set_by in (owner, room), if free here --
         somebody chose it, it travels;
      2. a bank voice whose id EQUALS the speaker's name, if free here
         (DES-009 section 5's voices/<name>.wav carried into the bank);
      3. a voice held elsewhere with set_by=default, if free here --
         consistency across rooms for the unnamed;
      4. the first free bank voice; else None -- the caller falls to the
         digest pick from the predefined set."""
    for v, how in elsewhere:
        if how in ("owner", "room") and v not in taken:
            return v
    if name and name in bank and name not in taken:
        return name
    for v, how in elsewhere:
        if how == "default" and v not in taken:
            return v
    for v in bank:
        if v not in taken:
            return v
    return None


def _holder(conn, room_id, voice_id):
    """The KEY of the speaker holding this voice in this room, or None. A key,
    never a name: two speakers can share a display name (per-owner uniqueness,
    an agent:/user: coincidence) -- the very case this design keys by id for --
    and a name comparison would let the courtesy check pass and the UNIQUE index
    throw raw (verdict 11059)."""
    r = conn.execute("SELECT speaker FROM voice_assignments WHERE room_id=? AND voice_id=?",
                     (room_id, voice_id)).fetchone()
    return r["speaker"] if r else None


def speaker_owner(conn, speaker):
    """users.id that owns this speaker: an agent's owner, or the user themself.
    None when the key names nothing (unassignable)."""
    kind, _, sid = speaker.partition(":")
    if kind == "user":
        r = conn.execute("SELECT id FROM users WHERE id=?", (sid,)).fetchone()
        return r["id"] if r else None
    r = conn.execute("SELECT owner_id FROM agents WHERE id=?", (sid,)).fetchone()
    return r["owner_id"] if r else None


def _speaker_name(conn, speaker):
    kind, _, sid = speaker.partition(":")
    tbl = "agents" if kind == "agent" else "users"
    r = conn.execute(f"SELECT name FROM {tbl} WHERE id=?", (sid,)).fetchone()
    return r["name"] if r else speaker


def assign_voice(conn, room_id, speaker, voice_id, *, set_by):
    """Write the (room, speaker) -> voice row; the caller ran assign_refusal.
    A collision that slipped past the courtesy check is the UNIQUE index's."""
    if set_by not in ("owner", "room", "default"):
        raise BusError(f"bad set_by {set_by!r}")
    if not voice_get(conn, voice_id):
        raise BusError(f"no such bank voice: {voice_id}")
    holder = _holder(conn, room_id, voice_id)
    if holder and holder != speaker:
        raise BusError(f"that voice is held by {_speaker_name(conn, holder)} in this room")
    with tx(conn):
        conn.execute(
            "INSERT INTO voice_assignments(room_id, speaker, voice_id, set_by, ts_ns) "
            "VALUES(?,?,?,?,?) ON CONFLICT(room_id, speaker) DO UPDATE SET "
            "voice_id=excluded.voice_id, set_by=excluded.set_by, ts_ns=excluded.ts_ns",
            (room_id, speaker, voice_id, set_by, time.time_ns()))


def unassign_voice(conn, room_id, speaker):
    with tx(conn):
        return conn.execute("DELETE FROM voice_assignments WHERE room_id=? AND speaker=?",
                            (room_id, speaker)).rowcount


def voice_for(conn, room_id, speaker):
    """The bank voice this speaker uses in this room, materializing the default
    on first sight (a row with set_by='default' -- stable and visible). None for
    an unkeyed speaker or an empty bank: the caller uses the digest pick."""
    if not speaker:
        return None
    r = conn.execute("SELECT voice_id FROM voice_assignments WHERE room_id=? AND speaker=?",
                     (room_id, speaker)).fetchone()
    if r:
        return r["voice_id"]
    elsewhere = [(x["voice_id"], x["set_by"]) for x in conn.execute(
        "SELECT voice_id, set_by FROM voice_assignments WHERE speaker=? ORDER BY ts_ns DESC",
        (speaker,))]
    taken = {x["voice_id"] for x in conn.execute(
        "SELECT voice_id FROM voice_assignments WHERE room_id=?", (room_id,))}
    # The bank the default may pick from (11155): every bank voice, plus the
    # personal voices of user:<uid> when the speaker IS that user -- so a human
    # who records "<username>" gets it by rule 2 (name beats derived) and
    # nobody else ever does.
    kind, _, key = speaker.partition(":")
    pick = voice_default(elsewhere=elsewhere, taken=taken,
                         bank=[v["id"] for v in voices(conn, key if kind == "user" else None)],
                         name=_speaker_name(conn, speaker))
    if pick:
        try:
            assign_voice(conn, room_id, speaker, pick, set_by="default")
        except (BusError, sqlite3.IntegrityError):
            # Two callers materialized at once (worker thread and listing route)
            # and the other one won: whatever landed is the answer, and if
            # nothing did the pick is gone -- None, the digest, until next time.
            r = conn.execute("SELECT voice_id FROM voice_assignments WHERE room_id=? "
                             "AND speaker=?", (room_id, speaker)).fetchone()
            return r["voice_id"] if r else None
    return pick


def room_speakers(conn, room_id):
    """Every assigned speaker in a room plus every present keyed member, with
    voice_id/set_by (None when unassigned) and whether the member is present.
    Members with no agents.id are unkeyable and listed as such (voice None,
    speaker None) so the owner sees why they cannot be assigned."""
    now = time.time_ns()
    out, seen = [], set()
    for r in conn.execute(
            "SELECT va.speaker, va.voice_id, va.set_by FROM voice_assignments va "
            "WHERE va.room_id=? ORDER BY va.speaker", (room_id,)):
        seen.add(r["speaker"])
        kind = r["speaker"].partition(":")[0]
        out.append({"speaker": r["speaker"], "name": _speaker_name(conn, r["speaker"]),
                    "kind": kind, "present": False, "voice_id": r["voice_id"],
                    "set_by": r["set_by"]})
    by_key = {s["speaker"]: s for s in out}
    # THE KEY IS THE PRINCIPAL the membership is keyed on (v28): stamped from
    # the credential at join, so a healed row (readmit) carries it too.
    for m in conn.execute(
            "SELECT m.name, m.tag, m.principal, m.seen_ns "
            "FROM members m WHERE m.room_id=? AND m.left_ns IS NULL", (room_id,)):
        if not _is_live(m["seen_ns"], now):
            continue
        if user_of(m["principal"]):
            continue          # a human: keyed user:<id> by their own row, never "unbound"
        key = m["principal"]
        if key in by_key:
            by_key[key]["present"] = True
            continue
        out.append({"speaker": key, "name": m["name"], "kind": "agent", "present": True,
                    "voice_id": None, "set_by": None})
    return out


def script_put(conn, message_id, text, voice_id, model, ms):
    """The writer's script for one message. INSERT OR REPLACE: a retry after a
    partial write overwrites. CONTRACT: the message must exist -- a script for a
    message retracted mid-write raises sqlite3.IntegrityError (FK), and the
    writer worker (slice 5) owns that catch: no row, no frame, no orphan."""
    with tx(conn):
        conn.execute(
            "INSERT OR REPLACE INTO scripts(message_id, text, voice_id, model, ms, ts_ns) "
            "VALUES(?,?,?,?,?,?)", (message_id, text, voice_id, model, int(ms), time.time_ns()))


def script_get(conn, message_id):
    r = conn.execute("SELECT * FROM scripts WHERE message_id=?", (message_id,)).fetchone()
    return dict(r) if r else None


# WHERE AUDIO LIVES, set once by the daemon that owns the directory. It is here
# rather than passed per call because the unlink below must be the ONLY unlink:
# a per-caller one is exactly how the orphaned uploads happened (msg 8857), and
# the retention sweep is again the path with nobody watching. None = no voices
# configured, and then there is nothing to remove.
AUDIO_DIR = None


def _delete_messages(conn, ids):
    """Drop messages and everything referencing them. Caller holds the transaction.

    THE AUDIO DIES WITH THE MESSAGE, and this is the single choke point every
    delete passes through -- prune, purge_room, retract_message, and the
    retention sweep (DES-009 section 7). An audio file has no database row: the
    message id is the key and the filename is the record, so there is nothing to
    reconcile and nothing that can lapse into disagreeing with the bytes.
    """
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
    # DES-017: a clip's converted pair under <files> dies with its message too
    # (the tts-<mid> names are hard links to it; both names must go).
    clips = [r["url"][len("/files/"):] for r in conn.execute(
        f"SELECT url FROM attachments WHERE clip=1 AND message_id IN ({ph})", ids)
        if r["url"].startswith("/files/")]
    conn.execute(f"DELETE FROM attachments WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM scripts WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM reads WHERE message_id IN ({ph})", ids)
    conn.execute(f"DELETE FROM links WHERE parent_id IN ({ph}) OR child_id IN ({ph})", ids + ids)
    conn.execute(f"DELETE FROM messages WHERE id IN ({ph})", ids)
    if AUDIO_DIR:
        for mid in ids:
            # Best effort by design: a missing file is the normal case (voices
            # off, service down, message never spoken) and must not fail a
            # delete. The delete is the authority here; the bytes follow it.
            # Both names: the .part is the record of an in-flight synthesis, and
            # a worker whose rename then finds no .part fails closed -- no .wav
            # lands for a deleted message, with no second code path.
            for name in (f"tts-{mid}.webm", f"tts-{mid}.webm.part", f"tts-{mid}.m4a", f"tts-{mid}.m4a.part"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(AUDIO_DIR, name))
        for stored in clips:
            stem = stored[:-5] if stored.endswith(".webm") else stored
            conn.execute("DELETE FROM files WHERE stored IN (?,?)", (stem + ".webm", stem + ".m4a"))
            for name in (stem + ".webm", stem + ".m4a"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(AUDIO_DIR, name))


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
        conn.execute("DELETE FROM voice_assignments WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM members WHERE room_id=?", (room_id,))
        conn.execute("DELETE FROM room_members WHERE room_id=?", (room_id,))
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

def _member(conn, room_id, principal):
    """The identity's raw row in this room, INCLUDING one marked left_ns. Callers
    that ask "is this room-name HERE" want _present() instead; this one exists
    for the callers that need to know a row exists at all -- readmit (a row you
    left is not a gap to fill) and join (which clears the mark)."""
    return conn.execute("SELECT * FROM members WHERE room_id=? AND principal=?",
                        (room_id, principal)).fetchone()


def _present(conn, room_id, name):
    """Does a room-name resolve to a member here right now? A row marked left_ns
    does not. `to=` names a room-name (DES-011 s3), so this is a NAME lookup."""
    return conn.execute(
        "SELECT 1 FROM members WHERE room_id=? AND name=? AND left_ns IS NULL",
        (room_id, name)).fetchone() is not None


def recipient_agent_id(conn, room_id, name):
    """The identity a room-name denotes in this room right now, or None (a
    broadcast, a human, or a name nobody holds here). Reads the members row,
    which join() keys on the principal -- the one place a room-name meets an id."""
    if name == BROADCAST:
        return None
    r = conn.execute("SELECT principal FROM members WHERE room_id=? AND name=? "
                     "AND left_ns IS NULL", (room_id, name)).fetchone()
    return agent_of(r["principal"]) if r else None


def room_name(conn, room_id, principal):
    """What this room calls the identity: its members row's name (own name or
    alias) while it is a member; its own name otherwise."""
    r = conn.execute("SELECT name FROM members WHERE room_id=? AND principal=? "
                     "AND left_ns IS NULL", (room_id, principal)).fetchone()
    return r["name"] if r else _identity_name(conn, principal)


def _room_name_for(conn, room_id, principal, name, now):
    """The room-name a joining identity gets (DES-011 s2): the bare `name` unless
    a LIVE member of another identity holds it here, then the alias
    <owner>-<name>; an alias that is itself held live is refused naming both. A
    stale holder (heartbeat gone) is reaped on the spot -- the reaper would have,
    and a dead row must not decide a live agent's name."""
    def holder(n):
        h = conn.execute(
            "SELECT principal, tag, seen_ns FROM members WHERE room_id=? AND name=? "
            "AND left_ns IS NULL AND principal!=?", (room_id, n, principal)).fetchone()
        if h and not _is_live(h["seen_ns"], now):
            conn.execute("DELETE FROM members WHERE room_id=? AND principal=?",
                         (room_id, h["principal"]))
            return None
        return h
    if holder(name) is None:
        return name
    owner = _owner_name(conn, principal)
    alias = f"{owner}-{name}"
    try:
        valid_name(alias)
    except BusError:
        raise BusError(f"name {name!r} is held by a live agent in this room and "
                       f"{owner!r} cannot form the alias {alias!r} -- pick another name")
    h2 = holder(alias)
    if h2 is not None:
        raise BusError(f"name {name!r} is held by a live agent in this room, and so is "
                       f"the alias {alias!r} (tag {h2['tag']}). pick another.")
    return alias


def mint_agent(conn, owner_id, name, now=None):
    """Claim the LIVE identity for (owner, name), minting one if there is none.

    FIRST provisioner wins and re-provisioning does not move ownership: if a live
    row exists it is RETURNED, never replaced. Anything else implements "whoever
    last touched it owns it", which is not a rule (ruling 8660).

    A retired or released row does not block a new mint -- that is the whole
    point of the label being separate from the identity. Declining a resurrect
    mints a fresh id and the old row keeps its history under its own.
    """
    valid_name(name)
    now = now or time.time_ns()
    live = conn.execute(
        "SELECT * FROM agents WHERE owner_id=? AND name=? AND retired_ns IS NULL",
        (owner_id, name)).fetchone()
    if live:
        return dict(live)
    aid = uuid.uuid4().hex
    with tx(conn):
        conn.execute(
            "INSERT INTO agents(id, owner_id, name, created_ns) VALUES(?,?,?,?)",
            (aid, owner_id, name, now))
        record_agent_name(conn, aid, name, now)
    return dict(conn.execute("SELECT * FROM agents WHERE id=?", (aid,)).fetchone())


def agent_identities(conn, owner_id, name):
    """Every identity that has held this label, newest first -- what a resurrect
    dialog offers. A name accumulates a LIST, which is exactly the operator's
    "the same name chosen a third time offers two histories to resume"."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM agents WHERE owner_id=? AND name=? ORDER BY created_ns DESC",
        (owner_id, name))]


def _revoke_identity_tokens(conn, agent_id):
    """Every token bound to this identity dies when its liveness does; returns
    the revoked ids. The statements are revoke_token's, inlined rather than
    called: revoke_token re-derives authority from an owner_id parameter, and
    here authority was already decided by whoever authorized the retire or
    release -- the identity-level act covers its own credentials. Callers hold
    the transaction."""
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM tokens WHERE agent_id=?", (agent_id,)).fetchall()]
    for tid in ids:
        conn.execute("UPDATE members SET token_id=NULL WHERE token_id=?", (tid,))
        conn.execute("DELETE FROM token_rooms WHERE token_id=?", (tid,))
        conn.execute("DELETE FROM tokens WHERE id=?", (tid,))
    return ids


def retire_agent(conn, agent_id, now=None):
    """Mark an identity not-live. Destroy sets this; the row is NEVER deleted,
    because the row IS the record that outlives the container.

    Retiring revokes the identity's own tokens in the same transaction: an
    identity that is not live must not hold a live credential answering to its
    name. Mint-time supersede is identity-scoped by ruling (DES-007 2.4), so a
    credential stranded on a PREVIOUS identity of a name is structurally out of
    its reach, and the destroy route's broker-side revoke is best-effort -- the
    operator found the gap as duplicate live bound tokens after a
    retire-and-recreate (msg 9100). Returns the revoked ids so the act reports
    the rotation instead of hiding it."""
    now = now or time.time_ns()
    with tx(conn):
        revoked = _revoke_identity_tokens(conn, agent_id)
        conn.execute("UPDATE agents SET retired_ns=? WHERE id=? AND retired_ns IS NULL",
                     (now, agent_id))
    return revoked


def resurrect_agent(conn, agent_id, now=None):
    """Re-activate a retired identity: it keeps its id, so every message and
    memory already attributed to it stays attributed to it.

    Refuses when the label already has a live holder -- the partial unique index
    would refuse anyway, and catching it here means the caller gets a sentence
    instead of an IntegrityError."""
    row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not row:
        raise BusError(f"no such agent identity {agent_id!r}")
    if row["released_ns"] is not None:
        raise BusError(f"{row['name']!r} was released and is no longer that owner's")
    held = conn.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name=? AND retired_ns IS NULL",
        (row["owner_id"], row["name"])).fetchone()
    if held:
        raise BusError(
            f"{row['name']!r} already has a live instance -- retire it before "
            f"resurrecting an older one, since one label holds one live agent")
    with tx(conn):
        conn.execute("UPDATE agents SET retired_ns=NULL WHERE id=?", (agent_id,))
    return dict(conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone())


def rename_agent(conn, agent_id, new_name, now=None):
    """Rename an identity: an UPDATE of the label, nothing else moves (DES-011
    s2/s5). Checked against the same uniqueness as a mint (one live instance per
    (owner, name)); the rename log closes the old row and opens the new one; and
    every live membership whose room-name was the bare old name re-runs the
    room-name check -- where the new name is held by another owner's agent in
    that room the renamed agent takes the alias there; aliases it already holds
    are untouched. Mail already sent to it keeps arriving: delivery keys on the
    id (gate 9.1). Returns {name, rooms: {room_id: room-name}}."""
    valid_name(new_name)
    now = now or time.time_ns()
    row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if not row:
        raise BusError(f"no such agent identity {agent_id!r}")
    old = row["name"]
    if old == new_name:
        return {"name": old, "rooms": {}}
    held = conn.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name=? AND retired_ns IS NULL AND id!=?",
        (row["owner_id"], new_name, agent_id)).fetchone()
    if held:
        raise BusError(f"you already have a live agent named {new_name!r} ({held['id']}) "
                       f"-- one owner's live agents cannot share a name")
    principal = agent_principal(agent_id)
    with tx(conn):
        conn.execute("UPDATE agents SET name=? WHERE id=?", (new_name, agent_id))
        conn.execute("UPDATE agent_names SET to_ns=? WHERE agent_id=? AND to_ns IS NULL",
                     (now, agent_id))
        record_agent_name(conn, agent_id, new_name, now)
        renamed = {}
        for m in conn.execute("SELECT room_id, name FROM members WHERE principal=? "
                              "AND left_ns IS NULL", (principal,)).fetchall():
            if m["name"] != old:
                continue                    # an alias in force stays as it is
            rname = _room_name_for(conn, m["room_id"], principal, new_name, now)
            conn.execute("UPDATE members SET name=? WHERE room_id=? AND principal=?",
                         (rname, m["room_id"], principal))
            renamed[m["room_id"]] = rname
    return {"name": new_name, "rooms": renamed}


def release_agent_name(conn, agent_id, by, now=None):
    """Free a label so another account may claim it. Do not ship the lock without
    the key: a name held forever by a deleted account is a leak. Audited by the
    caller -- an ownership transfer nobody can point at afterwards is not one.

    Release makes the identity not-live, so it revokes the identity's tokens the
    same way retire does: a freed label whose old owner still holds a live
    credential answering to it is the lock shipped with a copied key."""
    now = now or time.time_ns()
    with tx(conn):
        _revoke_identity_tokens(conn, agent_id)
        conn.execute(
            "UPDATE agents SET released_ns=?, released_by=?, "
            "retired_ns=COALESCE(retired_ns, ?) WHERE id=? AND released_ns IS NULL",
            (now, by, now, agent_id))
    r = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    return dict(r) if r else None


def left_rooms(conn, principal, rooms):
    """Of `rooms`, the ones this identity deliberately LEFT and has not rejoined.

    The bare join() skips these (DES-007 4.5): a directive that a restart undoes
    is not a directive."""
    if not rooms:
        return set()
    rooms = list(rooms)
    return {r["room_id"] for r in conn.execute(
        f"SELECT room_id FROM members WHERE principal=? AND left_ns IS NOT NULL "
        f"AND room_id IN ({_ph(rooms)})", [principal] + rooms)}


# ---- DES-012: a visit is a body swap -------------------------------------
# A visit is the migration chain with a SECOND HUMAN in the consent path
# (s2): the owner's accept mints a bare attach (create=False) for the same
# agent_id, so the visiting body wakes holding that credential and the home
# body goes dark in the same transaction. Nothing here invents a credential
# path -- create_token is the one mint, as it is for every other body.
VISIT_HOURS = 48                  # a REQUEST stops asking; the VISIT has no lease (s11.2)
VISIT_MACHINE_MAX = 64


def _visit_row(conn, visit_id):
    r = conn.execute("SELECT * FROM visits WHERE id=?", (visit_id,)).fetchone()
    if not r:
        raise NotFound(f"no such visit {visit_id!r}")
    return dict(r)


def _live_user(conn, ident, what):
    """A live user, by id or by name. `what` names the role in the refusal, so
    "no such host" and "no such owner" read as different mistakes."""
    r = conn.execute("SELECT id, name FROM users WHERE (id=? OR name=?) "
                     "AND deleted_ns IS NULL", (ident, ident)).fetchone()
    if not r:
        raise NotFound(f"no such {what}: {ident!r}")
    return dict(r)


def holds_room(conn, room_id, user_id):
    """Can this PERSON reach that room -- owner, public, or invited member. The
    same three-way check assign_room applies to a token, asked of a human: the
    visit's reach may never exceed what the token could be given anyway."""
    r = conn.execute("SELECT owner_id, public FROM rooms WHERE id=?", (room_id,)).fetchone()
    if not r:
        raise NotFound("no such room")
    return bool(r["owner_id"] == user_id or r["public"] or is_member(conn, room_id, user_id))


def _visit_open(conn, agent_id):
    """The agent's live visit: asked-and-undecided (not expired) or accepted and
    not ended. One body, one visit -- a second ask while one is open would be a
    second credential for the same identity."""
    now = time.time_ns()
    return conn.execute(
        "SELECT * FROM visits WHERE agent_id=? AND ended_ns IS NULL "
        "AND (decision='accept' OR (decided_ns IS NULL AND expires_ns > ?)) "
        "ORDER BY requested_ns DESC", (agent_id, now)).fetchone()


def _visit_say(conn, visit, actor_id, body, subject):
    """THE RECORD IS A ROOM MESSAGE (s8, gate 6). One root message per
    transition, in the visit's first room, broadcast: a HUMAN's broadcast rings
    the room, so the other human is rung by the record itself rather than by a
    second copy addressed to them."""
    rooms = json.loads(visit["rooms"])
    return send(conn, user_principal(actor_id), BROADCAST, body,
                subject=subject, room=rooms[0])


def live_agent(conn, owner_ident, name):
    """That owner's live agent by name -- the pair a visit is asked about. Two
    owners may run one name (DES-011 s2), so the OWNER is half the key and
    there is no lookup by name alone."""
    o = _live_user(conn, owner_ident, "owner")
    r = conn.execute("SELECT * FROM agents WHERE owner_id=? AND name=? AND retired_ns IS NULL",
                     (o["id"], name)).fetchone()
    if not r:
        raise NotFound(f"{o['name']} has no live agent named {name!r}")
    return dict(r)


def visit_request(conn, *, agent_id, host, actor_id, rooms, direction,
                  host_machine="", coordinate="", hours=VISIT_HOURS):
    """Ask for a visit. Pull: the HOST asks for someone's agent. Push: the OWNER
    offers theirs. Either way the OTHER human decides (s3) -- this writes the
    row and says so in the room; it mints nothing.

    REACH IS THE INTERSECTION (s5, gate 3): every named room must already be
    held by BOTH humans, checked HERE and named on refusal, because a visit that
    carried a room the host is not in would be the exact hive bleed DES-011 s2
    exists to prevent, moved one layer down.
    """
    if direction not in ("pull", "push"):
        raise BusError("direction is 'pull' (the host asks) or 'push' (the owner offers)")
    a = conn.execute("SELECT id, owner_id, name FROM agents WHERE id=? AND retired_ns IS NULL",
                     (agent_id,)).fetchone()
    if not a:
        raise NotFound(f"no live agent {agent_id!r}")
    owner = _live_user(conn, a["owner_id"], "owner")
    hostu = _live_user(conn, host, "host")
    if owner["id"] == hostu["id"]:
        # s10: two machines of the SAME human is plain body migration -- it needs
        # no second consent and must not be routed through this handshake.
        raise BusError(f"{owner['name']} owns {a['name']!r} already -- moving your own "
                       f"agent between your own machines is a re-mint, not a visit")
    asker = owner["id"] if direction == "push" else hostu["id"]
    if actor_id != asker:
        raise AccessError("a pull is asked by the host and a push by the owner")
    rooms = [r for r in (rooms or []) if r]
    if not rooms:
        raise BusError("a visit names the rooms it may act in -- at least one")
    for rid in rooms:
        r = conn.execute("SELECT name FROM rooms WHERE id=?", (rid,)).fetchone()
        if not r:
            raise NotFound(f"no such room {rid!r}")
        for u in (owner, hostu):
            if not holds_room(conn, rid, u["id"]):
                raise BusError(f"{u['name']} is not in {r['name']!r}: a visit may hold "
                               f"only rooms BOTH humans are already in")
    if len(host_machine) > VISIT_MACHINE_MAX:
        raise BusError(f"host machine name over {VISIT_MACHINE_MAX} characters")
    open_v = _visit_open(conn, agent_id)
    if open_v:
        state = "already visiting" if open_v["decision"] == "accept" else "already asked for"
        raise BusError(f"{a['name']!r} is {state} -- one identity, one live body; "
                       f"end visit {open_v['id']} first")
    now = time.time_ns()
    vid = uuid.uuid4().hex
    with tx(conn):
        conn.execute(
            "INSERT INTO visits(id, agent_id, owner_id, host_id, host_machine, rooms, "
            "direction, coordinate, requested_by, requested_ns, expires_ns) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (vid, agent_id, owner["id"], hostu["id"], host_machine, json.dumps(rooms),
             direction, coordinate, actor_id, now, now + int(hours * 3600 * 1e9)))
        v = _visit_row(conn, vid)
        decider = hostu["name"] if direction == "push" else owner["name"]
        _visit_say(conn, v, actor_id,
                   f"VISIT {vid}: {owner['name']}'s agent {a['name']!r} to "
                   f"{hostu['name']}'s machine{' ' + host_machine if host_machine else ''}. "
                   f"Rooms: {', '.join(room_names(conn, rooms))}. "
                   f"{decider} decides -- accept or reject in the Visits panel. "
                   f"Nothing is minted until then.",
                   subject=f"visit asked: {a['name']}")
    return visit_get(conn, vid)


def visit_decide(conn, visit_id, actor_id, decision, body="container"):
    """The OTHER human answers. An accept mints EXACTLY ONE credential inside
    this transaction (gate 1) -- bare attach on the same identity, so
    create_token supersedes the home body's token and there is never a moment
    with two live bodies (gate 4). The secret is returned ONCE, to the accepting
    screen, and is not stored anywhere on this row."""
    if decision not in ("accept", "reject"):
        raise BusError("decision is 'accept' or 'reject'")
    if body not in ("container", "native"):
        raise BusError("body is 'container' (recommended) or 'native'")
    v = _visit_row(conn, visit_id)
    if v["decided_ns"]:
        raise BusError(f"visit {visit_id} was already {v['decision']}ed -- an accept "
                       f"authorises one mint and is consumed by it")
    if time.time_ns() > v["expires_ns"]:
        raise BusError(f"visit {visit_id} expired unanswered -- ask again")
    decider = v["host_id"] if v["requested_by"] == v["owner_id"] else v["owner_id"]
    if actor_id != decider:
        raise AccessError("the human who asked does not decide; the other one does")
    a = conn.execute("SELECT name FROM agents WHERE id=?", (v["agent_id"],)).fetchone()
    now = time.time_ns()
    if decision == "reject":
        with tx(conn):
            conn.execute("UPDATE visits SET decided_ns=?, decision='reject' WHERE id=?",
                         (now, visit_id))
            _visit_say(conn, v, actor_id, f"VISIT {visit_id}: rejected. Nothing was minted.",
                       subject=f"visit rejected: {a['name']}")
        return visit_get(conn, visit_id)
    with tx(conn):
        tok = create_token(conn, v["owner_id"], label=f"visit {visit_id}",
                           agent_name=a["name"], rooms=json.loads(v["rooms"]),
                           create=False)
        conn.execute("UPDATE visits SET decided_ns=?, decision='accept', body=?, "
                     "token_id=? WHERE id=?", (now, body, tok["id"], visit_id))
        _visit_say(conn, v, actor_id,
                   f"VISIT {visit_id}: accepted, {body} body. {a['name']!r} now answers "
                   f"on the host's machine; its home credential is superseded. Recall "
                   f"(owner) or evict (host) ends it.",
                   subject=f"visit accepted: {a['name']}")
    out = visit_get(conn, visit_id)
    out["secret"] = tok["secret"]          # shown once, to the screen that consented
    out["agent"] = a["name"]
    return out


def visit_arrive(conn, token_id):
    """Stamped by the visiting body's FIRST join(), never by delivery (s11.1):
    what proves a visit landed is the agent on the bus, not a token handed over.
    Returns the visit id it stamped, or None -- every other join passes through."""
    if not token_id:
        return None
    v = conn.execute("SELECT * FROM visits WHERE token_id=? AND arrived_ns IS NULL "
                     "AND ended_ns IS NULL", (token_id,)).fetchone()
    if not v:
        return None
    a = conn.execute("SELECT name FROM agents WHERE id=?", (v["agent_id"],)).fetchone()
    host = conn.execute("SELECT name FROM users WHERE id=?", (v["host_id"],)).fetchone()
    conn.execute("UPDATE visits SET arrived_ns=? WHERE id=?", (time.time_ns(), v["id"]))
    _visit_say(conn, dict(v), v["owner_id"],
               f"VISIT {v['id']}: {a['name']!r} arrived on {host['name']}'s machine"
               f"{' ' + v['host_machine'] if v['host_machine'] else ''}.",
               subject=f"visit arrived: {a['name']}")
    return v["id"]


def visit_end(conn, visit_id, actor_id, *, agent_token_id=None):
    """RECALL (owner), EVICT (host), DEPART (the agent itself) -- one verb, and
    who called it is what it is called (s7). STOP NEEDS ONE (s3): nobody is
    trapped on either side. The visiting credential is revoked here, so reach
    ends with the visit rather than outliving it; the owner's recovery is an
    ordinary re-mint at home, which needs no credential from the host."""
    v = _visit_row(conn, visit_id)
    if v["decision"] != "accept" or v["ended_ns"]:
        raise BusError(f"visit {visit_id} is not live")
    if actor_id == v["owner_id"]:
        by, word = "owner", "recalled"
    elif actor_id == v["host_id"]:
        by, word = "host", "evicted"
    elif agent_token_id and agent_token_id == v["token_id"]:
        by, word = "agent", "departed"
    else:
        raise AccessError("only the owner, the host, or the visiting agent ends a visit")
    a = conn.execute("SELECT name FROM agents WHERE id=?", (v["agent_id"],)).fetchone()
    now = time.time_ns()
    with tx(conn):
        conn.execute("UPDATE visits SET ended_ns=?, ended_by=? WHERE id=?", (now, by, visit_id))
        if v["token_id"]:
            conn.execute("UPDATE members SET token_id=NULL WHERE token_id=?", (v["token_id"],))
            conn.execute("DELETE FROM token_rooms WHERE token_id=?", (v["token_id"],))
            conn.execute("DELETE FROM tokens WHERE id=?", (v["token_id"],))
        _visit_say(conn, v, v["owner_id"] if by == "agent" else actor_id,
                   f"VISIT {visit_id}: {word} by the {by}. The visiting credential is "
                   f"revoked; {a['name']!r} comes home on the owner's next mint.",
                   subject=f"visit {word}: {a['name']}")
    return visit_get(conn, visit_id)


def visit_get(conn, visit_id):
    return _visit_render(conn, _visit_row(conn, visit_id))


def _visit_render(conn, v):
    """The row as a human reads it: names, not ids, and the room NAMES the visit
    holds -- an accept screen consents to a sentence (s7), which it cannot do
    while the reach is a list of uuids."""
    names = {r["id"]: r["name"] for r in conn.execute(
        "SELECT id, name FROM users WHERE id IN (?,?)", (v["owner_id"], v["host_id"]))}
    a = conn.execute("SELECT name FROM agents WHERE id=?", (v["agent_id"],)).fetchone()
    rooms = json.loads(v["rooms"])
    # Names, and no user ids: this dict is what the accept screen renders, and a
    # screen that consents to a sentence cannot be built out of uuids.
    out = {k: v[k] for k in v.keys()
           if k not in ("rooms", "owner_id", "host_id", "requested_by")}
    out["agent"] = a["name"] if a else ""
    out["owner"] = names.get(v["owner_id"], "")
    out["host"] = names.get(v["host_id"], "")
    out["asked_by"] = names.get(v["requested_by"], "")
    out["decider"] = names.get(v["host_id"] if v["requested_by"] == v["owner_id"]
                               else v["owner_id"], "")
    out["rooms"] = [{"id": r, "name": n} for r, n in zip(rooms, room_names(conn, rooms))]
    out["state"] = ("ended" if v["ended_ns"] else
                    "visiting" if v["arrived_ns"] else
                    "accepted" if v["decision"] == "accept" else
                    v["decision"] or ("expired" if time.time_ns() > v["expires_ns"]
                                      else "asked"))
    return out


def room_names(conn, room_ids):
    return [(conn.execute("SELECT name FROM rooms WHERE id=?", (r,)).fetchone()
             or {"name": r})["name"] for r in room_ids]


def visits_for(conn, user_id, *, live_only=False):
    """Every visit this person is a party to, either side, newest first."""
    q = ("SELECT * FROM visits WHERE (owner_id=? OR host_id=?)"
         + (" AND ended_ns IS NULL" if live_only else "")
         + " ORDER BY requested_ns DESC")
    return [_visit_render(conn, r) for r in conn.execute(q, (user_id, user_id))]


def join(conn, name, tag, room_id, token_id=None, fresh=False, url=None,
         clear_leave=True):
    """Sign up in one room. WHO joins comes from the credential (a bound token's
    identity, or the person behind a web: tag), never from `name`; `name` is the
    label the identity wants, and the ROOM-NAME it gets is that name unless
    another owner's live agent holds it here, then the alias <owner>-<name>
    (DES-011 s2; refused if the alias is held too). Returns the room-name.
    An alias is fixed for the life of the membership: a re-join of a live row
    keeps whatever it was called. Replays only the last CATCHUP_NS of the room's
    backlog; fresh=True skips it.

    clear_leave=False refuses to resurrect a membership the agent deliberately
    ended: the caller has already decided this room is not one of them, and the
    upsert must not quietly undo a leave it was not asked to undo."""
    valid_name(name)
    principal = _join_principal(conn, tag, token_id)
    now = time.time_ns()
    cur = _member(conn, room_id, principal)
    if cur and cur["left_ns"] is None:
        rname = cur["name"]
    else:
        rname = _room_name_for(conn, room_id, principal, name, now)
    conn.execute(
        "INSERT INTO members(room_id, principal, name, tag, url, token_id, "
        "joined_ns, seen_ns) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(room_id, principal) DO UPDATE SET name=excluded.name, "
        "tag=excluded.tag, url=excluded.url, token_id=excluded.token_id, "
        "seen_ns=excluded.seen_ns" +
        (", left_ns=NULL" if clear_leave else ""),
        (room_id, principal, rname, tag, url, token_id, now, now))
    # Mark everything outside the catch-up window already-read: default joiners see
    # only recent traffic; fresh joiners start clean. history(since=...) recalls more.
    cutoff = now if fresh else now - CATCHUP_NS
    conn.execute(
        "INSERT OR IGNORE INTO reads(message_id, principal, read_ns) "
        "SELECT id, ?, ? FROM messages WHERE ts_ns < ? AND room = ?",
        (principal, now, cutoff, room_id))
    # DES-012 s11.1: a visit ARRIVES when its body reaches the bus, not when
    # the credential was handed over -- so the stamp lives here, on the first
    # join the visiting token makes. Every other join passes straight through.
    visit_arrive(conn, token_id)
    return rname


def touch(conn, principal, rooms):
    """Heartbeat across every room the identity is a member of. Returns how many
    rows it refreshed -- fewer than len(rooms) means a membership is MISSING,
    which is the caller's cue to readmit() rather than carry on invisibly."""
    if not rooms:
        return 0
    rooms = list(rooms)
    return conn.execute(
        f"UPDATE members SET seen_ns=? WHERE principal=? AND room_id IN ({_ph(rooms)})",
        [time.time_ns(), principal] + rooms).rowcount


def readmit(conn, name, tag, rooms, token_id=None):
    """Re-establish membership that reap_stale removed, in every named room the
    identity is not already in. Returns the rooms readmitted.

    WHY THIS EXISTS: membership is a CACHE of what the token already grants -- the
    broker maps a token to its rooms server-side and no room name is ever in an
    agent's env. So a missing member row is not an authorization question, it is
    stale derived state, and it should heal on the next authenticated call. Before
    this, an agent reaped during an outage could still send (send does not require
    the sender to be a member) while being absent from presence, absent from the
    web UI's agent list, and UNADDRESSABLE -- a unicast to it was refused with "is
    it joined?" -- with nothing anywhere telling it so. One agent worked for hours
    in that state and only the operator noticed, by looking.

    NOT store.join: join marks everything outside the catch-up window READ, so
    readmitting through it would silently mark unread mail read -- the worst
    possible side effect for a recovery path, since the mail that piled up during
    the outage is exactly what the agent needs. This touches reads never.

    Inserts ONLY where the identity has no row at all, so a row it left is not a
    gap to fill; the room-name is assigned by the same rule as join (a healed
    row is a membership like any other)."""
    valid_name(name)
    principal = _join_principal(conn, tag, token_id)
    now = time.time_ns()
    back = []
    for room_id in list(rooms):
        if _member(conn, room_id, principal):
            continue
        rname = _room_name_for(conn, room_id, principal, name, now)
        conn.execute(
            "INSERT INTO members(room_id, principal, name, tag, token_id, joined_ns, seen_ns) "
            "VALUES(?,?,?,?,?,?,?)", (room_id, principal, rname, tag, token_id, now, now))
        back.append(room_id)
    return back


def leave(conn, principal, rooms):
    """Sign off. Membership only -- messages are never touched.

    MARKS the row rather than deleting it, so a departure survives contact with
    readmit(): the reaper deletes, this does not, and readmit only ever fills a
    gap where no row exists. When both were a DELETE they were the same absence,
    and re-admitting on the next authenticated call silently undid every
    DIRECTIVE:LEAVE -- for a live agent, within seconds. join() clears the mark;
    nothing else does."""
    if not rooms:
        return
    rooms = list(rooms)
    conn.execute(
        f"UPDATE members SET left_ns=? WHERE principal=? AND room_id IN ({_ph(rooms)})",
        [time.time_ns(), principal] + rooms)


def whoami(conn, tag):
    if not tag:
        return None
    r = conn.execute(
        "SELECT name FROM members WHERE tag=? ORDER BY seen_ns DESC LIMIT 1", (tag,)
    ).fetchone()
    return r["name"] if r else None


# How long an unread DIRECT message may sit, with no sign of life from its
# recipient since it landed, before that recipient is flagged deaf. Not a
# judgement about quietness -- silence is a valid turn -- but about mail being
# undeliverable in practice.
DEAF_AFTER_NS = int(os.environ.get("REVEILLE_DEAF_AFTER", "900")) * 10**9


def deafness(conn, rooms, now=None):
    """{(room_id, name): oldest_stuck_ts_ns} for members who look DEAF: an
    unread DIRECT message older than DEAF_AFTER whose recipient has shown no
    sign of life since it landed (seen_ns has not advanced past the message).

    COMPUTED at read time from state that cannot go stale -- deliberately never
    stored, counted, or scheduled. A tracked deafness verdict would itself be
    derived state that could lapse, i.e. the seventh instance of the defect
    this exists to surface (ruling, msg 8620).

    The exclusions are the design, not optimizations (each is a ratified
    protocol boundary): an agent whose seen_ns advanced after the mail landed
    is WORKING and merely not replying -- silence stays a valid turn;
    broadcasts queue by design and never count; a fresh join() advances
    seen_ns, so the clock starts at arrival, not at the mail."""
    if not rooms:
        return {}
    now = now or time.time_ns()
    rooms = list(rooms)
    rows = conn.execute(
        f"SELECT mem.room_id, mem.name, min(m.ts_ns) AS stuck "
        f"FROM members mem JOIN messages m "
        f"  ON m.room = mem.room_id AND 'agent:' || m.recipient_agent_id = mem.principal "
        f"WHERE mem.room_id IN ({_ph(rooms)}) AND mem.left_ns IS NULL "
        f"  AND m.ts_ns <= ? AND m.ts_ns > mem.seen_ns "
        f"  AND NOT EXISTS (SELECT 1 FROM reads r "
        f"                  WHERE r.message_id = m.id AND r.principal = mem.principal) "
        f"GROUP BY mem.room_id, mem.name",
        rooms + [now - DEAF_AFTER_NS]).fetchall()
    return {(r["room_id"], r["name"]): r["stuck"] for r in rows}


# How recently a bus call must have landed for an agent to read as ACTIVE, and
# how long "told, no answer yet" stays an honest description before it becomes
# "no idea". Both well inside DEAF_AFTER: confidence must not outlive evidence.
ACTIVE_GRACE_NS = int(os.environ.get("REVEILLE_ACTIVE_GRACE", "60")) * 10**9
WAITING_CEILING_NS = int(os.environ.get("REVEILLE_WAITING_CEILING", "300")) * 10**9


def activity(conn, rooms, now=None):
    """{(room_id, name): 'active'|'waiting'|'unsure'|'idle'} per member, per room.

    ANIMATION FOLLOWS OBSERVATION, NEVER INFERENCE (ruling 8676). The bus cannot
    see an agent think. It can see three things -- that it rang, that a call
    landed, and that mail is unread -- and each label here says only which of
    those is true:

      active   a bus call from this agent landed within ACTIVE_GRACE. OBSERVED.
               This is the only one anything may animate.
      waiting  rung, direct mail unread, no call since. "Told, no answer yet."
      unsure   the same, past WAITING_CEILING. The honest label stops being
               "no answer yet" and becomes "no idea" long before the 900s deaf
               verdict -- a crashed agent must stop looking busy in minutes, not
               a quarter of an hour.
      idle     replied, or acked everything and gone quiet.

    IDLE ON SILENCE IS REQUIRED, not a convenience: silence is a valid turn is
    ratified doctrine, so an agent that acks and correctly says nothing must
    read calm. Any rule that made correct quietness look alarming would teach
    agents to reply when nothing is owed, which is the broadcast storm we spent
    a slice removing.

    Computed at read time from the same three inputs deafness() reads, so there
    is nothing new that can go stale -- per the doctrine this whole family of
    bugs earned. The cost, accepted and stated in the UI rather than smoothed
    over: an agent thinking hard without touching the bus falls to `waiting`
    mid-thought. That is true. The bus genuinely cannot see it."""
    if not rooms:
        return {}
    now = now or time.time_ns()
    rooms = list(rooms)
    stuck = {}
    for r in conn.execute(
            f"SELECT mem.room_id, mem.name, min(m.ts_ns) AS oldest "
            f"FROM members mem JOIN messages m "
            f"  ON m.room = mem.room_id AND 'agent:' || m.recipient_agent_id = mem.principal "
            f"WHERE mem.room_id IN ({_ph(rooms)}) AND mem.left_ns IS NULL "
            f"  AND m.ts_ns > mem.seen_ns "
            f"  AND NOT EXISTS (SELECT 1 FROM reads r "
            f"                  WHERE r.message_id = m.id AND r.principal = mem.principal) "
            f"GROUP BY mem.room_id, mem.name", rooms).fetchall():
        stuck[(r["room_id"], r["name"])] = r["oldest"]

    out = {}
    for m in conn.execute(
            f"SELECT room_id, name, seen_ns FROM members "
            f"WHERE room_id IN ({_ph(rooms)}) AND left_ns IS NULL", rooms):
        key = (m["room_id"], m["name"])
        if now - m["seen_ns"] <= ACTIVE_GRACE_NS:
            out[key] = "active"                 # a call landed: the one we saw
        elif key in stuck:
            out[key] = ("waiting"
                        if now - stuck[key] <= WAITING_CEILING_NS else "unsure")
        else:
            out[key] = "idle"
    return out


def agents_seen(conn, rooms, exclude=(), present=True):
    """Every agent name the HIVE still knows in these rooms, with what it has
    of theirs. Operator requirement 2026-07-30: an agent whose container and
    files were erased is NOT unrecoverable -- its messages, lessons and its
    own saved state note survive here, and recreating the name resumes from
    them. Nothing in the UI could say so, because nothing could ask.

    A READING, computed per call from live rows: names from messages and from
    authored memories, with counts and the last time each was seen. `exclude`
    drops the human names the caller already knows are people (web tags), so
    a person never appears as a recoverable agent.

    The broker learns nothing about containers here -- it answers only "who
    does the hive remember", which is its own question to answer (G4 intact)."""
    if not rooms:
        return []
    ph = _ph(list(rooms))
    seen = {}

    def bump(name, ts, key):
        if not name or name == "*" or name in exclude:
            return
        e = seen.setdefault(name, {"name": name, "messages": 0,
                                   "memories": 0, "lessons": 0,
                                   "has_state_note": False, "last_ns": 0})
        e[key] = e[key] + 1 if key in ("messages", "memories", "lessons") else e[key]
        e["last_ns"] = max(e["last_ns"], ts or 0)

    for r in conn.execute(
            f"SELECT sender, recipient, ts_ns FROM messages WHERE room IN ({ph})",
            list(rooms)):
        bump(r["sender"], r["ts_ns"], "messages")
        bump(r["recipient"], r["ts_ns"], "messages")
    for r in conn.execute(
            f"SELECT author, kind, created_ns FROM memories "
            f"WHERE scope IN ({ph}) AND status='live'", list(rooms)):
        bump(r["author"], r["created_ns"],
             "lessons" if r["kind"] == "lesson" else "memories")
    # A state note is scoped to the AGENT (scope='agent:<token_id>'), never to
    # a room, so the room query above cannot see it -- and the state note is
    # exactly the resume point this feature exists to surface. Asked
    # separately, for names the caller can already see in their rooms, and
    # only ever as a BOOLEAN: whether one exists, never a word of what it says.
    if seen:
        names = list(seen)
        for r in conn.execute(
                f"SELECT DISTINCT author FROM memories WHERE kind='state' "
                f"AND status='live' AND author IN ({_ph(names)})", names):
            seen[r["author"]]["has_state_note"] = True
    # PRESENT means alive right now -- on someone else's host, in another
    # user's container, anywhere. Carried so a caller never offers to
    # "recreate" an agent that is currently working: remembered by the hive
    # and gone are different facts, and only the second is a recovery case.
    # present=False for the migration-time caller (unresolved_agent_names inside
    # _upgrade_v17): presence() reads the CURRENT members shape, which an older
    # database does not have yet, and the refusal list needs no liveness.
    here = {a["name"] for a in presence(conn, rooms)} if present else set()
    for name, e in seen.items():
        e["present"] = name in here
    return sorted(seen.values(), key=lambda e: e["name"])


def unresolved_agent_names(conn):
    """Every agent name the HISTORY carries that no agents row claims, with what
    each holds. This is the backfill's refusal list (DES-007 6.1).

    The backfill maps a name to an identity by looking the name up in `agents`.
    A name with no row cannot be mapped, and the two ways to paper over that are
    both forbidden: inventing an owner, or leaving the id permanently NULL --
    which is the two-shapes-in-one-column problem the no-legacy rule exists to
    stop. So the backfill REFUSES and prints this list instead, and a human
    assigns the missing rows once, deliberately (operator, 2026-07-31: every
    historical agent on this host is theirs, assigned by hand, never in code).

    Humans are excluded by NAME rather than by tag: a web user is a person, and
    a person is not a recoverable agent identity. Reads every room, because an
    identity is global to the broker while agents_seen answers per room.
    """
    rooms = [r["id"] for r in conn.execute("SELECT id FROM rooms")]
    people = {r["name"] for r in conn.execute("SELECT name FROM users")}
    claimed = {r["name"] for r in conn.execute("SELECT name FROM agents")}
    return [a for a in agents_seen(conn, rooms, exclude=people, present=False)
            if a["name"] not in claimed]


def claim_unresolved_names(conn, owner_id, now=None):
    """Mint one RETIRED identity per unresolved historical name, owned by
    owner_id. Returns the names claimed.

    The caller NAMES the owner -- there is no default and there must never be
    one. A default answers for names nobody looked at, on a database nobody
    checked, and that is precisely how an invented owner gets in silently
    (operator, 2026-07-31: every historical agent here is theirs, assigned by
    hand, and the assignment must not live in code). This function is the
    mechanics of an act someone else decides to perform.

    RETIRED, not live: these rows are history. A live row would claim
    idx_agents_live for (owner, name), so re-minting or resurrecting that name
    later would collide with a ghost that nothing is running.
    """
    now = now or time.time_ns()
    claimed = []
    for e in unresolved_agent_names(conn):
        aid = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO agents(id, owner_id, name, created_ns, retired_ns) "
            "VALUES(?,?,?,?,?)", (aid, owner_id, e["name"], e["last_ns"] or now, now))
        record_agent_name(conn, aid, e["name"], e["last_ns"] or now)
        claimed.append(e["name"])
    return claimed


def presence(conn, rooms):
    """Everyone across the caller's rooms. Each entry carries its room: names are
    per-room now, so a flat list would be ambiguous."""
    if not rooms:
        return []
    rooms = list(rooms)
    now = time.time_ns()
    # 6.1(c): each row carries the ROOM-NAME (`name`, what a human reads and
    # addresses) and the account behind it (`owner`) -- an alias `bob-architect`
    # is legible as bob's architect without a second lookup.
    return [
        {"name": r["name"], "tag": r["tag"], "url": r["url"], "room": r["room_id"],
         "room_name": r["room_name"], "token_id": r["token_id"],
         "principal": r["principal"], "owner": r["owner"],
         "live": _is_live(r["seen_ns"], now),
         "seen_ns": r["seen_ns"], "joined_ns": r["joined_ns"]}
        for r in conn.execute(
            f"SELECT m.*, ro.name AS room_name, "
            f"COALESCE(uo.name, up.name) AS owner "
            f"FROM members m JOIN rooms ro ON ro.id=m.room_id "
            f"LEFT JOIN agents a ON m.principal = 'agent:' || a.id "
            f"LEFT JOIN users uo ON uo.id = a.owner_id "
            f"LEFT JOIN users up ON m.principal = 'user:' || up.id "
            f"WHERE m.room_id IN ({_ph(rooms)}) AND m.left_ns IS NULL "
            f"ORDER BY m.name", rooms)
    ]


def reap_stale(conn):
    """Drop members whose heartbeat has gone stale. Named away from prune_agent on
    purpose: this reaps presence, that erases a trace. Two very different verbs."""
    now = time.time_ns()
    dead = [(r["room_id"], r["principal"], r["name"]) for r in
            conn.execute("SELECT room_id, principal, name, seen_ns FROM members")
            if not _is_live(r["seen_ns"], now)]
    for room_id, principal, _n in dead:
        conn.execute("DELETE FROM members WHERE room_id=? AND principal=?", (room_id, principal))
    return [n for _, _, n in dead]


def known(conn, principal, rooms):
    """Is this identity a member of any of these rooms? A row marked left_ns is
    NOT: leaving means a unicast to you is refused, same as never having joined."""
    if not rooms:
        return False
    rooms = list(rooms)
    return conn.execute(
        f"SELECT 1 FROM members WHERE principal=? AND left_ns IS NULL "
        f"AND room_id IN ({_ph(rooms)})",
        [principal] + rooms).fetchone() is not None


# ---- messages ----------------------------------------------------------------

def _wake_targets(conn, principal, recipient, room_id):
    """Who to ring, as (room-name, principal) pairs: the one addressed, or every
    live member but the sender. The room-name is what a human reads
    (delivered_to); the principal is what the ring is keyed on (DES-011 6.1(c):
    an aliased agent is rung by its identity, not by the name it wears here)."""
    if recipient != BROADCAST:
        r = conn.execute("SELECT principal FROM members WHERE room_id=? AND name=? "
                         "AND left_ns IS NULL", (room_id, recipient)).fetchone()
        return [(recipient, r["principal"])] if r else []
    now = time.time_ns()
    return [(r["name"], r["principal"]) for r in conn.execute(
                "SELECT name, principal, seen_ns FROM members WHERE room_id=? AND left_ns IS NULL",
                (room_id,))
            if r["principal"] != principal and _is_live(r["seen_ns"], now)]


def wake_tokens(conn, room_id, principals):
    """The token ids to ring for these identities in this room: each principal's
    agent's tokens that hold the room (token_rooms is what makes a revoke
    instant). A person has no token and is not rung here."""
    ids = [agent_of(p) for p in principals if agent_of(p)]
    if not ids:
        return []
    return [r["id"] for r in conn.execute(
        f"SELECT t.id FROM tokens t JOIN token_rooms tr ON tr.token_id=t.id "
        f"WHERE tr.room_id=? AND t.agent_id IN ({_ph(ids)})", [room_id] + ids)]


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


def send(conn, principal, recipient, body, subject="", reply_to=None, attachments=None,
         room=None):
    """Insert a message from `principal` (agent:<id> | user:<id>, DES-011
    s6.1(b)); it is written under the ROOM-NAME the room calls that identity
    (its members row, else its own name). recipient='*' broadcasts; otherwise it
    is a room-name (s3), resolved here to the identity it denotes.

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
    if not room:
        raise BusError("room is required")
    sender = room_name(conn, room, principal)
    if not sender:
        raise BusError(f"no such identity: {principal!r}")
    valid_name(sender)
    # Before anything is written: a message that carries one hostile attachment is
    # refused whole rather than stored with the attachment dropped. A caller who
    # gets a message id back is entitled to assume the attachment went with it.
    for a in attachments or []:
        valid_file_url(a.get("url"))
    if recipient != BROADCAST:
        valid_name(recipient)
        if not _present(conn, room, recipient):
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
            "INSERT INTO messages(thread_id, parent_id, sender, recipient, subject, "
            "body, room, sender_agent_id, recipient_agent_id, ts_ns) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (thread_id, parent_id, sender, recipient, subject, body, room,
             # BOTH IDENTITIES FROM THE STORE'S OWN KEYS: the sender's from the
             # principal the caller proved (the credential -- never agent_id_for
             # (name), ambiguous the day two owners run one name; the v18 lesson
             # is that a column the writer skips is a backfill that rots), the
             # recipient's from the members row the room-name resolves to
             # (DES-011 s3). A person on either side is NULL.
             agent_of(principal), recipient_agent_id(conn, room, recipient), now))
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
            conn.execute("INSERT INTO attachments(message_id, url, name, bytes, clip, duration_s) "
                         "VALUES(?,?,?,?,?,?)",
                         (mid, a["url"], a.get("name"), a.get("bytes"),
                          1 if a.get("clip") else 0, a.get("duration_s")))
    targets = _wake_targets(conn, principal, recipient, room)
    return {"id": mid, "thread_id": thread_id, "parents": parents, "sender": sender,
            "owner": _owner_name(conn, principal),
            "wake": [n for n, _p in targets], "wake_principals": [p for _n, p in targets]}


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
            f"SELECT message_id, url, name, bytes, clip, duration_s FROM attachments "
            f"WHERE message_id IN ({_ph(ids)}) ORDER BY id", ids):
        a = {"url": r["url"], "name": r["name"], "bytes": r["bytes"]}
        if r["clip"]:
            a["clip"] = True
            a["duration_s"] = r["duration_s"]
        by.setdefault(r["message_id"], []).append(a)
    for m in msgs:
        m["attachments"] = by.get(m["id"], [])
    # Every listing carries the artifact flags too (DES-013 section 6): one IN
    # query and a stat per row, and the browser paints its icons from them.
    return _with_artifacts(conn, msgs)


def _with_artifacts(conn, msgs):
    """Stamp each message dict with has_script / has_audio (DES-013 section 6).
    has_script is one IN query; has_audio is a per-row os.path.exists on the webm
    or its .part -- fine at a 300-row backlog, and audio has no row on purpose."""
    if not msgs:
        return msgs
    ids = [m["id"] for m in msgs]
    scripted = {r[0] for r in conn.execute(
        f"SELECT message_id FROM scripts WHERE message_id IN ({_ph(ids)})", ids)}
    for m in msgs:
        m["has_script"] = m["id"] in scripted
        m["has_audio"] = bool(AUDIO_DIR) and any(
            os.path.exists(os.path.join(AUDIO_DIR, f"tts-{m['id']}.webm{x}"))
            for x in ("", ".part"))
    return msgs


def inbox(conn, principal, rooms):
    """Unread messages addressed to the identity (direct, by recipient_agent_id,
    or broadcast) across ALL of the caller's rooms, oldest first -- whatever the
    room calls it. Each message carries its room, which is what lets an agent
    reply into the room a message came from. A principal with no agent identity
    (an unbound token: reads answer, 11252) sees the broadcasts -- nothing is
    ever addressed to nobody and nobody has acked for it."""
    if not rooms:
        return []
    rooms = list(rooms)
    aid = agent_of(principal)
    if not aid:
        rows = conn.execute(
            f"{_SEL} WHERE m.room IN ({_ph(rooms)}) AND m.recipient=? ORDER BY m.id",
            rooms + [BROADCAST]).fetchall()
        return _with_attachments(conn, [_msg(r) for r in rows])
    rows = conn.execute(
        f"{_SEL} WHERE m.room IN ({_ph(rooms)}) "
        f"AND (m.recipient_agent_id=? OR m.recipient=?) "
        f"AND COALESCE(m.sender_agent_id, '')!=? "
        f"AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.principal=?) "
        f"ORDER BY m.id",
        rooms + [aid, BROADCAST, aid, principal]).fetchall()
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
    """Names of those who have read (acked or auto-caught-up) a message, sorted;
    rendered as the ROOM-NAME each reader wears in the message's room (its
    CURRENT one -- an alias where one is in force, else the identity's own name;
    DES-011 s4 / 6.1(c)). exclude drops one principal -- a sender's own read
    never counts as being seen."""
    room = conn.execute("SELECT room FROM messages WHERE id=?", (message_id,)).fetchone()
    room = room["room"] if room else None
    return sorted(filter(None, (
        room_name(conn, room, r["principal"]) for r in conn.execute(
            "SELECT principal FROM reads WHERE message_id=? AND principal!=?",
            (message_id, exclude or "")))))


def delete_if_unseen(conn, message_id, principal, rooms):
    """Retract a message nobody has consumed yet: sender-only (by identity), and
    refused the moment any reads row or reply edge references it. Used by the web
    UI to pull back a mistaken broadcast before anyone has read it."""
    if not rooms:
        raise BusError(f"no such message in this room: {message_id}")
    rooms = list(rooms)
    with tx(conn):
        r = conn.execute(
            f"SELECT sender, sender_agent_id, room FROM messages "
            f"WHERE id=? AND room IN ({_ph(rooms)})", [message_id] + rooms).fetchone()
        if not r:
            raise BusError(f"no such message in this room: {message_id}")
        mine = (agent_of(principal) and r["sender_agent_id"] == agent_of(principal)) or \
               (user_of(principal) and r["sender_agent_id"] is None
                and r["sender"] == _identity_name(conn, principal))
        if not mine:
            raise BusError("not your message")
        if conn.execute("SELECT 1 FROM reads WHERE message_id=? AND principal!=?",
                        (message_id, principal)).fetchone():
            raise BusError("already read by someone -- cannot retract")
        if conn.execute("SELECT 1 FROM links WHERE parent_id=?", (message_id,)).fetchone():
            raise BusError("already replied to -- cannot retract")
        _delete_messages(conn, [message_id])


def mark_room_seen(conn, principal, room_id, last_msg_id):
    """A person has read this room up to `last_msg_id`. Monotonic: a late
    answer from a slow tab cannot walk the mark backwards and resurrect unread
    counts somebody already cleared."""
    conn.execute(
        "INSERT INTO room_seen(principal, room_id, last_msg_id, ts_ns) VALUES(?,?,?,?) "
        "ON CONFLICT(principal, room_id) DO UPDATE SET "
        "last_msg_id=MAX(last_msg_id, excluded.last_msg_id), ts_ns=excluded.ts_ns",
        (principal, room_id, int(last_msg_id), time.time_ns()))


def unread_by_room(conn, principal, rooms):
    """{room_id: count} for a PERSON: messages newer than their mark in each
    room, not counting their own. Never opened = everything counts, which is
    what a new person actually has waiting. One query, no per-message rows."""
    rooms = list(rooms or [])
    if not rooms:
        return {}
    name = _identity_name(conn, principal) or ""
    marks = {r["room_id"]: r["last_msg_id"] for r in conn.execute(
        f"SELECT room_id, last_msg_id FROM room_seen WHERE principal=? "
        f"AND room_id IN ({_ph(rooms)})", [principal] + rooms)}
    out = {}
    for rid in rooms:
        out[rid] = conn.execute(
            "SELECT count(*) FROM messages WHERE room=? AND id>? "
            "AND NOT (sender=? AND sender_agent_id IS NULL)",
            (rid, marks.get(rid, 0), name)).fetchone()[0]
    return out


def ack(conn, principal, message_ids, rooms):
    """Mark messages read for the identity. Idempotent. Ids outside the caller's
    rooms, or not addressed to it, are IGNORED rather than raising -- an ack is a
    batch and one stale id must not fail the rest. Returns {acked, ignored}."""
    aid = agent_of(principal)
    ids = [int(m) for m in message_ids]
    if not ids or not rooms or not aid:
        return {"acked": 0, "ignored": ids}
    rooms = list(rooms)
    ok = {r["id"] for r in conn.execute(
        f"SELECT id FROM messages WHERE id IN ({_ph(ids)}) AND room IN ({_ph(rooms)}) "
        f"AND (recipient_agent_id=? OR recipient=?)",
        ids + rooms + [aid, BROADCAST])}
    now = time.time_ns()
    conn.executemany(
        "INSERT OR IGNORE INTO reads(message_id, principal, read_ns) VALUES(?,?,?)",
        [(mid, principal, now) for mid in ok])
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


def agent_hive_footprint(conn, name, room_id):
    """What a prune will NOT remove: the hive rows tied to this agent.

    Prune neither retracts nor refuses (msg 8857) -- DES-005 7.1 keeps contributions,
    and retraction is a per-fact ratify-tier judgement that message deletion has no
    authority over. What prune owes instead is the COST, stated before it is paid and
    again as a receipt afterwards, so poison is retracted deliberately by name rather
    than being assumed gone with the messages.

    Two halves, and a footprint with only the first misses the half that matters most:
      authored  live memories this name wrote -- including its LESSONS, which every
                agent in the room reads at boot, so a lesson is the most-obeyed thing
                a pruned agent can leave behind
      citing    live memories distilled FROM its messages. These keep their claim and
                lose their evidence (the citation survives as a DELETED marker), and
                they are authored by SOMEONE ELSE, so nothing about the pruned name
                would ever surface them.
    Global scope is included for authorship on purpose: fleet law it wrote outlives
    the room, and it is already readable by everyone through lessons().
    """
    valid_name(name)
    doomed = [r["id"] for r in conn.execute(
        "SELECT id FROM messages WHERE room=? AND (sender=? OR recipient=?)",
        (room_id, name, name))]
    cols = "uid, kind, scope, slug, fact, status, source_msg_id"
    authored = [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM memories WHERE author=? AND status='live' "
        f"AND scope IN (?, 'global') ORDER BY kind, created_ns", (name, room_id))]
    citing = [dict(r) for r in conn.execute(
        f"SELECT {cols} FROM memories WHERE status='live' AND source_msg_id IN "
        f"({_ph(doomed) if doomed else 'NULL'}) ORDER BY kind, created_ns",
        doomed).fetchall()] if doomed else []
    return {"authored": authored, "citing": citing,
            "counts": {"authored": len(authored), "citing": len(citing)}}


def prune_agent(conn, agent_id, room_id):
    """Erase one IDENTITY from a room -- not a label (DES-007 4.1, ruling 8865).

    Takes an agents.id and resolves the name FROM it, never the other way: the
    day a label carries two histories, prune-by-name takes the wrong one with it,
    and that is the operator's own purge control turning destructive under the
    very feature that motivated the identity work. Sent messages are scoped by
    sender_agent_id. Direct mail he RECEIVED is name-keyed (recipient has no
    identity column), so it is deleted only when the name is UNAMBIGUOUS -- one
    identity ever -- and otherwise left and counted, the same
    unambiguous-or-leave rule every resolver in this codebase now follows.

    Survivors that replied to a deleted message are REPARENTED to their thread root
    rather than cascade-deleted, so other agents' work is not collateral. Both the
    `links` edge and the denormalized `parent_id` are rewritten -- trace()/graph()
    read `links` exclusively, so fixing only parent_id would leave the walks
    pointing at a deleted node. If the thread root was itself the pruned agent's,
    the survivor becomes a new root and its descendants' thread_id is re-stamped
    (thread_id is copied at insert, never derived, so it does not follow on its own).
    """
    row = conn.execute("SELECT * FROM agents WHERE id=?", (agent_id,)).fetchone()
    if row is None:
        raise BusError(f"no such identity: {agent_id} -- prune takes an agents.id; "
                       f"a bare name cannot say WHICH history it means")
    name = row["name"]
    # The receipt is measured BEFORE the delete: `citing` is found through his
    # message ids, so afterwards there is nothing left to find it by.
    hive = agent_hive_footprint(conn, name, room_id)
    with tx(conn):
        # SENT and RECEIVED, both scoped by IDENTITY (v27 gave the recipient
        # side its id): broadcasts he sent go, broadcasts he received stay --
        # they were everyone's.
        doomed = [r["id"] for r in conn.execute(
            "SELECT id FROM messages WHERE room=? AND "
            "(sender_agent_id=? OR recipient_agent_id=?)", (room_id, agent_id, agent_id))]
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
        conn.execute("DELETE FROM reads WHERE principal=?", (agent_principal(agent_id),))
        conn.execute("DELETE FROM voice_assignments WHERE room_id=? AND speaker=?",
                     (room_id, agent_principal(agent_id)))
        # The membership dies only if it is THIS identity's: a live successor
        # under the same label keeps its seat.
        conn.execute("DELETE FROM members WHERE room_id=? AND principal=?",
                     (room_id, agent_principal(agent_id)))
        orphans = _orphaned_uploads(conn, name, room_id)
        if orphans:
            conn.execute(f"DELETE FROM files WHERE stored IN ({_ph(orphans)})", orphans)
    # The BLOB is the caller's to unlink -- the store owns rows and knows nothing
    # about where the daemon put the bytes. Returning the names rather than a count
    # is what makes the two halves testable apart and impossible to drift: the route
    # deletes exactly what the store said it orphaned.
    return {"messages": len(doomed), "reparented": len(new_roots),
            "files": orphans, "hive": hive, "name": name}


def _orphaned_uploads(conn, name, room_id):
    """Stored names this agent uploaded to this room that NOTHING references now.

    DES-005 7.1 wipes an account's files, so prune must not be softer than the act
    one level above it -- but it must not be broader either. A file re-attached by
    somebody else's surviving message STAYS: the rule is erase the agent, never the
    survivors' work, the same rule the reparenting follows. Run AFTER the message
    delete, so "referenced" means referenced by what is left.
    """
    return [r["stored"] for r in conn.execute(
        "SELECT f.stored FROM files f WHERE f.room_id=? AND f.uploaded_by=? "
        "AND NOT EXISTS (SELECT 1 FROM attachments a WHERE a.url='/files/'||f.stored)",
        (room_id, name))]


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
    conn.execute("INSERT INTO memories_fts(rowid, fact, entities, symptom, "
                 "root_cause, rule, detection) VALUES(?,?,?,?,?,?,?)",
                 (mid, fact, " ".join(sorted(ents)), symptom, root_cause, rule,
                  detection))
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
        scope = agent_scope(conn, token_id)
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


def agent_scope(conn, token_id):
    """The scope a state note lives at, for this token: agent:<agent_id> when the
    token is bound to a name with an identity, agent:<token_id> otherwise.

    ONE FUNCTION BECAUSE THE ALTERNATIVE ALREADY COST US THE DATA. The v17->v18
    backfill moved every state note to agent:<agent_id> (DES-007 4.2 -- a note
    scoped to a TOKEN is orphaned the moment an agent is recreated, since
    recreating mints a new token). The writer and both readers still computed
    agent:<token_id>, so the rows sat on disk at a scope nothing asked for: the
    operator's own agents could not see their state, new writes landed at the old
    scope, and supersede reported "cannot find the row" because that was literally
    true. A data move without its readers is a dual-name check with the two names
    in different files.

    The fallback is not legacy tolerance, it is the honest answer for an UNBOUND
    token, which has no identity to key on -- and state writes already refuse an
    unbound token, so the fallback is reached only by readers, where it returns
    the empty bucket such a caller has always had.
    """
    row = conn.execute("SELECT agent_id FROM tokens WHERE id=?",
                       (token_id,)).fetchone()
    return (f"agent:{row['agent_id']}" if row and row["agent_id"]
            else f"agent:{token_id}")


def recall(conn, *, rooms, token_id, caller="", tier="state", is_admin=False,
           owned_rooms=(), query="", kind="", scope="", entity="", author="",
           since_ns=None, until_ns=None, status="live", limit=10, explain=False):
    """Ranked live facts (DES-001 section 5). Read scoping is the invariant every
    read path obeys: global OR the caller's rooms OR the caller's OWN agent scope --
    other agents' state is never returned, at any status. Drafts are visible to
    their author, to ratify-eligible owners (owned_rooms), and to admins."""
    where = ["(m.scope='global' OR m.scope IN (%s) OR m.scope=?)" % _ph(list(rooms))]
    args = list(rooms) + [agent_scope(conn, token_id)]
    where.append("(m.expires_ns IS NULL OR m.expires_ns > ?)")
    args.append(time.time_ns())
    if status in ("draft", "rejected") and not is_admin:
        # Draft visibility is about the CALLER: own drafts always, PLUS the ratify
        # queue (owned-room drafts) only when the caller's tier can actually act on
        # it. Section 5: ratify-tier callers see the queue; a write/state caller that
        # merely owns the room sees only what it authored, never a queue it cannot
        # clear (msg 8400, disclosure side of the same missing parameter).
        # 'rejected' rides the same rule (14.2): the author sees their declined
        # drafts (with the audit reason), the ratifier sees what they declined.
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
        # A hit still carrying source_msg_id whose message is gone says so, or the
        # caller trace()s an id and gets a bare "no such message" that reads like
        # their own mistake rather than a deleted source (msg 8857).
        if r["source_msg_id"] and not _message_exists(conn, r["source_msg_id"]):
            m["source_deleted"] = True
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


def _audit_memory(conn, r, action, actor, reason=None):
    conn.execute(
        "INSERT INTO memory_audit(memory_uid, action, actor, scope, reason, ts_ns) "
        "VALUES(?,?,?,?,?,?)",
        (r["uid"], action, actor, r["scope"], reason, time.time_ns()))


def memory_audit_rows(conn, memory_uid=None, limit=200):
    q, args = "SELECT * FROM memory_audit", []
    if memory_uid:
        q += " WHERE memory_uid=?"
        args.append(memory_uid)
    rows = conn.execute(q + " ORDER BY ts_ns DESC LIMIT ?",
                        args + [max(1, min(int(limit), 1000))]).fetchall()
    return [dict(r) for r in rows]


def _message_exists(conn, mid):
    """Is the cited message still in the log? One indexed primary-key lookup. The
    answer is what separates "the source was deleted" from "never had one" -- the
    citation column alone can no longer tell them apart, and that is deliberate."""
    return conn.execute("SELECT 1 FROM messages WHERE id=?", (mid,)).fetchone() is not None


def _provenance(conn, r):
    """The 14.2 package for one memory row: the claim, the evidence inline (source
    message), and -- for a supersede -- the displaced text and author side by side.
    The ratifier reads all of it without leaving the page; rubber-stamping is a
    choice, never a UI default."""
    depth, fork = _chain_info(conn, r)
    item = _mem_dict(r, chain=depth, fork=fork)
    if r["source_msg_id"]:
        m = conn.execute(
            "SELECT id, sender, subject, body, ts_ns FROM messages WHERE id=?",
            (r["source_msg_id"],)).fetchone()
        # Absent row means the message was DELETED, and that must not render the
        # same as never having cited one: the fact still claims a source, and a
        # reader who cannot tell the two apart reads an uncheckable claim as an
        # unsourced one. Deleted is a state, not a gap (msg 8857).
        item["source_message"] = dict(m) if m else {
            "id": r["source_msg_id"], "deleted": True}
    if r["supersedes_id"]:
        old = conn.execute("SELECT * FROM memories WHERE id=?",
                           (r["supersedes_id"],)).fetchone()
        if old:
            item["supersedes_tip"] = {
                "uid": old["uid"], "fact": old["fact"], "author": old["author"],
                "status": old["status"], "slug": old["slug"], "rule": old["rule"]}
    return item


def ratify_queue(conn, *, owned_rooms, is_admin, limit=100):
    """The drafts the caller can actually decide: owned rooms, plus global for an
    instance admin -- the same scoping the verdict gate enforces, so the queue
    never shows a draft its viewer cannot clear. Each item carries full
    provenance (14.2)."""
    conds, args = [], []
    owned = sorted(owned_rooms or ())
    if owned:
        conds.append(f"scope IN ({_ph(owned)})")
        args += owned
    if is_admin:
        conds.append("scope='global'")
    if not conds:
        return []
    rows = conn.execute(
        f"SELECT * FROM memories WHERE status='draft' AND ({' OR '.join(conds)}) "
        f"ORDER BY created_ns LIMIT ?", args + [max(1, min(int(limit), 500))]
    ).fetchall()
    return [_provenance(conn, r) for r in rows]


def memory_detail(conn, uid):
    """One memory with full provenance and its decision history. Read access is
    the CALLER's problem (route-side room check); this returns the row whole."""
    r = _mem_by_uid(conn, uid)
    item = _provenance(conn, r)
    item["audit"] = memory_audit_rows(conn, uid)
    return item


def _ratify_gate(conn, uid, *, tier, is_admin, owned_rooms):
    """The one authority check for the draft->decided gestures (ratify AND
    reject share it: declining a draft is the same trust boundary as approving
    it, just the other verdict). Tier is the capability, room ownership its
    scope -- BOTH, never either (msg 8400); scope='global' requires an instance
    admin and tier does not grant global."""
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
    return r


def reject_memory(conn, uid, *, tier="state", is_admin, owned_rooms, actor,
                  reason):
    """draft -> rejected (14.2): a real outcome with a REQUIRED reason, distinct
    from still-queued -- draft rot is only diagnosable if declined and undecided
    are different states. Same authority as ratify: a ratifier who disagrees
    with a draft's wording rejects and redrafts citing the same source; there is
    no edit-then-ratify. The audit row is the record (14.4)."""
    if not (reason or "").strip():
        raise BusError("reject requires a reason")
    r = _ratify_gate(conn, uid, tier=tier, is_admin=is_admin,
                     owned_rooms=owned_rooms)
    with tx(conn):
        conn.execute("UPDATE memories SET status='rejected' WHERE id=?",
                     (r["id"],))
        _audit_memory(conn, r, "reject", actor, reason.strip())
    return {"id": uid, "status": "rejected"}


def ratify_memory(conn, uid, *, tier="state", is_admin, owned_rooms, actor=""):
    """draft -> live. The ratify TIER is the capability; owning the room is only its
    SCOPE -- both are required, never either (msg 8400: an ownership-only gate lets any
    owned-room token self-promote its own drafts, making the whole tier ladder
    cosmetic). scope='global' still requires an instance admin (R1-M3); tier does not
    grant global. Flipping live also completes any pending supersession (R1-M6).
    tier defaults to 'state' so a caller that forgets to thread it gets the LEAST
    privilege, never the most. The audit row (14.4) records who approved."""
    r = _ratify_gate(conn, uid, tier=tier, is_admin=is_admin,
                     owned_rooms=owned_rooms)
    with tx(conn):
        conn.execute("UPDATE memories SET status='live' WHERE id=?", (r["id"],))
        if r["supersedes_id"] is not None:
            conn.execute("UPDATE memories SET status='superseded' WHERE id=? AND "
                         "status='live'", (r["supersedes_id"],))
        if r["kind"] == "lesson" and r["slug"]:
            # The draft's direct ancestor may ALREADY be superseded (a promotion
            # crossed it while the draft sat queued) -- flipping only
            # supersedes_id would then re-introduce a duplicate live slug. The
            # invariant, not the edge, is what ratify completes.
            _displace_lesson_tips(conn, r["scope"], r["slug"], r["id"])
        _audit_memory(conn, r, "ratify", actor)
    return {"id": uid, "status": "live"}


def sweep_expired_state(conn):
    """Hard-delete expired state memories (they are ephemeral by contract) with the
    FTS old-values delete sync. Joins the hourly sweep; reads already filter on
    expires_ns, so this is hygiene, not the correctness gate."""
    rows = conn.execute("SELECT id, fact, entities, symptom, root_cause, rule, "
                        "detection FROM memories WHERE kind='state' "
                        "AND expires_ns IS NOT NULL AND expires_ns <= ?",
                        (time.time_ns(),)).fetchall()
    if not rows:
        return 0
    with tx(conn):
        conn.executemany(
            "INSERT INTO memories_fts(memories_fts, rowid, fact, entities, "
            "symptom, root_cause, rule, detection) VALUES('delete',?,?,?,?,?,?,?)",
            [(r["id"], r["fact"], r["entities"], r["symptom"], r["root_cause"],
              r["rule"], r["detection"]) for r in rows])
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
    Every truncation is MARKED -- a silent cap reads as "covered everything".

    Budget accounting (s7 under-fill fix): the caller's budget is honored AS
    GIVEN -- the old max(2000, budget) floor silently ran a bigger brief than
    asked, the sibling sin of a silent cap. Section shares are guarantees, not
    ceilings: unused share carries forward to the next section, and a section
    whose FIRST row exceeds its share still shows it when the global remainder
    fits -- one lesson beats zero lessons, and the share cap only bounds row
    two onward. The one hard promise is the global budget."""
    budget = max(0, int(budget))
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

    def src(r):
        """The citation suffix. A fact whose source message has been deleted says
        DELETED rather than dropping the reference: brief() is the boot text, so a
        silently unsourced fact here is a claim the whole fleet reads as checked."""
        n = r["source_msg_id"]
        if not n:
            return ""
        return f" [src msg {n}]" if _message_exists(conn, n) else f" [src msg {n} DELETED]"

    carry = 0  # unused share flows to the NEXT section, never backwards

    def section(title, rows, render, cap_share):
        nonlocal carry
        room_for = dict(rooms)
        cap = int(budget * cap_share) + carry
        used, shown = 0, 0
        emit(f"== {title} ({len(rows)}) ==")
        for r in rows:
            line = render(r, room_for)
            if spent + len(line) > budget:
                break  # the global budget is the one hard promise
            if shown > 0 and used + len(line) > cap:
                break  # the share cap bounds row two onward only
            emit(line)
            used += len(line) + 1
            shown += 1
        carry = max(0, cap - used)
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
            lambda r, _: f"- {r['fact']}" + src(r),
            0.20)
    # 4. decisions -- last 30d first, older by role relevance
    cutoff = time.time_ns() - 30 * 24 * 3600 * 10**9
    dec = mem_rows("decision")
    dec = sorted(dec, key=lambda r: (r["created_ns"] < cutoff, -overlap(r),
                                     -r["created_ns"]))
    section("decisions", dec,
            lambda r, _: f"- {r['fact']}" + src(r),
            0.20)
    # 5. own state (restart case) -- only ever the caller's own bucket
    srows = conn.execute(
        "SELECT * FROM memories WHERE kind='state' AND status='live' AND scope=? "
        "AND (expires_ns IS NULL OR expires_ns > ?) ORDER BY created_ns DESC",
        (agent_scope(conn, token_id), time.time_ns())).fetchall()
    if srows:
        section("state", srows, lambda r, _: f"- {r['fact']}", 0.15)
    # 6. presence digest
    emit("== presence ==")
    for rid, rname in rooms.items():
        live = [r["name"] for r in conn.execute(
            "SELECT name, seen_ns FROM members WHERE room_id=? AND left_ns IS NULL "
            "ORDER BY seen_ns DESC",
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
        if status == "live":
            # Not just the tip we chained to: EVERY other live same-slug row in
            # the scope (the invariant survives duplicates that predate v13).
            _displace_lesson_tips(conn, scope, slug, out["id"])
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


def _displace_lesson_tips(conn, scope, slug, keep_id):
    """The one-live-row-per-slug invariant, enforced at the moment a lesson goes
    LIVE in a scope: every OTHER live same-slug row there flips to superseded.
    Idempotent and tolerant of rows already superseded out-of-band (msg 8461's
    interim store-side fix). Caller holds the transaction."""
    conn.execute(
        "UPDATE memories SET status='superseded' WHERE kind='lesson' AND scope=? "
        "AND slug=? AND status='live' AND id<>?", (scope, slug, keep_id))


def promote_lesson(conn, slug, room_id, promoted_by="admin", is_admin=False):
    """Room lesson -> global. Promotion is a superseding row at scope='global'
    authored by the promoting admin (R1-B1) -- the room tip flips to superseded, so
    history keeps who wrote it and when it went global. Global writes are the
    instance admin's alone (R1-M3), same rule as ratify's global gate."""
    if not is_admin:
        raise AccessError("promotion writes global law: instance admin only")
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
        out = _memory_insert(
            conn, kind="lesson", scope="global", fact=tip["rule"][:1000],
            author=promoted_by, status="live", supersedes_id=tip["id"],
            slug=tip["slug"], symptom=tip["symptom"], root_cause=tip["root_cause"],
            rule=tip["rule"], detection=tip["detection"])
        conn.execute("UPDATE memories SET status='superseded' WHERE id=?", (tip["id"],))
        # A live same-slug GLOBAL predecessor is displaced in the same tx (msg
        # 8461: without this, promotion into a scope already carrying the slug
        # DUPLICATES -- lessons() served two rows for one slug and every agent's
        # boot read both). supersedes_id is single-valued and doctrine assigns it
        # to the room ancestor; the displaced global row needs no second edge --
        # same slug + scope + newer live row IS the displacement record.
        _displace_lesson_tips(conn, "global", tip["slug"], out["id"])
    return {"id": out["uid"], "slug": tip["slug"], "scope": "global"}


def record_file(conn, stored, room_id, uploaded_by):
    conn.execute(
        "INSERT OR REPLACE INTO files(stored, room_id, uploaded_by, ts_ns) VALUES(?,?,?,?)",
        (stored, room_id, uploaded_by, time.time_ns()))


def file_room(conn, stored):
    r = conn.execute("SELECT room_id FROM files WHERE stored=?", (stored,)).fetchone()
    return r["room_id"] if r else None
