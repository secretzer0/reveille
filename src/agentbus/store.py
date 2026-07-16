#!/usr/bin/env python3
"""SQLite broker core: presence, threaded messages, per-agent read state.

Pure stdlib. This is the data plane -- no MCP, no wake, no identity magic; those
live in daemon.py. Everything here is a function over a sqlite3 connection so it
is trivially testable, and it is transport-agnostic (stdio or HTTP, same core).

Model
-----
agents     one row per joined session (name <-> session tag, last-seen heartbeat).
messages   append-only. recipient = an agent name, or '*' for broadcast.
           thread_id = the root message's id; parent_id = the direct reply target.
reads      (message_id, agent) -- an agent has consumed a message.

A message is UNREAD for agent A when A is a recipient (direct or '*'), A is not
the sender, and no reads row exists for (message, A). Catch-up falls out of this
for free: join() pre-inserts reads rows for everything older than CATCHUP_NS, so
a joiner replays only recent traffic (--fresh pre-inserts for the whole backlog,
starting clean). Older mail stays queryable via history().
"""
import os
import re
import sqlite3
import time

# LIVE = heartbeat within this window. Exceeds the standup silence window (30m
# default) so an idle-but-alive agent that only wakes on its heartbeat stays LIVE.
LIVE_TTL_NS = 40 * 60 * 1_000_000_000
# Join catch-up window: a joiner replays only this much backlog by default; older
# mail is auto-marked read. Explicit recall of any period stays available via
# history(since=...).
CATCHUP_NS = 15 * 60 * 1_000_000_000
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
BROADCAST = "*"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    name      TEXT PRIMARY KEY,
    tag       TEXT,
    url       TEXT,
    joined_ns INTEGER NOT NULL,
    seen_ns   INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id INTEGER,
    parent_id INTEGER REFERENCES messages(id),
    sender    TEXT NOT NULL,
    recipient TEXT NOT NULL,
    subject   TEXT NOT NULL DEFAULT '',
    body      TEXT NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_msg_recipient ON messages(recipient);
