"""One composer submit, many unicasts, one feed row (ruling 14434).

The set of recipients a human selects exists only in the composer and used to
die at submit: N POSTs, N rows, N feed entries, and the operator's screenshot
of the duplicates is what ruled this. The composer now mints ONE id per
submit and every copy carries it; the feed collapses rows sharing
(room, sender, send_group) into one rendered row whose recipient chips each
open their OWN thread.

RECORDED AT SEND TIME, NEVER INFERRED AT RENDER -- the ruling's rejected
alternative (grouping on sender/body/ts-window) would merge two deliberate
identical unicasts and assert a set that never existed. So the gate's second
half is the one that matters: NULL groups can never merge, whatever their
text or timing.

Everything else is asserted unchanged: N rows, N distinct threads, N
receipts -- an agent still receives and acks each copy.
"""
import sys

sys.path.insert(0, "src")
from reveille import store  # noqa: E402

from conftest import sit  # noqa: E402
from ident import join  # noqa: E402


def _mk(tmp_path):
    db = str(tmp_path / "g.db")
    c = store.connect(db)
    store.migrate(c, db)
    return c


def _room_with(c, *agents):
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "r")
    store.join(c, "travis", "web:travis", room["id"], None)
    for a in agents:
        join(c, a, room["id"])
    return room, "user:" + admin["id"]


def test_one_submit_shares_the_group_and_stays_two_rows_two_threads(tmp_path):
    """Gate (a) of 14434: the copies share send_group and remain two rows
    with two thread_ids -- the grouping is render data, not a threading
    change, and each recipient's conversation stays its own."""
    c = _mk(tmp_path)
    room, uid = _room_with(c, "arch", "devops")
    g = "3f2c8d1e-one-submit"
    r1 = store.send(c, uid, "arch", "same words", room=room["id"], send_group=g)
    r2 = store.send(c, uid, "devops", "same words", room=room["id"], send_group=g)
    rows = {m["id"]: m for m in store.tail(c, rooms=[room["id"]])}
    assert rows[r1["id"]]["send_group"] == g == rows[r2["id"]]["send_group"]
    assert r1["id"] != r2["id"]
    assert rows[r1["id"]]["thread_id"] != rows[r2["id"]]["thread_id"], (
        "copies of one submit must keep their own threads")
    assert rows[r1["id"]]["to"] == "arch"
    assert rows[r2["id"]]["to"] == "devops"
    c.close()


def test_null_groups_never_merge_whatever_text_or_timing(tmp_path):
    """Gate (b), the half that matters: two deliberate identical unicasts
    sent back to back carry NO group -- nothing at the data level links
    them, so no client can honestly render them as one set."""
    c = _mk(tmp_path)
    room, uid = _room_with(c, "arch")
    r1 = store.send(c, uid, "arch", "ping", room=room["id"])
    r2 = store.send(c, uid, "arch", "ping", room=room["id"])
    rows = {m["id"]: m for m in store.tail(c, rooms=[room["id"]])}
    assert rows[r1["id"]]["send_group"] is None
    assert rows[r2["id"]]["send_group"] is None
    c.close()


def test_the_web_route_stores_the_composer_mint_bounded(broker):
    """The write path the composer uses: POST /send carries send_group, the
    row keeps it, and a client-supplied value is bounded at 64 chars -- it
    is client text headed for a column."""
    u = sit(broker, "travis")
    room = store.create_room(broker.conn, u["id"], "r")
    store.join(broker.conn, "travis", "web:travis", room["id"], None)
    tok = store.create_token(broker.conn, u["id"], "arch", agent_name="arch",
                             create=True, rooms=[room["id"]])
    store.join(broker.conn, "arch", "arch", room["id"], tok["id"])
    r = broker.post("/send?room=" + room["id"],
                    json={"to": "arch", "body": "b", "send_group": "u" * 200})
    assert r.status_code == 200, r.text
    row = [m for m in store.tail(broker.conn, rooms=[room["id"]])
           if m["id"] == r.json()["id"]][0]
    assert row["send_group"] == "u" * 64

    # No group offered -> NULL stored, exactly today's shape.
    r2 = broker.post("/send?room=" + room["id"],
                     json={"to": "arch", "body": "b2"})
    row2 = [m for m in store.tail(broker.conn, rooms=[room["id"]])
            if m["id"] == r2.json()["id"]][0]
    assert row2["send_group"] is None
