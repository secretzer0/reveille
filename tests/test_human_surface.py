"""DES-011 s6.1(c): the human surface. What people READ is the room-name and
the account behind it; what the bus KEYS and RINGS is the identity. Gate: the
same identity's message rendered in a room where an alias is in force and in
one where it is not shows the room-name in each; a ring for an aliased agent
lands on its token; presence and rings carry the owner.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ident import P, join  # noqa: E402
from reveille import daemon, store  # noqa: E402
from test_delivery_by_id import world  # noqa: E402


def _two_architects(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    home = store.create_room(c, bob["id"], "BobsRoom")
    join(c, "architect", big["id"], owner_id=travis["id"])          # architect
    join(c, "architect", big["id"], owner_id=bob["id"])             # bob-architect
    join(c, "architect", home["id"], owner_id=bob["id"])            # architect at home
    join(c, "human-ish", big["id"], owner_id=travis["id"])
    tv = store.agent_principal(c.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name='architect'", (travis["id"],)).fetchone()[0])
    bb = store.agent_principal(c.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name='architect'", (bob["id"],)).fetchone()[0])
    return c, travis, bob, big, home, tv, bb


def test_the_same_message_renders_the_room_name_with_and_without_an_alias(tmp_path):
    c, travis, bob, big, home, tv, bb = _two_architects(tmp_path)
    x = store.send(c, bb, "*", "same words", room=big["id"])
    y = store.send(c, bb, "*", "same words", room=home["id"])
    # the store's own render (what inbox/history/the feed show)
    seen = {m["room"]: m["from"] for m in store.inbox(c, tv, [big["id"]])}
    assert seen == {big["id"]: "bob-architect"}
    hist = {m["id"]: m["from"] for r in (big, home)
            for m in store.thread(c, x["id"] if r is big else y["id"], [r["id"]])}
    assert hist[x["id"]] == "bob-architect" and hist[y["id"]] == "architect"
    # and the sender's own account name rides beside it for the ring/feed
    assert x["owner"] == "bob" and y["owner"] == "bob"
    # readers render the room-name too: bob's architect read travis's message in
    # BigProject as bob-architect, its own at home as architect
    m = store.send(c, tv, "*", "from travis's", room=big["id"])
    store.ack(c, bb, [m["id"]], [big["id"], home["id"]])
    assert store.readers(c, m["id"]) == ["bob-architect"]
    n = store.send(c, P(c, "human-ish"), "*", "note", room=big["id"])
    store.ack(c, tv, [n["id"]], [big["id"]])
    assert store.readers(c, n["id"]) == ["architect"]


def test_presence_carries_the_owner_beside_the_room_name(tmp_path):
    c, travis, bob, big, home, tv, bb = _two_architects(tmp_path)
    rows = {(p["room"], p["name"]): p for p in store.presence(c, [big["id"], home["id"]])}
    assert rows[(big["id"], "architect")]["owner"] == "travis"
    assert rows[(big["id"], "bob-architect")]["owner"] == "bob"
    assert rows[(home["id"], "architect")]["owner"] == "bob"
    assert rows[(big["id"], "bob-architect")]["principal"] == bb
    # a person: owner is the person
    store.join(c, "travis", "web:travis", big["id"], None)
    rows = {p["name"]: p for p in store.presence(c, [big["id"]])}
    assert rows["travis"]["owner"] == "travis"


def test_an_aliased_agents_ring_lands_on_its_token(tmp_path):
    """The RED before (b): _wake_targets returned 'bob-architect' and the waiter
    was registered under (token, 'architect') -- no ring. Now the waiter is
    keyed on the token and the ring is by identity."""
    c, travis, bob, big, home, tv, bb = _two_architects(tmp_path)
    t_bob = c.execute(
        "SELECT t.id FROM tokens t JOIN agents a ON a.id=t.agent_id WHERE a.owner_id=? "
        "AND a.name='architect'", (bob["id"],)).fetchone()["id"]
    t_tv = c.execute(
        "SELECT t.id FROM tokens t JOIN agents a ON a.id=t.agent_id WHERE a.owner_id=? "
        "AND a.name='architect'", (travis["id"],)).fetchone()["id"]
    q_bob, q_tv = asyncio.Queue(), asyncio.Queue()
    prev = daemon._conn
    daemon._conn = c
    daemon._waiters.clear()
    daemon._waiters[t_bob] = {q_bob}
    daemon._waiters[t_tv] = {q_tv}
    try:
        res = store.send(c, P(c, "human-ish"), "bob-architect", "for bob's", subject="hi",
                         room=big["id"])
        assert res["wake"] == ["bob-architect"], "delivered_to reads the room-name"
        assert res["wake_principals"] == [bb]
        daemon._notify(big["id"], res["wake_principals"], res["id"], res["sender"],
                       "hi", owner=res["owner"])
        assert q_bob.qsize() == 1 and q_tv.qsize() == 0
        fact = q_bob.get_nowait()
        assert fact == {"id": res["id"], "from": "human-ish", "owner": "travis",
                        "subject": "hi", "room": big["id"]}
        # broadcast: every live member but the sender, by identity
        res = store.send(c, P(c, "human-ish"), "*", "all", room=big["id"])
        assert sorted(res["wake"]) == ["architect", "bob-architect"]
        daemon._notify(big["id"], res["wake_principals"], res["id"], res["sender"], "")
        assert q_bob.qsize() == 1 and q_tv.qsize() == 1
        # a person in the room is never rung here (no token)
        assert store.wake_tokens(c, big["id"], [store.user_principal(travis["id"])]) == []
    finally:
        daemon._waiters.clear()
        daemon._conn = prev


def test_reachable_and_deafness_key_on_the_token_not_the_name(tmp_path):
    c, travis, bob, big, home, tv, bb = _two_architects(tmp_path)
    t_bob = c.execute(
        "SELECT t.id FROM tokens t JOIN agents a ON a.id=t.agent_id WHERE a.owner_id=? "
        "AND a.name='architect'", (bob["id"],)).fetchone()["id"]
    daemon._waiters.clear()
    daemon._waiters[t_bob] = {asyncio.Queue()}
    try:
        rows = {p["name"]: p for p in store.presence(c, [big["id"]])}
        assert daemon._reachable(rows["bob-architect"]), "aliased row, waiter on the token"
        assert not daemon._reachable(rows["architect"])
    finally:
        daemon._waiters.clear()


def test_the_usage_text_teaches_room_names():
    assert "NAMES ARE PER ROOM" in daemon.USAGE
    assert "id/from/owner/room/subject" in daemon.USAGE
