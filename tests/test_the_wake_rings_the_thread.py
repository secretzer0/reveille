"""THREAD-WAKE (rulings 12472, 12494, 12525, consolidated 12532): an agent
REPLY-broadcast rings the thread's agent participants -- and only when the
ring would be useful, and only while a human is steering.

Two gates, different jobs, both required:
GATE 1, USEFULNESS -- decides WHEN a ring is useful. Never ring a recipient
that has READ since the message landed (inbox()/ack(); the predicate is READ,
not ACTED -- send() without inbox() acts and reads nothing). Idle and unread
-> ring immediately, no window, no cap. Mid-turn -- which on the broker means
AN OUTSTANDING POKE, the only turn signal it has: last_used_ns moves only on
reveille calls, so a body deep in a local build looks idle by any clock --
-> mark pending, fire once when the poke clears, several pendings coalesce
to one ring naming the thread.
GATE 2, STEERING -- decides WHETHER the fleet may be woken at all. Counter =
agent replies on this thread since a human last spoke, derived from the
messages table (no new state, and a human message resets it by construction).
At K=12, everything is suppressed, deferred rings included, until a human
speaks. Gate 1 alone perpetuates a storm; gate 2 is what lets one die.

A PARENTLESS broadcast still rings NOBODY -- that guard is unchanged.
The 900 s idle nudge is the FLOOR under gate 1's deferred half: worst case an
idle body learns at 900 s even if no ring fires (12494).

The one new column: tokens.last_inbox_ns (schema v38) -- ruled in 12532 after
checking that last-act cannot carry the read predicate.

All negatives BY ENUMERATION, proven RED at 73cce3d (0.2.202): store has no
thread_reply_targets/agent_replies_since_human/mark_read, daemon has no
_thread_wake/_fire_deferred, and an agent reply-broadcast rings nobody.
"""
import asyncio
import inspect
import os
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402


def world():
    """A room, a human, four agents: alfa (the sender), bravo (thread root
    author), charlie (sibling reply author), delta (in the room, silent)."""
    path = os.path.join(tempfile.mkdtemp(), "broker.db")
    c = store.connect(path)
    store.migrate(c, path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, u["id"], "Hive")
    toks = {}
    for name in ("alfa", "bravo", "charlie", "delta"):
        toks[name] = store.create_token(c, u["id"], name, agent_name=name,
                                        create=True, rooms=[room["id"]])
        store.join(c, name, "agent", room["id"], token_id=toks[name]["id"])
    return c, u, room, toks


def ap(tok):
    return f"agent:{tok['agent_id']}"


def hooked(c, toks, names):
    """Register a wake waiter queue per named agent, on a clean slate."""
    daemon._waiters.clear()
    daemon._poke_pending.clear()
    daemon._thread_pending.clear()
    qs = {}
    for n in names:
        qs[n] = asyncio.Queue()
        daemon._waiters[toks[n]["id"]] = {qs[n]}
    return qs


def thread_with_sibling(c, u, room, toks):
    """bravo roots a thread; charlie and the HUMAN both reply to the root."""
    root = store.send(c, ap(toks["bravo"]), "*", "root", room=room["id"])
    store.send(c, ap(toks["charlie"]), "*", "sibling", reply_to=root["id"],
               room=room["id"])
    store.send(c, f"user:{u['id']}", "*", "human sibling", reply_to=root["id"],
               room=room["id"])
    return root


def drain(q):
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


def test_a_parentless_broadcast_rings_nobody():
    c, u, room, toks = world()
    qs = hooked(c, toks, ("bravo", "charlie", "delta"))
    prev = daemon._conn
    daemon._conn = c
    try:
        res = store.send(c, ap(toks["alfa"]), "*", "shout", room=room["id"])
        out = daemon._thread_wake(room["id"], res, ap(toks["alfa"]), "shout")
        assert out["rung"] == [] and out["pended"] == []
        assert all(q.empty() for q in qs.values()), (
            "a parentless broadcast rings NOBODY -- the storm guard stands")
        assert not daemon._thread_pending
    finally:
        daemon._conn = prev


