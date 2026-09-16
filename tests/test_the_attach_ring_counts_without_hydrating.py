"""The attach ring counts; it does not hydrate (ruling 20404, B2).

wake_ws rang a just-attached client by calling store.inbox() and taking two
len()s of the result -- the full _SEL join, the attachments and scripts IN
queries and two os.path.exists per unread row -- for two integers, on EVERY
attach. A broker restart reconnects the whole fleet inside a 1-15 s ladder, so
that is per agent per reconnect.

It reads store.agent_activity() now (the same B1 function), and the frame's
keys are unchanged. Two properties are asserted here because both are
load-bearing and neither was covered:

  1. a direct backlog still rings, with the same counts it always carried;
  2. a BROADCAST-ONLY backlog still sends nothing -- the "DO NOT REMOVE THE
     BROADCAST FILTER" comment's property, which had no test at all. Ringing
     it would wake every agent holding any unread broadcast on every broker
     restart.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
from conftest import sit  # noqa: E402,F401
from reveille import store  # noqa: E402


def world(broker):
    """A room, a person in it, and `arch` with a bound token that reaches it."""
    u = sit(broker, "travis")
    room = store.create_room(broker.conn, u["id"], "r")
    store.join(broker.conn, "travis", "web:travis", room["id"], None)
    tok = store.create_token(broker.conn, u["id"], "arch", agent_name="arch",
                             create=True, rooms=[room["id"]])
    store.join(broker.conn, "arch", "arch", room["id"], tok["id"])
    return u, room, tok


def test_a_direct_backlog_rings_with_its_counts(broker):
    u, room, tok = world(broker)
    person = store.user_principal(u["id"])
    store.send(broker.conn, person, "arch", "for you", room=room["id"])
    store.send(broker.conn, person, store.BROADCAST, "for the room", room=room["id"])

    with broker.websocket_connect(
            "/wake?name=arch",
            headers={"Authorization": "Bearer " + tok["secret"]}) as ws:
        frame = ws.receive_json()
    assert frame["wake"] is True and frame["reason"] == "backlog"
    # The broadcast is unread too, so `unread` counts it and `direct` does not.
    # These are the numbers a woken agent applies the reply test to.
    assert frame["direct"] == 1 and frame["unread"] == 2


def test_a_broadcast_only_backlog_sends_nothing(broker):
    """The DO NOT REMOVE comment's property, asserted rather than trusted.

    "Nothing was sent" is proven by what arrives FIRST: connect with only a
    broadcast waiting, then send a real unicast and assert the first frame is
    THAT message. A backlog frame would have arrived ahead of it.
    """
    u, room, tok = world(broker)
    person = store.user_principal(u["id"])
    store.send(broker.conn, person, store.BROADCAST, "for the room", room=room["id"])

    with broker.websocket_connect(
            "/wake?name=arch",
            headers={"Authorization": "Bearer " + tok["secret"]}) as ws:
        r = broker.post("/send?room=" + room["id"],
                        json={"to": "arch", "body": "now this one", "subject": "s"})
        assert r.status_code == 200, r.text
        frame = ws.receive_json()
    assert frame["reason"] != "backlog", "a broadcast-only backlog must not ring"
    assert frame["wake"] is True and frame["from"] == "travis"
    assert frame["direct"] == 1
