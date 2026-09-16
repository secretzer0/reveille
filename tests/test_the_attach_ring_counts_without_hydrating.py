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
from reveille import daemon, store  # noqa: E402


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
    mid = store.send(broker.conn, person, "arch", "for you", room=room["id"])["id"]
    bid = store.send(broker.conn, person, store.BROADCAST, "for the room",
                     room=room["id"])["id"]

    with broker.websocket_connect(
            "/wake?name=arch",
            headers={"Authorization": "Bearer " + tok["secret"]}) as ws:
        frame = ws.receive_json()
    assert frame["wake"] is True and frame["reason"] == "backlog"
    # The broadcast is unread too, so `unread` counts it and `direct` does not.
    # These are the numbers a woken agent applies the reply test to.
    assert frame["direct"] == 1 and frame["unread"] == 2
    # F8: `id` is the newest unread fact -- the broadcast here, since it landed
    # last. It is the SAME key the message frame uses, so waked's high-water
    # line needs no special case, and without it a backlog ring plus 60 s with
    # no ack double-rang through the mail probe.
    assert frame["id"] == max(mid, bid) == bid
    assert frame["version"].startswith(daemon.__version__)


def test_a_broadcast_only_backlog_says_hello_and_does_not_ring(broker):
    """The DO NOT REMOVE comment's property, asserted rather than trusted --
    and restated for F8, which is why this test changed name.

    Before F8 the property was "sends nothing". Now every attach sends ONE
    frame, so the property is "does not RING": `wake` false, reason `hello`.
    A ring is what spends a model turn; a frame the daemon reads and does not
    spool costs nobody anything. Ringing a broadcast backlog would wake every
    agent holding any unread broadcast on every broker restart.
    """
    u, room, tok = world(broker)
    person = store.user_principal(u["id"])
    bid = store.send(broker.conn, person, store.BROADCAST, "for the room",
                     room=room["id"])["id"]

    with broker.websocket_connect(
            "/wake?name=arch",
            headers={"Authorization": "Bearer " + tok["secret"]}) as ws:
        hello = ws.receive_json()
        assert hello["wake"] is False, "a broadcast-only backlog must not ring"
        assert hello["reason"] == "hello"
        assert hello["unread"] == 1 and hello["direct"] == 0
        assert hello["id"] == bid, "the hello still carries the high-water mark"
        # A real unicast rings normally, right behind it.
        r = broker.post("/send?room=" + room["id"],
                        json={"to": "arch", "body": "now this one", "subject": "s"})
        assert r.status_code == 200, r.text
        frame = ws.receive_json()
    assert frame["wake"] is True and frame["from"] == "travis"
    assert frame["direct"] == 1


def test_an_empty_inbox_still_gets_a_hello_carrying_the_version(broker):
    """THE FRAME IS UNCONDITIONAL, and that is the whole of F8: an attach with
    nothing waiting used to be SILENT. Three things depended on a frame that
    might never come -- the daemon's only notice of a new broker version, the
    wedge detector's "the broker SPOKE", and the shared high-water mark."""
    _u, _room, tok = world(broker)
    with broker.websocket_connect(
            "/wake?name=arch",
            headers={"Authorization": "Bearer " + tok["secret"]}) as ws:
        hello = ws.receive_json()
    assert hello == {"wake": False, "reason": "hello", "unread": 0,
                     "direct": 0, "id": 0,
                     "version": daemon.wake_version_line()}