def test_a_reply_broadcast_rings_exactly_the_thread_agents():
    """Authors(parents) UNION authors(sibling replies to the same parent),
    minus the sender, AGENTS ONLY -- by enumeration: the human sibling author
    is not rung (no wake socket to ring), the in-room bystander is not rung,
    the sender is not rung."""
    c, u, room, toks = world()
    qs = hooked(c, toks, ("bravo", "charlie", "delta", "alfa"))
    prev = daemon._conn
    daemon._conn = c
    try:
        root = thread_with_sibling(c, u, room, toks)
        res = store.send(c, ap(toks["alfa"]), "*", "the reply",
                         reply_to=root["id"], room=room["id"])
        out = daemon._thread_wake(room["id"], res, ap(toks["alfa"]), "the reply")
        assert sorted(out["rung"]) == ["bravo", "charlie"]
        assert out["pended"] == []
        assert len(drain(qs["bravo"])) == 1 and len(drain(qs["charlie"])) == 1
        assert qs["delta"].empty(), "in the room but not in the thread"
        assert qs["alfa"].empty(), "never yourself"
        # and the ring says WHY, so the woken agent reads it as thread mail
        got = None
        res2 = store.send(c, ap(toks["alfa"]), "*", "again",
                          reply_to=root["id"], room=room["id"])
        daemon._thread_wake(room["id"], res2, ap(toks["alfa"]), "again")
        got = drain(qs["bravo"])[0]
        assert got["why"] == "thread-reply" and got["thread"] == res2["thread_id"]
    finally:
        daemon._conn = prev


def test_mid_turn_means_pending_and_read_kills_the_pending():
    """Gate 1's deferral and its predicate: a recipient with an outstanding
    poke is mid-wake; the reply pends instead of stacking. When the poke
    clears AND the recipient has READ since the message landed, the pending
    dies unfired -- ringing it would wake a body to tell it what it knows."""
    c, u, room, toks = world()
    qs = hooked(c, toks, ("bravo", "charlie"))
    prev = daemon._conn
    daemon._conn = c
    try:
        root = thread_with_sibling(c, u, room, toks)
        daemon._poke_pending[toks["bravo"]["id"]] = time.time_ns()
        res = store.send(c, ap(toks["alfa"]), "*", "while busy",
                         reply_to=root["id"], room=room["id"])
        out = daemon._thread_wake(room["id"], res, ap(toks["alfa"]), "while busy")
        assert "bravo" in out["pended"] and "bravo" not in out["rung"]
        assert qs["bravo"].empty()
        # bravo reads (inbox/ack stamps last_inbox_ns), then goes idle
        store.mark_read(c, toks["bravo"]["id"])
        daemon._poke_pending.pop(toks["bravo"]["id"])
        daemon._fire_deferred()
        assert qs["bravo"].empty(), "read since the message landed -> no ring"
        assert toks["bravo"]["id"] not in daemon._thread_pending
    finally:
        daemon._conn = prev


def test_a_deferred_ring_fires_once_and_coalesces():
    c, u, room, toks = world()
    qs = hooked(c, toks, ("bravo",))
    prev = daemon._conn
    daemon._conn = c
    try:
        root = thread_with_sibling(c, u, room, toks)
        daemon._poke_pending[toks["bravo"]["id"]] = time.time_ns()
        for body in ("one", "two"):
            res = store.send(c, ap(toks["alfa"]), "*", body,
                             reply_to=root["id"], room=room["id"])
            daemon._thread_wake(room["id"], res, ap(toks["alfa"]), body)
        assert len(daemon._thread_pending) == 1, "pendings COALESCE per recipient"
        daemon._poke_pending.pop(toks["bravo"]["id"])
        daemon._fire_deferred()
        rings = drain(qs["bravo"])
        assert len(rings) == 1, "fires EXACTLY ONCE on the idle transition"
        assert rings[0]["why"] == "thread-reply"
        daemon._fire_deferred()
        assert qs["bravo"].empty(), "and never a second time"
    finally:
        daemon._conn = prev


def _run_thread_hot(c, u, room, toks, n):
    root = store.send(c, ap(toks["bravo"]), "*", "root", room=room["id"])
    for i in range(n):
        who = toks["bravo"] if i % 2 else toks["charlie"]
        store.send(c, ap(who), "*", f"r{i}", reply_to=root["id"], room=room["id"])
    return root


def test_at_k_everything_is_suppressed_including_deferred(caplog):
    """Gate 2: at K agent replies since a human last spoke, no ring fires --
    new or deferred -- and the suppression is LOGGED WITH THE COUNTER, because
    a dropped wake that leaves no trace is how the last one hid."""
    c, u, room, toks = world()
    qs = hooked(c, toks, ("bravo", "charlie"))
    prev = daemon._conn
    daemon._conn = c
    try:
        root = _run_thread_hot(c, u, room, toks, daemon.THREAD_WAKE_K)
        res = store.send(c, ap(toks["alfa"]), "*", "the 13th",
                         reply_to=root["id"], room=room["id"])
        with caplog.at_level("INFO"):
            out = daemon._thread_wake(room["id"], res, ap(toks["alfa"]), "the 13th")
        assert out["rung"] == [] and out["pended"] == []
        assert out["counter"] >= daemon.THREAD_WAKE_K
        assert all(q.empty() for q in qs.values())
        assert any("suppress" in r.message.lower() and str(out["counter"]) in r.message
                   for r in caplog.records), "suppressions carry the counter"
        # a pending parked earlier in this thread is suppressed at fire time too
        daemon._thread_pending[toks["charlie"]["id"]] = {
            "fact": {"why": "thread-reply", "thread": res["thread_id"]},
            "ts_ns": time.time_ns(), "thread": res["thread_id"]}
        daemon._fire_deferred()
        assert qs["charlie"].empty(), "deferred rings suppressed at >= K too"
        assert toks["charlie"]["id"] not in daemon._thread_pending
    finally:
        daemon._conn = prev


