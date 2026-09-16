"""agent_activity counts; it does not hydrate (ruling 20404, B1).

`GET /agent/activity` used to answer `len(inbox(...))`, which built every
unread row -- two JOINs, the attachments and scripts IN queries, and two
os.path.exists per row -- to produce one integer. waked's mail probe asks it
every --mail-probe seconds per agent, ~15x more often than the idle nudge it
replaces, so the hydration had to go.

That leaves ONE NUMBER COMPUTED IN TWO PLACES, which is lesson 6e493fe8's
exact shape: the gate asserts the count and the hydrated list AGREE, never
that each looks right on its own. `direct` is held against the expression the
wake frame uses at daemon.py:3731, character for character, for the same
reason -- a frame saying direct:0 is what ends a woken agent's turn before it
reads anything.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from ident import P, join  # noqa: E402
from reveille import store  # noqa: E402


def frame_direct(msgs):
    """daemon.py:3731's own expression. Copied deliberately: this gate exists
    to catch the two drifting apart, so it must ask the question the frame
    asks, not a tidier version of it."""
    return sum(1 for m in msgs if m["to"] != store.BROADCAST)


def agrees(conn, principal, rooms):
    """The whole invariant: the counted answer equals the hydrated one."""
    got = store.agent_activity(conn, principal, rooms)
    mail = store.inbox(conn, principal, rooms)
    assert got["unread"] == len(mail), f"{got['unread']} counted, {len(mail)} hydrated"
    assert got["direct"] == frame_direct(mail), (
        f"{got['direct']} counted, {frame_direct(mail)} in the frame")
    assert got["newest_id"] == max((m["id"] for m in mail), default=0)
    return got


def world(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    travis = store.setup_first_admin(c, "travis", "hunter2hunter2")
    big = store.create_room(c, travis["id"], "Big")
    far = store.create_room(c, travis["id"], "Far")
    for room in (big, far):
        join(c, "me", room["id"])
        join(c, "peer", room["id"])
    return c, travis, big, far


def test_the_count_agrees_with_the_inbox_it_replaced(tmp_path):
    c, _travis, big, far = world(tmp_path)
    me, peer = P(c, "me"), P(c, "peer")
    rooms = [big["id"]]

    # Nothing sent yet: an empty inbox is zeros, not a missing key.
    assert agrees(c, me, rooms) == {"last_send_ns": 0, "unread": 0, "direct": 0,
                                    "newest_id": 0}

    unicast = store.send(c, peer, "me", "for you", room=big["id"])["id"]
    bcast = store.send(c, peer, store.BROADCAST, "for the room", room=big["id"])["id"]
    # MINE, AND A BROADCAST: this is the row that exercises the sender filter.
    # A unicast I send is already excluded by the recipient clause, so it
    # proves nothing -- only my own BROADCAST matches `recipient='*'` and has
    # to be dropped for being mine. (Written after manufacturing the filter's
    # removal and watching this gate stay green without it.)
    store.send(c, me, store.BROADCAST, "from me, to the room", room=big["id"])
    store.send(c, me, "peer", "from me", room=big["id"])
    # READ: excluded by the NOT EXISTS, and it is the NEWEST, so a count that
    # forgot the receipts would also report the wrong newest_id.
    read = store.send(c, peer, "me", "already seen", room=big["id"])["id"]
    store.ack(c, me, [read], rooms)
    # ANOTHER ROOM: excluded while `rooms` names only Big.
    far_one = store.send(c, peer, "me", "elsewhere", room=far["id"])["id"]

    got = agrees(c, me, rooms)
    assert got["unread"] == 2 and got["direct"] == 1
    assert got["newest_id"] == bcast, "the read message is newer and must not count"
    assert got["last_send_ns"] > 0, "me sent one, so the roll decision can see it"

    # Widen the rooms and the far message joins -- same predicate, more rooms.
    both = agrees(c, me, [big["id"], far["id"]])
    assert both["unread"] == 3 and both["direct"] == 2
    assert both["newest_id"] == far_one
    assert unicast  # bound above for the reader's benefit; the ids are the story


def test_an_unbound_principal_sees_the_broadcasts(tmp_path):
    """11252: reads answer, and nothing is addressed to nobody. inbox()'s
    unbound branch has no reads filter and no sender filter -- deliberately --
    so the count must not grow one either."""
    c, travis, big, _far = world(tmp_path)
    person = store.user_principal(travis["id"])
    rooms = [big["id"]]
    peer = P(c, "peer")

    store.send(c, peer, "me", "not for the room", room=big["id"])
    bcast = store.send(c, peer, store.BROADCAST, "for the room", room=big["id"])["id"]

    got = agrees(c, person, rooms)
    assert got["unread"] == 1 and got["direct"] == 0
    assert got["newest_id"] == bcast


def test_no_rooms_is_zero_not_a_scan(tmp_path):
    """A token that reaches no room has no mail by construction. inbox()
    returns [] before touching the db; the count returns the same shape."""
    c, _travis, big, _far = world(tmp_path)
    store.send(c, P(c, "peer"), "me", "for you", room=big["id"])
    assert agrees(c, P(c, "me"), []) == {"last_send_ns": 0, "unread": 0,
                                         "direct": 0, "newest_id": 0}
