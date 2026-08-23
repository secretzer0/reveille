"""How a person asked to be addressed (rulings 14032/14048).

secretzer0, in the message that ruled this: "quit calling me 'operator' ...
operator is a complete fail to know who it was". The broker walks the
preference ONCE (store.moniker_of) and every surface that names a human
serves the resolved string: presence rows, the brief's presence digest,
whoami's owner_moniker, GET /me. The UI slice (red-shirt's, 14048 order:
broker field FIRST) renders these fields and re-derives nothing.
"""
import sys

import pytest

sys.path.insert(0, "src")
from reveille import store  # noqa: E402

from conftest import sit  # noqa: E402
from ident import P, join  # noqa: E402


def _mk(tmp_path):
    db = str(tmp_path / "m.db")
    c = store.connect(db)
    store.migrate(c, db)
    return c


# ---- the pure walk -----------------------------------------------------------

def _row(**kw):
    base = {"name": "tmelhiser", "nickname": None, "persona": None,
            "moniker_order": None}
    base.update(kw)
    return base


def test_no_preference_resolves_to_username():
    assert store.moniker_of(_row()) == "tmelhiser"


def test_nickname_wins_by_default():
    assert store.moniker_of(_row(nickname="secretzer0")) == "secretzer0"


def test_persona_fills_when_nickname_empty():
    assert store.moniker_of(_row(persona="the-captain")) == "the-captain"


def test_order_is_the_users_not_ours():
    r = _row(nickname="secretzer0",
             moniker_order='["username", "nickname"]')
    assert store.moniker_of(r) == "tmelhiser"


def test_operator_only_when_the_user_put_it_first():
    assert store.moniker_of(_row(moniker_order='["operator"]')) == "operator"


def test_junk_order_falls_back_to_default():
    assert store.moniker_of(_row(nickname="secretzer0",
                                 moniker_order="not json")) == "secretzer0"


def test_blank_values_are_skipped_not_served():
    assert store.moniker_of(_row(nickname="   ")) == "tmelhiser"


# ---- the stored field and the surfaces ----------------------------------------

def test_set_moniker_round_trip_and_refusals(tmp_path):
    c = _mk(tmp_path)
    u = store.setup_first_admin(c, "travis", "hunter2hunter2")
    assert store.user_moniker(c, u["id"]) == "travis"
    assert store.set_moniker(c, u["id"], nickname="secretzer0") == "secretzer0"
    assert store.set_moniker(c, u["id"],
                             order=["username", "nickname"]) == "travis"
    assert store.set_moniker(c, u["id"], order=[]) == "secretzer0"  # default back
    assert store.set_moniker(c, u["id"], nickname="") == "travis"   # cleared
    with pytest.raises(store.BusError):
        store.set_moniker(c, u["id"], order=["nickname", "nickname"])
    with pytest.raises(store.BusError):
        store.set_moniker(c, u["id"], order=["boss"])
    with pytest.raises(store.BusError):
        store.set_moniker(c, u["id"], nickname="x" * 65)
    with pytest.raises(store.BusError):
        store.set_moniker(c, "no-such-id", nickname="x")
    f = store.moniker_fields(c, u["id"])
    assert f["moniker"] == "travis" and f["nickname"] == ""
    c.close()


def test_presence_carries_the_resolved_address(tmp_path):
    """14048: walked ONCE broker-side. A human row carries their own moniker;
    an agent row carries its OWNER's -- both answer 'what do I call the
    person here'."""
    c = _mk(tmp_path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "r")
    store.join(c, "travis", "web:travis", room["id"], None)
    join(c, "arch", room["id"])
    store.set_moniker(c, admin["id"], nickname="secretzer0")
    rows = {r["name"]: r for r in store.presence(c, [room["id"]])}
    assert rows["travis"]["moniker"] == "secretzer0"
    assert rows["arch"]["moniker"] == "secretzer0"      # the owner's address
    assert store.agent_owner_moniker(
        c, store.agent_of(P(c, "arch"))) == ("travis", "secretzer0")
    c.close()


def test_me_serves_and_patches_the_moniker(broker):
    """GET /me carries the raw fields + resolved string; PATCH /me is the one
    write door the UI slice builds against."""
    sit(broker, "travis")
    r = broker.get("/me").json()
    assert r["moniker"] == "travis"
    assert r["moniker_order"] == list(store.MONIKER_KINDS)
    r = broker.patch("/me", json={"nickname": "secretzer0"})
    assert r.status_code == 200 and r.json()["moniker"] == "secretzer0"
    assert broker.get("/me").json()["moniker"] == "secretzer0"
    r = broker.patch("/me", json={"moniker_order": ["boss"]})
    assert r.status_code >= 400


# ---- delivery carries the address (ruling 14056) -------------------------------

def test_delivery_carries_the_senders_address(tmp_path):
    """A MESSAGE CARRIES ITS SENDER'S RESOLVED MONIKER AT DELIVERY. `from`
    stays the identity (ids exact is bus doctrine); `from_moniker` is what
    the reading agent ADDRESSES. Walked broker-side by the same moniker_of
    -- the lookup 14048 forbade relocating onto the agent."""
    c = _mk(tmp_path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "r")
    store.join(c, "travis", "web:travis", room["id"], None)
    join(c, "arch", room["id"])
    store.set_moniker(c, admin["id"], nickname="secretzer0")
    res = store.send(c, store.user_principal(admin["id"]), "arch",
                     "fix it", room=room["id"])
    assert res["sender_moniker"] == "secretzer0"
    got = store.inbox(c, P(c, "arch"), [room["id"]])
    assert got[-1]["from"] == "travis"
    assert got[-1]["from_moniker"] == "secretzer0"
    # history/search rows ride the same seam
    hit = store.search(c, keywords=["fix"], rooms=[room["id"]])[-1]
    assert hit["from_moniker"] == "secretzer0"
    # thread/trace (the _subgraph render) too
    g = store.graph(c, res["thread_id"], [room["id"]])
    assert g["messages"][0]["from_moniker"] == "secretzer0"
    c.close()


def test_agent_sent_messages_carry_nothing_new(tmp_path):
    """An agent-sent message has no human sender: no from_moniker key at all,
    and send() reports sender_moniker None -- ruled, not an omission."""
    c = _mk(tmp_path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "r")
    store.join(c, "travis", "web:travis", room["id"], None)
    join(c, "arch", room["id"])
    res = store.send(c, P(c, "arch"), "*", "status: green", room=room["id"])
    assert res["sender_moniker"] is None
    rows = [m for m in store.tail(c, rooms=[room["id"]]) if m["id"] == res["id"]]
    assert "from_moniker" not in rows[0]
    c.close()