CREATE INDEX IF NOT EXISTS idx_msg_thread    ON messages(thread_id);
CREATE INDEX IF NOT EXISTS idx_links_child   ON links(child_id);
CREATE INDEX IF NOT EXISTS idx_links_parent  ON links(parent_id);
"""


class BusError(Exception):
    """Caller-facing error (bad name, name collision, unknown agent)."""


def valid_name(name):
    if not NAME_RE.fullmatch(name or ""):
        raise BusError(
            f"invalid name {name!r}: use [A-Za-z0-9_-], 1-64 chars, not starting with - or _"
        )


def connect(db_path):
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    return conn


def _is_live(seen_ns, now=None):
    return (now or time.time_ns()) - seen_ns < LIVE_TTL_NS


def _row(conn, name):
    return conn.execute("SELECT * FROM agents WHERE name=?", (name,)).fetchone()


# ---- presence ----------------------------------------------------------------

def join(conn, name, tag, fresh=False, url=None):
    """Sign up under `name`. Fails if a *live* agent holds it under a different tag.
    `url` records where the agent lives (its broker base URL), for cross-machine.
    Replays only the last CATCHUP_NS of backlog; fresh=True skips it entirely."""
    valid_name(name)
    now = time.time_ns()
    cur = _row(conn, name)
    if cur and cur["tag"] != tag and _is_live(cur["seen_ns"], now):
        raise BusError(f"name {name!r} is held by a live agent (tag {cur['tag']}). pick another.")
    conn.execute(
        "INSERT INTO agents(name, tag, url, joined_ns, seen_ns) VALUES(?,?,?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET tag=excluded.tag, url=excluded.url, seen_ns=excluded.seen_ns",
        (name, tag, url, now, now),
    )
    # Mark everything outside the catch-up window already-read: default joiners see
    # only recent traffic; fresh joiners start clean. history(since=...) recalls more.
    cutoff = now if fresh else now - CATCHUP_NS
    conn.execute(
        "INSERT OR IGNORE INTO reads(message_id, agent, read_ns) "
        "SELECT id, ?, ? FROM messages WHERE sender != ? AND ts_ns < ?",
        (name, now, name, cutoff),
    )
    return name


def touch(conn, name):
    """Heartbeat. Called once per loop turn so the agent stays LIVE."""
    valid_name(name)
    n = conn.execute(
        "UPDATE agents SET seen_ns=? WHERE name=?", (time.time_ns(), name)
    ).rowcount
    if not n:
        raise BusError(f"not joined: {name}")


def leave(conn, name):
    conn.execute("DELETE FROM agents WHERE name=?", (name,))


def whoami(conn, tag):
    if not tag:
        return None
    r = conn.execute(
        "SELECT name FROM agents WHERE tag=? ORDER BY seen_ns DESC LIMIT 1", (tag,)
    ).fetchone()
    return r["name"] if r else None


def presence(conn):
    now = time.time_ns()
    return [
        {"name": r["name"], "tag": r["tag"], "url": r["url"],
         "live": _is_live(r["seen_ns"], now),
         "seen_ns": r["seen_ns"], "joined_ns": r["joined_ns"]}
        for r in conn.execute("SELECT * FROM agents ORDER BY name")
    ]


def prune(conn):
    now = time.time_ns()
    dead = [r["name"] for r in conn.execute("SELECT name, seen_ns FROM agents")
            if not _is_live(r["seen_ns"], now)]
    for name in dead:
        conn.execute("DELETE FROM agents WHERE name=?", (name,))
    return dead


# ---- messages ----------------------------------------------------------------

def _wake_targets(conn, sender, recipient):
    if recipient != BROADCAST:
        return [recipient]
    now = time.time_ns()
    return [r["name"] for r in conn.execute("SELECT name, seen_ns FROM agents")
            if r["name"] != sender and _is_live(r["seen_ns"], now)]


def send(conn, sender, recipient, body, subject="", reply_to=None):
    """Insert a message. recipient='*' broadcasts.

    reply_to threads this under a parent. Pass one id for a normal reply, or a
    list of ids to re-link/merge several branches (the message gets many parents).
    The first id is the PRIMARY parent: it sets parent_id and which thread this
    joins. Every id becomes an edge in `links`, so the full fork/merge web is kept.

    Returns {id, thread_id, parents, wake:[names]}.
    """
    valid_name(sender)
    if recipient != BROADCAST:
        valid_name(recipient)
        if not _row(conn, recipient):
            raise BusError(f"no such agent: {recipient!r} (is it joined?)")
    parents = [] if reply_to is None else ([reply_to] if isinstance(reply_to, int) else list(reply_to))
    thread_id, parent_id = None, None
    if parents:
        ph = ",".join("?" * len(parents))
        found = {r["id"]: r for r in conn.execute(
            f"SELECT id, thread_id FROM messages WHERE id IN ({ph})", parents)}
        for p in parents:
            if p not in found:
                raise BusError(f"reply_to {p}: no such message")
        parent_id = parents[0]                  # primary parent
        thread_id = found[parents[0]]["thread_id"]  # joins the primary parent's thread
    now = time.time_ns()
    cur = conn.execute(
        "INSERT INTO messages(thread_id, parent_id, sender, recipient, subject, body, ts_ns) "
        "VALUES(?,?,?,?,?,?,?)",
        (thread_id, parent_id, sender, recipient, subject, body, now),
    )
    mid = cur.lastrowid
    if thread_id is None:  # new thread roots on its own id
        conn.execute("UPDATE messages SET thread_id=? WHERE id=?", (mid, mid))
        thread_id = mid
    for p in parents:
        conn.execute("INSERT OR IGNORE INTO links(parent_id, child_id) VALUES(?,?)", (p, mid))
    return {"id": mid, "thread_id": thread_id, "parents": parents,
            "wake": _wake_targets(conn, sender, recipient)}


def _msg(r):
    return {"id": r["id"], "thread_id": r["thread_id"], "parent_id": r["parent_id"],
            "from": r["sender"], "to": r["recipient"], "subject": r["subject"],
            "body": r["body"], "ts_ns": r["ts_ns"]}


def inbox(conn, agent):
    """Unread messages addressed to `agent` (direct or broadcast), oldest first."""
    valid_name(agent)
    rows = conn.execute(
        "SELECT * FROM messages m "
        "WHERE (m.recipient=? OR m.recipient=?) AND m.sender!=? "
        "AND NOT EXISTS (SELECT 1 FROM reads r WHERE r.message_id=m.id AND r.agent=?) "
        "ORDER BY m.id",
        (agent, BROADCAST, agent, agent),
    ).fetchall()
    return [_msg(r) for r in rows]


def thread(conn, thread_id):
    """Every message in a thread, oldest first (read or not). Linear view."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE thread_id=? ORDER BY id", (thread_id,)
    ).fetchall()
    return [_msg(r) for r in rows]


