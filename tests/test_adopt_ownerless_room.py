"""EPIC-001 #4 (ruling 11604 gap): an admin adopts an OWNERLESS room.

Deleting a person leaves their rooms standing -- the history is not theirs to
take -- and until someone owns one again, nobody can change its name,
retention or publicity. Adoption is the explicit act that fixes that, with an
audit row. It is never a transfer: a room WITH an owner is refused.
"""
import asyncio
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from reveille import daemon, store  # noqa: E402


def world(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    bob = store.create_user(c, "bob", "hunter2hunter2")
    room = store.create_room(c, bob["id"], "BobsRoom")
    store.join(c, "bob", "web:bob", room["id"], None)
    store.send(c, store.user_principal(bob["id"]), "*", "history", room=room["id"])
    return c, admin, bob, room


def _req(method, path_params, body, user_id, is_admin=True, name="travis"):
    scope = {"type": "http", "method": method, "path": "/x", "headers": [],
             "query_string": b"", "path_params": path_params}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    r = Request(scope, receive)
    r.scope["_p"] = daemon.Principal(kind="user", name=name, user_id=user_id,
                                     is_admin=is_admin, rooms={})
    return r


def _call(fn, req, conn, monkeypatch):
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_user_principal", lambda request: request.scope["_p"])
    r = asyncio.run(fn(req))
    return r.status_code, json.loads(r.body)


def test_a_deleted_owners_room_is_listed_with_what_is_at_stake(tmp_path, monkeypatch):
    c, admin, bob, room = world(tmp_path)
    assert store.ownerless_rooms(c) == []
    store.delete_user(c, bob["id"])
    out = store.ownerless_rooms(c)
    assert [r["name"] for r in out] == ["BobsRoom"]
    assert out[0]["messages"] == 1 and out[0]["members"] == 1
    st, body = _call(daemon.rooms_ownerless_http, _req("GET", {}, {}, admin["id"]),
                     c, monkeypatch)
    assert st == 200 and [r["name"] for r in body["rooms"]] == ["BobsRoom"]
    # not an admin: the deployment's list is not theirs to read
    st, body = _call(daemon.rooms_ownerless_http,
                     _req("GET", {}, {}, admin["id"], is_admin=False), c, monkeypatch)
    assert st == 403


def test_adopting_gives_it_an_owner_and_leaves_the_record(tmp_path, monkeypatch):
    c, admin, bob, room = world(tmp_path)
    store.delete_user(c, bob["id"])
    st, body = _call(daemon.room_owner_http,
                     _req("PATCH", {"rid": room["id"]}, {}, admin["id"]), c, monkeypatch)
    assert (st, body["owner"], body["name"]) == (200, "travis", "BobsRoom")
    assert [r["name"] for r in store.list_rooms(c, admin["id"])] == ["BobsRoom"]
    assert store.ownerless_rooms(c) == []
    audit = c.execute("SELECT action, actor, subject FROM room_audit "
                      "WHERE room_id=? ORDER BY id DESC LIMIT 1", (room["id"],)).fetchone()
    assert tuple(audit) == ("adopt", "web:travis", "travis")
    # and the owner's verbs work again -- which is the whole point
    store.rename_room(c, room["id"], admin["id"], "Adopted")
    assert store.get_room(c, room["id"])["name"] == "Adopted"
    # the messages did not move: adoption is about the room, not its history
    assert c.execute("SELECT count(*) FROM messages WHERE room=?",
                     (room["id"],)).fetchone()[0] == 1


def test_a_room_with_an_owner_is_never_taken(tmp_path, monkeypatch):
    c, admin, bob, room = world(tmp_path)
    with pytest.raises(store.BusError, match="already has an owner"):
        store.adopt_room(c, room["id"], admin["id"], "web:travis")
    st, body = _call(daemon.room_owner_http,
                     _req("PATCH", {"rid": room["id"]}, {}, admin["id"]), c, monkeypatch)
    assert st == 400 and "already has an owner" in body["error"]
    # a non-admin cannot adopt even an ownerless one
    store.delete_user(c, bob["id"])
    st, body = _call(daemon.room_owner_http,
                     _req("PATCH", {"rid": room["id"]}, {}, admin["id"], is_admin=False),
                     c, monkeypatch)
    assert st == 403
    assert store.ownerless_rooms(c)[0]["name"] == "BobsRoom", "still nobody's"


def test_a_name_the_adopter_already_owns_is_named_not_an_integrity_error(tmp_path):
    c, admin, bob, room = world(tmp_path)
    store.create_room(c, admin["id"], "BobsRoom")     # same label, different owner
    store.delete_user(c, bob["id"])
    with pytest.raises(store.BusError, match="already owns a room named"):
        store.adopt_room(c, room["id"], admin["id"], "web:travis")
    assert store.ownerless_rooms(c)[0]["name"] == "BobsRoom"


def test_adoption_can_hand_it_to_somebody_else(tmp_path):
    c, admin, bob, room = world(tmp_path)
    dee = store.create_user(c, "dee", "hunter2hunter2")
    store.delete_user(c, bob["id"])
    out = store.adopt_room(c, room["id"], dee["id"], "web:travis")
    assert out["owner"] == "dee"
    assert [r["name"] for r in store.list_rooms(c, dee["id"])] == ["BobsRoom"]
    with pytest.raises(store.NotFound):
        store.adopt_room(c, "no-such-room", dee["id"], "web:travis")
    # an unknown adopter is refused on a room that is still ownerless
    other = store.create_room(c, bob["id"], "Another")
    c.execute("UPDATE rooms SET owner_id=NULL WHERE id=?", (other["id"],))
    with pytest.raises(store.NotFound, match="no such user"):
        store.adopt_room(c, other["id"], "no-such-user", "web:travis")
    assert [r["name"] for r in store.ownerless_rooms(c)] == ["Another"]


def test_the_migration_takes_the_new_verb(tmp_path):
    db = str(tmp_path / "m.db")
    c = store.connect(db)
    store.migrate(c, db)
    assert store._version(c) == store.SCHEMA_VERSION
    store._audit_room(c, "r", "adopt", "web:travis", "travis")
    with pytest.raises(Exception):
        store._audit_room(c, "r", "seize", "web:travis", "travis")
    c.close()


def test_the_page_offers_adoption_only_to_an_admin():
    page = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                             "index.html")).read()
    assert "if(d.is_admin){" in page and "/rooms/ownerless" in page
    assert "data-adopt=" in page and "OWNERLESS ROOMS" in page
    assert "'/rooms/'+b.dataset.adopt+'/owner'" in page