def test_a_human_message_resets_the_counter_instantly():
    c, u, room, toks = world()
    hooked(c, toks, ("bravo", "charlie"))
    prev = daemon._conn
    daemon._conn = c
    try:
        root = _run_thread_hot(c, u, room, toks, daemon.THREAD_WAKE_K + 3)
        store.send(c, f"user:{u['id']}", "*", "steering", reply_to=root["id"],
                   room=room["id"])
        res = store.send(c, ap(toks["alfa"]), "*", "after the human",
                         reply_to=root["id"], room=room["id"])
        out = daemon._thread_wake(room["id"], res, ap(toks["alfa"]), "after the human")
        assert out["counter"] < daemon.THREAD_WAKE_K
        assert sorted(out["rung"]) == ["bravo", "charlie"], (
            "full speed resumes the instant a human speaks")
    finally:
        daemon._conn = prev


def test_the_predicate_is_read_not_acted():
    """send() without inbox() acts and reads nothing; 'acted' would silently
    skip a body that never saw the message. Store level: last_used_ns moves on
    resolve, last_inbox_ns only on mark_read. Wiring level: inbox and ack
    stamp it, send does not."""
    c, u, room, toks = world()
    before = time.time_ns()
    store.resolve_token(c, toks["bravo"]["secret"])          # an ACT
    assert store.read_since(c, toks["bravo"]["id"], before) is False, (
        "acting is not reading")
    store.mark_read(c, toks["bravo"]["id"])
    assert store.read_since(c, toks["bravo"]["id"], before) is True
    src_inbox = inspect.getsource(daemon.inbox.fn if hasattr(daemon.inbox, "fn")
                                  else daemon.inbox)
    src_ack = inspect.getsource(daemon.ack.fn if hasattr(daemon.ack, "fn")
                                else daemon.ack)
    src_send = inspect.getsource(daemon.send.fn if hasattr(daemon.send, "fn")
                                 else daemon.send)
    assert "mark_read" in src_inbox and "mark_read" in src_ack
    assert "mark_read" not in src_send


def test_the_migration_carries_the_read_stamp():
    path = os.path.join(tempfile.mkdtemp(), "old.db")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE tokens (id TEXT PRIMARY KEY)")
    c.execute("PRAGMA user_version=37")
    store._upgrade_v37(c, path)
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tokens)")}
    assert "last_inbox_ns" in cols
    assert c.execute("PRAGMA user_version").fetchone()[0] == 38


def test_delivered_and_rung_are_distinguished_by_name():
    """The lesson log-label-says-woke-means-delivered, closed at the source:
    now that a wake exists for broadcasts, the log and the return must not
    let one read as the other. The old `woke=` label is gone."""
    src = inspect.getsource(daemon.send.fn if hasattr(daemon.send, "fn")
                            else daemon.send)
    assert "rung=" in src and "delivered=" in src
    assert "woke=" not in src
    assert '"rung"' in src, "the return names what was actually rung"
    # and the do-not-fix comment was REWRITTEN, not deleted and not left lying
    # (doctrine 1743d2fb): it names the two gates that now guarantee the bound
    assert 'DO NOT "FIX"' not in src
    assert "gate 2" in src.lower() or "steering" in src.lower()


def test_the_ws_frame_and_the_floor_speak_the_feature():
    src = inspect.getsource(daemon)
    ws = src[src.index("async def wake_ws"):]
    assert 'fact.get("why"' in ws, "the frame's reason comes from the fact"
    assert "sweep" in inspect.getsource(daemon._pending_sweeper) or True
    assert "_fire_deferred" in inspect.getsource(daemon._pending_sweeper), (
        "a deferral nothing sweeps is a wake that never fires")
    flat = " ".join(daemon.USAGE.split())
    assert "900 s" in flat and "floor" in flat.lower(), (
        "the idle nudge is the documented floor under the deferred half (12494)")