def search(conn, *, keywords=None, since_ns=None, until_ns=None,
           involves=None, mine_agent=None, thread_id=None, limit=200):
    """Search the whole message log (read or not). Every filter ANDs together:
      keywords   list of words; a message matches if ANY word appears as a
                 case-insensitive substring, any position, in subject OR body
      since_ns   ts_ns >= since_ns        until_ns  ts_ns <= until_ns
      involves   agent is sender OR recipient (the full convo touching that agent)
      mine_agent agent is sender OR recipient (the caller's own slice)
      thread_id  restrict to one thread
    involves + mine_agent together => the 1:1 conversation between the two.
    Returns the most recent <=limit matches. Without keywords: oldest-first. With
    keywords: ranked best-first -- more distinct words matched wins, then more total
    hits, ties oldest-first. Each carries thread_id so the caller can expand the DAG
    with thread()/graph()/trace().
    """
    where, args = [], []
    if keywords:
        ors = " OR ".join(["(subject LIKE ? OR body LIKE ?)"] * len(keywords))
        where.append(f"({ors})")
        for k in keywords:
            args += [f"%{k}%", f"%{k}%"]
    if since_ns is not None:
        where.append("ts_ns >= ?")
        args.append(since_ns)
    if until_ns is not None:
        where.append("ts_ns <= ?")
        args.append(until_ns)
    if involves:
        where.append("(sender=? OR recipient=?)")
        args += [involves, involves]
    if mine_agent:
        where.append("(sender=? OR recipient=?)")
        args += [mine_agent, mine_agent]
    if thread_id:
        where.append("thread_id=?")
        args.append(thread_id)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    limit = max(1, min(int(limit), 1000))
    rows = conn.execute(
        f"SELECT * FROM messages{clause} ORDER BY id DESC LIMIT ?", args + [limit]
    ).fetchall()
    msgs = [_msg(r) for r in reversed(rows)]
    if keywords:
        # ponytail: rank the most recent <=limit matches, not the whole log; raise
        # limit if a search needs deeper reach. SQLite LIKE is ASCII-case-insensitive,
        # matching the Python .count() below.
        kws = [k.lower() for k in keywords]

        def rank(m):
            hay = f"{m['subject'] or ''} {m['body']}".lower()
            hits = [hay.count(k) for k in kws]
            return (sum(1 for h in hits if h), sum(hits))

        msgs.sort(key=rank, reverse=True)  # stable: ties stay oldest-first
    return msgs


def _subgraph(conn, ids):
    """{messages, edges} for a set of message ids -- edges = links with both
    endpoints inside the set. messages sorted by id; each carries its parents."""
    if not ids:
        return {"messages": [], "edges": []}
    ids = list(ids)
    ph = ",".join("?" * len(ids))
    edges = [[r["parent_id"], r["child_id"]] for r in conn.execute(
        f"SELECT parent_id, child_id FROM links "
        f"WHERE parent_id IN ({ph}) AND child_id IN ({ph}) ORDER BY child_id, parent_id",
        ids + ids)]
    parents = {}
    for p, c in edges:
        parents.setdefault(c, []).append(p)
    msgs = []
    for r in conn.execute(f"SELECT * FROM messages WHERE id IN ({ph}) ORDER BY id", ids):
        m = _msg(r)
        m["parents"] = parents.get(r["id"], [])
        msgs.append(m)
    return {"messages": msgs, "edges": edges}


def trace(conn, message_id):
    """Ancestor sub-DAG: every message that led to `message_id`, with the edges
    among them -- so you can track back exactly how we got here, including any
    forks and re-links upstream. Includes the node itself."""
    if not conn.execute("SELECT 1 FROM messages WHERE id=?", (message_id,)).fetchone():
        raise BusError(f"no such message: {message_id}")
    seen, frontier = set(), [message_id]
    while frontier:
        nid = frontier.pop()
        if nid in seen:
            continue
        seen.add(nid)
        for r in conn.execute("SELECT parent_id FROM links WHERE child_id=?", (nid,)):
            frontier.append(r["parent_id"])
    return _subgraph(conn, seen)


def graph(conn, thread_id):
    """The whole web of a thread: every message in it plus all fork/merge edges.
    Use this to render or walk the full conversation tree."""
    ids = [r["id"] for r in
           conn.execute("SELECT id FROM messages WHERE thread_id=?", (thread_id,))]
    return _subgraph(conn, set(ids))


def ack(conn, agent, message_ids):
    """Mark messages read for `agent`. Idempotent."""
    valid_name(agent)
    now = time.time_ns()
    conn.executemany(
        "INSERT OR IGNORE INTO reads(message_id, agent, read_ns) VALUES(?,?,?)",
        [(int(mid), agent, now) for mid in message_ids],
    )
    return len(message_ids)
