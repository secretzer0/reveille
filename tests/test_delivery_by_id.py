"""DES-011 s6.1(b): delivery by id (schema v28).

Gates 9.1 (a rename orphans nothing) and 9.3 (two owners' `architect` in one
room), the v27 -> v28 rebuild of members and reads onto the principal, and the
edges of the room-name rule: a stale holder is reaped, an alias that is itself
held is refused naming both, a rename re-runs the check where the bare name was
in force and leaves an alias alone.
"""
import asyncio
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ident import P, join  # noqa: E402
from reveille import daemon, store  # noqa: E402


def world(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    travis = store.setup_first_admin(c, "travis", "hunter2hunter2")
    bob = store.create_user(c, "bob", "hunter2hunter2")
    dee = store.create_user(c, "dee", "hunter2hunter2")
    big = store.create_room(c, travis["id"], "BigProject")
    for u in ("bob", "dee"):
        store.invite_member(c, big["id"], travis["id"], u, "web:travis")
    return c, path, travis, bob, dee, big


def _age(c, room_id, name, seconds_ago=3600):
    import time
    c.execute("UPDATE members SET seen_ns=? WHERE room_id=? AND name=?",
              (time.time_ns() - int(seconds_ago * 1e9), room_id, name))


# ---- 9.1 -----------------------------------------------------------------------

def test_a_rename_orphans_nothing(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    join(c, "alice", big["id"])
    join(c, "bob-bot", big["id"])
    bot = P(c, "bob-bot")
    aid = store.agent_of(bot)
    m = store.send(c, P(c, "alice"), "bob-bot", "before the rename", room=big["id"])
    out = store.rename_agent(c, aid, "robert")
    assert out == {"name": "robert", "rooms": {big["id"]: "robert"}}
    # the unread still arrives, addressed by id; the room now calls it robert
    inb = store.inbox(c, bot, [big["id"]])
    assert [x["id"] for x in inb] == [m["id"]]
    assert store.ack(c, bot, [m["id"]], [big["id"]])["acked"] == 1
    assert [p["name"] for p in store.presence(c, [big["id"]])] == ["alice", "robert"]
    # history still renders (the thread is intact; the stored address is what it was)
    assert [x["to"] for x in store.thread(c, m["thread_id"], [big["id"]])] == ["bob-bot"]
    # the new room-name is the address now; the old one names nobody
    m2 = store.send(c, P(c, "alice"), "robert", "after", room=big["id"])
    assert store.inbox(c, bot, [big["id"]])[0]["id"] == m2["id"]
    with pytest.raises(store.BusError, match="no such agent"):
        store.send(c, P(c, "alice"), "bob-bot", "gone", room=big["id"])
    # the rename log: old row closed, new row open
    log = c.execute("SELECT name, to_ns IS NULL AS open FROM agent_names WHERE agent_id=? "
                    "ORDER BY from_ns", (aid,)).fetchall()
    assert [(r["name"], r["open"]) for r in log] == [("bob-bot", 0), ("robert", 1)]
    # readers render the CURRENT name
    assert store.readers(c, m["id"]) == ["robert"]


def test_a_rename_refuses_a_name_the_owner_holds_live_and_a_no_op_is_a_no_op(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    join(c, "one", big["id"])
    join(c, "two", big["id"])
    with pytest.raises(store.BusError, match="already have a live agent named 'two'"):
        store.rename_agent(c, store.agent_of(P(c, "one")), "two")
    assert store.rename_agent(c, store.agent_of(P(c, "one")), "one") == {"name": "one", "rooms": {}}
    with pytest.raises(store.BusError):
        store.rename_agent(c, store.agent_of(P(c, "one")), "Not Valid!")


# ---- 9.3 -----------------------------------------------------------------------

def test_two_owners_architect_both_join_one_room(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    home = store.create_room(c, bob["id"], "BobsRoom")
    assert join(c, "architect", big["id"], owner_id=travis["id"]) == "architect"
    # bob's is aliased here, bare in his own room; join() says which
    assert join(c, "architect", big["id"], owner_id=bob["id"]) == "bob-architect"
    assert join(c, "architect", home["id"], owner_id=bob["id"]) == "architect"
    tv = store.agent_principal(c.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name='architect'", (travis["id"],)).fetchone()[0])
    bb = store.agent_principal(c.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name='architect'", (bob["id"],)).fetchone()[0])
    pres = {p["name"]: p["principal"] for p in store.presence(c, [big["id"]])}
    assert pres == {"architect": tv, "bob-architect": bb}
    # each room-name reaches only its holder
    join(c, "human-ish", big["id"], owner_id=travis["id"])
    sender = P(c, "human-ish")
    a = store.send(c, sender, "architect", "for travis's", room=big["id"])
    b = store.send(c, sender, "bob-architect", "for bob's", room=big["id"])
    assert [x["id"] for x in store.inbox(c, tv, [big["id"]])] == [a["id"]]
    assert [x["id"] for x in store.inbox(c, bb, [big["id"], home["id"]])] == [b["id"]]
    assert c.execute("SELECT recipient_agent_id FROM messages WHERE id=?",
                     (b["id"],)).fetchone()[0] == store.agent_of(bb)
    # bob's writes under its room-name here, and under its own name at home
    x = store.send(c, bb, "*", "hello", room=big["id"])
    y = store.send(c, bb, "*", "hello", room=home["id"])
    assert (x["sender"], y["sender"]) == ("bob-architect", "architect")
    # the alias survives the incumbent leaving, and a re-join keeps it
    store.leave(c, tv, [big["id"]])
    assert join(c, "architect", big["id"], owner_id=bob["id"]) == "bob-architect"
    assert store.room_name(c, big["id"], bb) == "bob-architect"
    # a stale holder (heartbeat gone) is reaped and the bare name reclaimed
    _age(c, big["id"], "bob-architect")
    assert join(c, "architect", big["id"], owner_id=dee["id"]) == "architect"


def test_an_alias_that_is_itself_held_is_refused_naming_both(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    join(c, "architect", big["id"], owner_id=travis["id"])
    join(c, "dee-architect", big["id"], owner_id=travis["id"], tag="TAG_x")
    with pytest.raises(store.BusError) as e:
        join(c, "architect", big["id"], owner_id=dee["id"])
    assert "'architect'" in str(e.value) and "'dee-architect'" in str(e.value) \
        and "TAG_x" in str(e.value)


def test_a_rename_re_runs_the_check_and_leaves_an_alias_alone(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    home = store.create_room(c, bob["id"], "BobsRoom")
    join(c, "architect", big["id"], owner_id=travis["id"])
    join(c, "architect", big["id"], owner_id=bob["id"])        # bob-architect here
    join(c, "architect", home["id"], owner_id=bob["id"])       # architect at home
    join(c, "scout", big["id"], owner_id=travis["id"])         # holds the name bob wants
    bb = store.agent_principal(c.execute(
        "SELECT id FROM agents WHERE owner_id=? AND name='architect'", (bob["id"],)).fetchone()[0])
    out = store.rename_agent(c, store.agent_of(bb), "scout")
    # the alias in BigProject is untouched; at home nobody holds `scout` -> bare
    assert out["rooms"] == {home["id"]: "scout"}
    assert store.room_name(c, big["id"], bb) == "bob-architect"
    # and a newcomer of that name at home is the one aliased now
    store.invite_member(c, home["id"], bob["id"], "travis", "web:bob")
    assert join(c, "scout", home["id"], owner_id=travis["id"], tag="TAG_t") == "travis-scout"


def test_join_needs_an_identity(tmp_path):
    c, path, travis, bob, dee, big = world(tmp_path)
    loose = store.create_token(c, travis["id"], "loose", rooms=[big["id"]])
    with pytest.raises(store.BusError, match="join needs an identity"):
        store.join(c, "nobody", "nobody", big["id"], loose["id"])
    with pytest.raises(store.BusError, match="join needs an identity"):
        store.join(c, "nobody", "nobody", big["id"])
    assert store.join(c, "travis", "web:travis", big["id"]) == "travis"   # a person
    assert store.presence(c, [big["id"]])[0]["principal"] == f"user:{travis['id']}"


# ---- the rename route ------------------------------------------------------------

def _req(method, path, body, aid):
    scope = {"type": "http", "method": method, "path": path, "headers": [], "query_string": b"",
             "path_params": {"aid": aid}}
    data = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": data, "more_body": False}
    return Request(scope, receive)


def test_the_rename_route_is_owner_or_admin(tmp_path, monkeypatch):
    c, path, travis, bob, dee, big = world(tmp_path)
    join(c, "worker", big["id"], owner_id=bob["id"])
    aid = store.agent_of(P(c, "worker"))
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_push_presence", lambda room: None)
    who = {}
    monkeypatch.setattr(daemon, "_user_principal", lambda request: who["p"])
    who["p"] = daemon.Principal(kind="user", name="dee", user_id=dee["id"], rooms={})
    r = asyncio.run(daemon.rename_agent_http(_req("PATCH", f"/identities/{aid}", {"name": "x"}, aid)))
    assert r.status_code == 403
    who["p"] = daemon.Principal(kind="user", name="bob", user_id=bob["id"], rooms={})
    r = asyncio.run(daemon.rename_agent_http(_req("PATCH", f"/identities/{aid}", {"name": "drone"}, aid)))
    assert r.status_code == 200 and json.loads(r.body) == {"name": "drone", "rooms": {big["id"]: "drone"}}
    who["p"] = daemon.Principal(kind="user", name="travis", user_id=travis["id"], rooms={},
                                is_admin=True)
    r = asyncio.run(daemon.rename_agent_http(_req("PATCH", f"/identities/{aid}", {"name": "bee"}, aid)))
    assert r.status_code == 200
    r = asyncio.run(daemon.rename_agent_http(_req("PATCH", "/identities/nope", {"name": "bee"}, "nope")))
    assert r.status_code == 404


# ---- v27 -> v28 -----------------------------------------------------------------

def test_the_rebuild_keys_members_and_reads_on_the_principal(tmp_path, capsys):
    c, path, travis, bob, dee, big = world(tmp_path)
    rid = big["id"]
    tok = store.create_token(c, travis["id"], "b", agent_name="keyed", rooms=[rid], create=True)
    c.execute("INSERT INTO agents(id, owner_id, name, created_ns) VALUES('old-1','%s','oldie',1)"
              % travis["id"])
    c.execute("INSERT INTO agents(id, owner_id, name, created_ns, retired_ns) "
              "VALUES('h-1','%s','hist',1,50)" % travis["id"])
    c.execute("INSERT INTO agents(id, owner_id, name, created_ns) VALUES('h-2','%s','hist',60)"
              % travis["id"])
    mid = c.execute("INSERT INTO messages(sender, recipient, subject, body, room, ts_ns) "
                    "VALUES('x','*','s','b',?,1)", (rid,)).lastrowid
    # The v27 shape of both tables, with every kind of row the live db has.
    c.executescript(f"""
        DROP TABLE reads; DROP TABLE members;
        CREATE TABLE members (room_id TEXT NOT NULL, name TEXT NOT NULL, agent_id TEXT, tag TEXT,
            url TEXT, token_id TEXT, joined_ns INTEGER NOT NULL, seen_ns INTEGER NOT NULL,
            left_ns INTEGER, PRIMARY KEY (room_id, name));
        CREATE TABLE reads (message_id INTEGER NOT NULL, agent TEXT NOT NULL, agent_id TEXT,
            read_ns INTEGER NOT NULL, PRIMARY KEY (message_id, agent));
        INSERT INTO members VALUES ('{rid}','oldie','old-1','oldie',NULL,NULL,1,10,NULL);
        INSERT INTO members VALUES ('{rid}','keyed',NULL,'keyed',NULL,'{tok["id"]}',1,20,NULL);
        INSERT INTO members VALUES ('{rid}','travis',NULL,'web:travis',NULL,NULL,1,30,NULL);
        INSERT INTO members VALUES ('{rid}','hist',NULL,'hist',NULL,NULL,1,40,NULL);
        INSERT INTO members VALUES ('{rid}','ghost',NULL,'ghost',NULL,NULL,1,50,NULL);
        INSERT INTO members VALUES ('{rid}','oldie-again','old-1','oldie',NULL,NULL,1,5,NULL);
        INSERT INTO reads VALUES ({mid},'oldie','old-1',1);
        INSERT INTO reads VALUES ({mid},'travis',NULL,2);
        INSERT INTO reads VALUES ({mid},'hist',NULL,70);
        INSERT INTO reads VALUES ({mid},'ghost',NULL,3);
    """)
    c.execute("PRAGMA user_version=27")
    assert store.migrate(c, path) == store.SCHEMA_VERSION
    rows = {r["name"]: r["principal"] for r in c.execute("SELECT name, principal FROM members")}
    assert rows == {"oldie": "agent:old-1", "keyed": f"agent:{tok['agent_id']}",
                    "travis": f"user:{travis['id']}", "hist": "agent:h-1"}
    reads = {r["principal"] for r in c.execute("SELECT principal FROM reads")}
    assert reads == {"agent:old-1", f"user:{travis['id']}", "agent:h-2"}
    out = capsys.readouterr().out
    assert "v28 members: 4 re-keyed on the identity, 2 dropped" in out
    assert "name 'ghost'" in out and "name 'oldie-again' (duplicate of oldie)" in out
    assert "v28 reads: 3 receipts re-keyed on the identity, 1 dropped" in out
    # rerunnable: nothing to do the second time
    c.execute("PRAGMA user_version=27")
    assert store.migrate(c, path) == store.SCHEMA_VERSION
    assert c.execute("SELECT count(*) FROM members").fetchone()[0] == 4
