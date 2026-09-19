"""0.2.260: the feed opens on a small window and walks BACKWARDS on demand.

Operator 2026-09-16: "we don't need all history at once ... we can jump in for
the last 100 msg, and as we scroll back, get paged windows."

store.tail already walked forward (since_id) or took the newest `limit`. The
third direction -- the `limit` messages immediately OLDER than an id -- is what
a feed scrolled to its top needs, and it is what lets the page stop holding
every message it has ever seen.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from reveille import store  # noqa: E402


def world(tmp_path, n=25):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    boss = store.setup_first_admin(c, "travis", "hunter2hunter2")
    bob = store.create_user(c, "bob", "hunter2hunter2")
    room = store.create_room(c, boss["id"], "A")
    store.invite_member(c, room["id"], boss["id"], "bob", "web:travis")
    store.join(c, "travis", "web:travis", room["id"], None)
    store.join(c, "bob", "web:bob", room["id"], None)
    ids = [store.send(c, store.user_principal(bob["id"]), "*", f"m{i}",
                      room=room["id"])["id"] for i in range(n)]
    return c, boss, room, ids


def test_before_id_walks_back_a_window_at_a_time(tmp_path):
    c, _b, room, ids = world(tmp_path)
    rooms = [room["id"]]

    # The opening window is the newest few, oldest-first, exactly as before.
    first = store.tail(c, limit=10, rooms=rooms)
    assert [m["id"] for m in first] == ids[-10:]

    # The page then asks for what is immediately above what it holds.
    older = store.tail(c, limit=10, rooms=rooms, before_id=first[0]["id"])
    assert [m["id"] for m in older] == ids[-20:-10]
    assert older[-1]["id"] < first[0]["id"], "before_id is exclusive"

    # And again, until history runs out -- a short page, then an empty one, is
    # how the pager learns to stop asking.
    oldest = store.tail(c, limit=10, rooms=rooms, before_id=older[0]["id"])
    assert [m["id"] for m in oldest] == ids[:5]
    assert store.tail(c, limit=10, rooms=rooms, before_id=ids[0]) == []


def test_paging_back_cannot_rewind_the_read_mark(tmp_path):
    """The invariant lives in mark_room_seen (MAX), and this is the case that
    would exercise it: a person reads to the end, then scrolls back through
    history. Unread must not come back from the dead."""
    c, boss, room, ids = world(tmp_path)
    P = store.user_principal(boss["id"])
    store.mark_room_seen(c, P, room["id"], ids[-1])
    assert store.unread_by_room(c, P, [room["id"]]) == {room["id"]: 0}

    page = store.tail(c, limit=10, rooms=[room["id"]], before_id=ids[-1])
    store.mark_room_seen(c, P, room["id"], max(m["id"] for m in page))
    assert store.unread_by_room(c, P, [room["id"]]) == {room["id"]: 0}


def test_the_route_carries_it(broker):
    """The page asks over HTTP, so the parameter is gated where the page uses it."""
    from conftest import sit
    boss = sit(broker, "travis", role="admin")
    c = broker.conn
    room = store.create_room(c, boss["id"], "A")
    store.join(c, "travis", "web:travis", room["id"], None)
    ids = [store.send(c, store.user_principal(boss["id"]), "*", f"n{i}",
                      room=room["id"])["id"] for i in range(12)]

    newest = broker.get(f"/messages?limit=5&room={room['id']}").json()["messages"]
    assert [m["id"] for m in newest] == ids[-5:]

    older = broker.get(
        f"/messages?limit=5&room={room['id']}&before_id={newest[0]['id']}").json()["messages"]
    assert [m["id"] for m in older] == ids[-10:-5]
    assert all(m["id"] < newest[0]["id"] for m in older)

    # An absent or empty value is not a filter: the route must read it as 0 and
    # answer the newest window, never hand a string to the store.
    assert [m["id"] for m in broker.get(
        f"/messages?limit=5&room={room['id']}&before_id=").json()["messages"]] == ids[-5:]
