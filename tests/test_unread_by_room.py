"""EPIC-001 #6: per-room unread counts (the sheet's promise, DES-016 s2).

A PERSON reads a room, so one high-water mark per (person, room) is the whole
story -- not a receipt per message, which is the agents' question. Reading the
room IS the mark: the backlog fetch a page makes for the room it is showing.
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from starlette.requests import Request  # noqa: E402

from reveille import daemon, store  # noqa: E402


def world(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    travis = store.setup_first_admin(c, "travis", "hunter2hunter2")
    bob = store.create_user(c, "bob", "hunter2hunter2")
    a = store.create_room(c, travis["id"], "A")
    b = store.create_room(c, travis["id"], "B")
    for r in (a, b):
        store.invite_member(c, r["id"], travis["id"], "bob", "web:travis")
        store.join(c, "travis", "web:travis", r["id"], None)
        store.join(c, "bob", "web:bob", r["id"], None)
    return c, travis, bob, a, b


def _say(c, user, room, body="hi"):
    return store.send(c, store.user_principal(user["id"]), "*", body, room=room["id"])


def test_a_person_counts_what_arrived_after_they_last_read(tmp_path):
    c, travis, bob, a, b = world(tmp_path)
    P = store.user_principal(travis["id"])
    rooms = [a["id"], b["id"]]
    assert store.unread_by_room(c, P, rooms) == {a["id"]: 0, b["id"]: 0}
    m1 = _say(c, bob, a); _say(c, bob, a); _say(c, bob, b)  # noqa: E702
    assert store.unread_by_room(c, P, rooms) == {a["id"]: 2, b["id"]: 1}
    # reading A up to its first message leaves one
    store.mark_room_seen(c, P, a["id"], m1["id"])
    assert store.unread_by_room(c, P, rooms)[a["id"]] == 1
    # your OWN words are never unread to you
    _say(c, travis, a, "mine")
    assert store.unread_by_room(c, P, rooms)[a["id"]] == 1
    # bob has his own view: he has read nothing, and travis's line counts for him
    Pb = store.user_principal(bob["id"])
    assert store.unread_by_room(c, Pb, rooms) == {a["id"]: 1, b["id"]: 0}, \
        "his own three do not count; travis's one does"


def test_the_mark_only_moves_forward(tmp_path):
    c, travis, bob, a, b = world(tmp_path)
    P = store.user_principal(travis["id"])
    m1 = _say(c, bob, a)
    m2 = _say(c, bob, a)
    store.mark_room_seen(c, P, a["id"], m2["id"])
    store.mark_room_seen(c, P, a["id"], m1["id"])     # a slow tab answering late
    assert store.unread_by_room(c, P, [a["id"]]) == {a["id"]: 0}, \
        "a late answer must not resurrect counts somebody cleared"


def test_never_opened_means_everything_is_waiting(tmp_path):
    c, travis, bob, a, b = world(tmp_path)
    for _ in range(3):
        _say(c, bob, a)
    dee = store.create_user(c, "dee", "hunter2hunter2")
    store.invite_member(c, a["id"], travis["id"], "dee", "web:travis")
    assert store.unread_by_room(c, store.user_principal(dee["id"]), [a["id"]]) == {a["id"]: 3}


def _req(path, params, user_id, name="travis", rooms=None):
    q = "&".join(f"{k}={v}" for k, v in params.items()).encode()
    scope = {"type": "http", "method": "GET", "path": path, "headers": [],
             "query_string": q, "path_params": {}}

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    r = Request(scope, receive)
    r.scope["_p"] = daemon.Principal(kind="user", name=name, user_id=user_id,
                                     is_admin=True, rooms=rooms or {})
    return r


def test_reading_the_room_is_the_mark_and_me_carries_the_counts(tmp_path, monkeypatch):
    c, travis, bob, a, b = world(tmp_path)
    rooms = {a["id"]: "A", b["id"]: "B"}
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_principal", lambda request: request.scope["_p"])
    monkeypatch.setattr(daemon, "_user_principal", lambda request: request.scope["_p"])
    _say(c, bob, a); _say(c, bob, a); _say(c, bob, b)  # noqa: E702

    me = json.loads(asyncio.run(daemon.me_http(_req("/me", {}, travis["id"], rooms=rooms))).body)
    assert me["unread"] == {a["id"]: 2, b["id"]: 1}
    # the page fetches the backlog of the room it is SHOWING -- that is the mark
    r = _req("/messages", {"room": a["id"]}, travis["id"], rooms=rooms)
    asyncio.run(daemon.messages_http(r))
    me = json.loads(asyncio.run(daemon.me_http(_req("/me", {}, travis["id"], rooms=rooms))).body)
    assert me["unread"] == {a["id"]: 0, b["id"]: 1}, "reading A cleared A, not B"
    # a fetch across ALL rooms (no room=) marks nothing: it is not reading a room
    _say(c, bob, a)
    asyncio.run(daemon.messages_http(_req("/messages", {}, travis["id"], rooms=rooms)))
    me = json.loads(asyncio.run(daemon.me_http(_req("/me", {}, travis["id"], rooms=rooms))).body)
    assert me["unread"][a["id"]] == 1


def test_an_agents_read_is_still_its_own_question(tmp_path):
    """Agents ack what was ADDRESSED to them; a person reads a room. The two
    do not share a mechanism, and a person's mark never touches reads."""
    c, travis, bob, a, b = world(tmp_path)
    before = c.execute("SELECT count(*) FROM reads").fetchone()[0]
    store.mark_room_seen(c, store.user_principal(travis["id"]), a["id"], 999)
    assert c.execute("SELECT count(*) FROM reads").fetchone()[0] == before
    assert c.execute("SELECT count(*) FROM room_seen").fetchone()[0] == 1


def test_the_page_shows_the_counts_where_the_rooms_are():
    page = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                             "index.html")).read()
    assert "const un=(me&&me.unread)||{};" in page
    assert "x.id!==room&&un[x.id]?'<span class=\"unread\">'" in page, \
        "the room you are looking at never wears a badge"
    assert "id=\"meRoomUnread\"" in page and ".unread:empty{display:none}" in page
    assert "setInterval(refreshUnread,15000);" in page and "refreshUnread()" in page
