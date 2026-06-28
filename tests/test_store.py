#!/usr/bin/env python3
"""Assert-based checks for the SQLite broker core. Run: uv run python tests/test_store.py
(or uv run pytest). No fixtures -- each test makes its own in-temp db."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from agentbus import store  # noqa: E402


def db():
    return store.connect(os.path.join(tempfile.mkdtemp(), "broker.db"))


def _age(conn, name, seconds_ago):
    past = time.time_ns() - int(seconds_ago * 1e9)
    conn.execute("UPDATE agents SET seen_ns=? WHERE name=?", (past, name))


def test_join_presence_and_whoami():
    c = db()
    store.join(c, "alice", "TAG_a")
    assert store.whoami(c, "TAG_a") == "alice"
    p = store.presence(c)
    assert len(p) == 1 and p[0]["name"] == "alice" and p[0]["live"]


def test_invalid_name_rejected():
    c = db()
    try:
        store.join(c, "has space", "T")
        assert False, "should have raised"
    except store.BusError:
        pass


def test_unicast_inbox_then_ack():
    c = db()
    store.join(c, "alice", "TAG_a")
    store.join(c, "bob", "TAG_b")
    store.send(c, "bob", "alice", "yo", subject="hi")
    box = store.inbox(c, "alice")
    assert len(box) == 1 and box[0]["from"] == "bob" and box[0]["body"] == "yo"
    store.ack(c, "alice", [box[0]["id"]])
    assert store.inbox(c, "alice") == []


def test_broadcast_seen_by_others_not_sender():
    c = db()
    store.join(c, "alice", "TAG_a")
    store.join(c, "bob", "TAG_b")
    store.send(c, "alice", store.BROADCAST, "hello all")
    assert len(store.inbox(c, "bob")) == 1
    assert store.inbox(c, "alice") == []  # sender never gets own broadcast
    bid = store.inbox(c, "bob")[0]["id"]
    store.ack(c, "bob", [bid])
    assert store.inbox(c, "bob") == []


def test_replay_on_join_then_fresh_skips():
    c = db()
    store.join(c, "alice", "TAG_a")
    store.send(c, "alice", store.BROADCAST, "old news")
    # a normal late joiner replays the unread backlog...
    store.join(c, "late", "TAG_l")
    assert len(store.inbox(c, "late")) == 1
    # ...a --fresh joiner starts clean
    store.join(c, "fresh", "TAG_f", fresh=True)
    assert store.inbox(c, "fresh") == []


def test_threading_reply_inherits_thread_and_parent():
    c = db()
    store.join(c, "alice", "TAG_a")
    store.join(c, "bob", "TAG_b")
    root = store.send(c, "alice", "bob", "question?")
    reply = store.send(c, "bob", "alice", "answer.", reply_to=root["id"])
    assert reply["thread_id"] == root["thread_id"], "reply must inherit the root thread"
    t = store.thread(c, root["thread_id"])
    assert [m["body"] for m in t] == ["question?", "answer."]
    assert t[1]["parent_id"] == root["id"]


def test_deep_thread_keeps_one_thread_id():
    c = db()
    store.join(c, "a", "TA")
    store.join(c, "b", "TB")
    m1 = store.send(c, "a", "b", "1")
    m2 = store.send(c, "b", "a", "2", reply_to=m1["id"])
    m3 = store.send(c, "a", "b", "3", reply_to=m2["id"])  # reply to a reply
    assert m3["thread_id"] == m1["thread_id"]
    assert len(store.thread(c, m1["thread_id"])) == 3


def test_fork_one_parent_many_children():
    c = db()
    store.join(c, "a", "TA"); store.join(c, "b", "TB")
    root = store.send(c, "a", "b", "topic")
    b1 = store.send(c, "b", "a", "branch-1", reply_to=root["id"])
    b2 = store.send(c, "b", "a", "branch-2", reply_to=root["id"])
    g = store.graph(c, root["thread_id"])
    assert {tuple(e) for e in g["edges"]} == {(root["id"], b1["id"]), (root["id"], b2["id"])}
    # both branches name the same parent
    by_id = {m["id"]: m for m in g["messages"]}
    assert by_id[b1["id"]]["parents"] == [root["id"]]
    assert by_id[b2["id"]]["parents"] == [root["id"]]


def test_relink_merge_many_parents():
    c = db()
    store.join(c, "a", "TA"); store.join(c, "b", "TB")
    root = store.send(c, "a", "b", "topic")
    b1 = store.send(c, "b", "a", "branch-1", reply_to=root["id"])
    b2 = store.send(c, "b", "a", "branch-2", reply_to=root["id"])
    merge = store.send(c, "a", "b", "merge", reply_to=[b1["id"], b2["id"]])
    assert merge["parents"] == [b1["id"], b2["id"]]
    assert merge["thread_id"] == root["thread_id"]  # joins the primary parent's thread
    g = store.graph(c, root["thread_id"])
    by_id = {m["id"]: m for m in g["messages"]}
    assert sorted(by_id[merge["id"]]["parents"]) == sorted([b1["id"], b2["id"]])


def test_trace_back_through_fork_and_merge():
    c = db()
    store.join(c, "a", "TA"); store.join(c, "b", "TB")
    root = store.send(c, "a", "b", "topic")
    b1 = store.send(c, "b", "a", "branch-1", reply_to=root["id"])
    b2 = store.send(c, "b", "a", "branch-2", reply_to=root["id"])
    merge = store.send(c, "a", "b", "merge", reply_to=[b1["id"], b2["id"]])
    tr = store.trace(c, merge["id"])
    ids = {m["id"] for m in tr["messages"]}
    assert ids == {root["id"], b1["id"], b2["id"], merge["id"]}, "trace must reach every ancestor"
    # the merge's two incoming edges are both present in the back-trace
    assert [b1["id"], merge["id"]] in tr["edges"]
    assert [b2["id"], merge["id"]] in tr["edges"]


def test_trace_excludes_unrelated_siblings():
    # A sibling branch you did NOT descend from must not appear in your back-trace.
    c = db()
    store.join(c, "a", "TA"); store.join(c, "b", "TB")
    root = store.send(c, "a", "b", "topic")
    mine = store.send(c, "b", "a", "mine", reply_to=root["id"])
    _other = store.send(c, "b", "a", "other", reply_to=root["id"])
    ids = {m["id"] for m in store.trace(c, mine["id"])["messages"]}
    assert ids == {root["id"], mine["id"]}, "unrelated sibling leaked into trace"


def test_live_name_collision_blocks_stale_reclaims():
    c = db()
    store.join(c, "carol", "TAG_a")
    try:
        store.join(c, "carol", "TAG_other")  # live holder, different tag
        assert False, "collision should raise"
    except store.BusError:
        pass
    _age(c, "carol", 60 * 60)  # 1h > 40m TTL -> stale
    store.join(c, "carol", "TAG_other")  # reclaim ok
    assert store.whoami(c, "TAG_other") == "carol"


def test_touch_keeps_live_and_prune_drops_stale():
    c = db()
    store.join(c, "ann", "T")
    _age(c, "ann", 60 * 60)
    assert not store.presence(c)[0]["live"]
    store.touch(c, "ann")
    assert store.presence(c)[0]["live"]
    _age(c, "ann", 60 * 60)
    assert store.prune(c) == ["ann"]
    assert store.presence(c) == []


def test_send_to_unknown_agent_errors():
    c = db()
    store.join(c, "alice", "TAG_a")
    try:
        store.send(c, "alice", "ghost", "hi")
        assert False, "should raise"
    except store.BusError:
        pass


def test_same_name_rejoin_same_tag_ok():
    # A session that re-runs join (reload) keeps its name; no collision against itself.
    c = db()
    store.join(c, "alice", "TAG_a")
    store.join(c, "alice", "TAG_a")  # idempotent
    assert store.whoami(c, "TAG_a") == "alice"


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    for fn in tests:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(tests)} passed")
