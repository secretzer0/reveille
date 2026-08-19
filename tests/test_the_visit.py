"""DES-012 (EPIC-001 #8): a visit is a body swap onto another human's host.

The handshake, the record and the four structural gates: consent is mutual and
single-use, reach is the intersection of the two humans' rooms, one identity
never holds two live bodies, and every transition leaves a room message.
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
    """Two humans, one shared room, one room only the owner is in, and an agent
    the owner owns. bob is the OWNER; travis is the admin and the HOST."""
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    travis = store.setup_first_admin(c, "travis", "hunter2hunter2")
    bob = store.create_user(c, "bob", "hunter2hunter2")
    shared = store.create_room(c, bob["id"], "Shared")
    store.invite_member(c, shared["id"], bob["id"], "travis", "web:bob")
    private = store.create_room(c, bob["id"], "BobsOwn")
    agent = store.mint_agent(c, bob["id"], "architect")
    store.join(c, "bob", "web:bob", shared["id"], None)
    store.join(c, "travis", "web:travis", shared["id"], None)
    return c, travis, bob, shared, private, agent


def ask(c, bob, travis, shared, agent, **kw):
    return store.visit_request(c, agent_id=agent["id"], host=travis["id"],
                               actor_id=bob["id"], rooms=[shared["id"]],
                               direction="push", **kw)


def msgs(c, room_id):
    return [r["body"] for r in c.execute(
        "SELECT body FROM messages WHERE room=? ORDER BY id", (room_id,))]


def test_the_ask_is_a_row_and_a_room_message_and_mints_nothing(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent, host_machine="thinkpad")
    assert (v["state"], v["owner"], v["host"], v["agent"]) == ("asked", "bob", "travis", "architect")
    assert [r["name"] for r in v["rooms"]] == ["Shared"]
    assert v["token_id"] is None, "an ask mints nothing -- the decision does"
    assert c.execute("SELECT count(*) FROM tokens").fetchone()[0] == 0
    # gate 6: the room carries the record, and it names who decides
    assert "travis decides" in msgs(c, shared["id"])[-1]
    assert store.visits_for(c, travis["id"])[0]["id"] == v["id"]


def test_reach_is_the_intersection_refused_at_ask_time(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    # gate 3: bob owns BobsOwn; travis is not in it, so a visit may not hold it
    with pytest.raises(store.BusError) as e:
        store.visit_request(c, agent_id=agent["id"], host=travis["id"], actor_id=bob["id"],
                            rooms=[shared["id"], private["id"]], direction="push")
    assert "travis is not in 'BobsOwn'" in str(e.value)
    assert c.execute("SELECT count(*) FROM visits").fetchone()[0] == 0
    with pytest.raises(store.BusError):          # and a visit with no rooms is no visit
        store.visit_request(c, agent_id=agent["id"], host=travis["id"], actor_id=bob["id"],
                            rooms=[], direction="push")


def test_a_visit_to_your_own_machine_is_not_a_visit(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    with pytest.raises(store.BusError) as e:
        store.visit_request(c, agent_id=agent["id"], host=bob["id"], actor_id=bob["id"],
                            rooms=[shared["id"]], direction="push")
    assert "not a visit" in str(e.value)


def test_the_asker_does_not_decide_and_an_accept_is_consumed_once(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent)
    with pytest.raises(store.AccessError):
        store.visit_decide(c, v["id"], bob["id"], "accept")       # gate 1: the asker
    out = store.visit_decide(c, v["id"], travis["id"], "accept")
    assert out["state"] == "accepted" and out["body"] == "container"
    assert len(out["secret"]) > 20
    with pytest.raises(store.BusError) as e:
        store.visit_decide(c, v["id"], travis["id"], "accept")
    assert "already accept" in str(e.value)
    # the secret is answered to the screen, never kept on the row
    row = dict(c.execute("SELECT * FROM visits WHERE id=?", (v["id"],)).fetchone())
    assert out["secret"] not in json.dumps(row)


def test_the_accept_mints_one_body_and_the_home_one_goes_dark(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    home = store.create_token(c, bob["id"], agent_name="architect", rooms=[shared["id"]])
    assert store.resolve_token(c, home["secret"])["id"] == home["id"]
    v = ask(c, bob, travis, shared, agent)
    out = store.visit_decide(c, v["id"], travis["id"], "accept", body="native")
    # gate 4: one identity, one live body -- but the displacement is the
    # VISITING BODY'S ARRIVAL, not the accept (two-phase, ruling 11945). An
    # accept that killed the home body before the harbor booted is how a visit
    # that never starts costs an agent its only credential.
    assert store.resolve_token(c, home["secret"])["id"] == home["id"], \
        "the home body works until the visitor proves it arrived"
    tok = store.resolve_token(c, out["secret"])
    assert tok["pending"], "the visit's credential is pending until it joins"
    store.join(c, "architect", "agent", shared["id"], token_id=tok["id"])
    assert store.resolve_token(c, home["secret"]) is None, "arrival displaces it"
    tok = store.resolve_token(c, out["secret"])
    assert tok["agent_id"] == agent["id"] and tok["owner_id"] == bob["id"]
    assert list(store.rooms_for_token(c, tok["id"])) == [shared["id"]]
    assert store.visit_get(c, v["id"])["body"] == "native"


def test_arrival_is_stamped_by_the_body_reaching_the_bus(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent, host_machine="thinkpad")
    out = store.visit_decide(c, v["id"], travis["id"], "accept")
    tid = store.resolve_token(c, out["secret"])["id"]
    assert store.visit_get(c, v["id"])["state"] == "accepted"
    store.join(c, "architect", "container", shared["id"], tid)
    assert store.visit_get(c, v["id"])["state"] == "visiting"
    assert "arrived on travis's machine thinkpad" in msgs(c, shared["id"])[-1]
    # idempotent: a second join is not a second arrival
    before = len(msgs(c, shared["id"]))
    store.join(c, "architect", "container", shared["id"], tid)
    assert len(msgs(c, shared["id"])) == before


def test_either_human_ends_it_and_the_credential_dies_with_the_visit(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent)
    out = store.visit_decide(c, v["id"], travis["id"], "accept")
    ended = store.visit_end(c, v["id"], travis["id"])            # the host evicts
    assert (ended["state"], ended["ended_by"]) == ("ended", "host")
    assert store.resolve_token(c, out["secret"]) is None
    assert "evicted by the host" in msgs(c, shared["id"])[-1]
    with pytest.raises(store.BusError):
        store.visit_end(c, v["id"], bob["id"])
    # and the name is free to visit again once nothing is open
    again = ask(c, bob, travis, shared, agent)
    assert again["state"] == "asked"
    out2 = store.visit_decide(c, again["id"], travis["id"], "accept")
    recalled = store.visit_end(c, again["id"], bob["id"])        # the owner recalls
    assert recalled["ended_by"] == "owner"
    assert store.resolve_token(c, out2["secret"]) is None


def test_the_visiting_agent_may_depart_on_its_own_token(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent)
    out = store.visit_decide(c, v["id"], travis["id"], "accept")
    tid = store.resolve_token(c, out["secret"])["id"]
    with pytest.raises(store.AccessError):
        store.visit_end(c, v["id"], "", agent_token_id="somebody-elses")
    ended = store.visit_end(c, v["id"], "", agent_token_id=tid)
    assert ended["ended_by"] == "agent"


def test_one_identity_one_open_visit(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    ask(c, bob, travis, shared, agent)
    with pytest.raises(store.BusError) as e:
        ask(c, bob, travis, shared, agent)
    assert "already asked for" in str(e.value)


def test_an_unanswered_ask_expires(tmp_path):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent, hours=0)
    assert store.visit_get(c, v["id"])["state"] == "expired"
    with pytest.raises(store.BusError) as e:
        store.visit_decide(c, v["id"], travis["id"], "accept")
    assert "expired" in str(e.value)
    # an expired ask blocks nothing: asking again is the remedy
    assert ask(c, bob, travis, shared, agent)["state"] == "asked"


# ---- the HTTP surface ----------------------------------------------------

def _req(method, path_params, body, p):
    scope = {"type": "http", "method": method, "path": "/visits", "headers": [],
             "query_string": b"", "path_params": path_params}

    async def receive():
        return {"type": "http.request", "body": json.dumps(body).encode(), "more_body": False}
    r = Request(scope, receive)
    r.scope["_p"] = p
    return r


def _call(fn, req, conn, monkeypatch):
    monkeypatch.setattr(daemon, "_conn", conn)
    monkeypatch.setattr(daemon, "_user_principal", lambda request: request.scope["_p"])
    monkeypatch.setattr(daemon, "_principal", lambda request: request.scope["_p"])
    r = asyncio.run(fn(req))
    return r.status_code, json.loads(r.body)


def _p(user, name, kind="user"):
    return daemon.Principal(kind=kind, name=name, user_id=user, rooms={})


def test_the_routes_ask_decide_and_end(tmp_path, monkeypatch):
    c, travis, bob, shared, private, agent = world(tmp_path)
    st, v = _call(daemon.visits_http,
                  _req("POST", {}, {"agent": "architect", "host": "travis",
                                    "rooms": [shared["id"]], "host_machine": "thinkpad"},
                       _p(bob["id"], "bob")), c, monkeypatch)
    assert (st, v["direction"], v["state"]) == (200, "push", "asked")
    # the host pulls the list and sees it as a party to it
    st, out = _call(daemon.visits_http, _req("GET", {}, {}, _p(travis["id"], "travis")),
                    c, monkeypatch)
    assert [x["id"] for x in out["visits"]] == [v["id"]]
    st, acc = _call(daemon.visit_http,
                    _req("POST", {"vid": v["id"], "verb": "accept"}, {"body": "container"},
                         _p(travis["id"], "travis")), c, monkeypatch)
    assert st == 200 and acc["secret"] and acc["state"] == "accepted"
    st, err = _call(daemon.visit_http,
                    _req("POST", {"vid": v["id"], "verb": "accept"}, {},
                         _p(travis["id"], "travis")), c, monkeypatch)
    assert st == 400
    st, end = _call(daemon.visit_http,
                    _req("POST", {"vid": v["id"], "verb": "end"}, {},
                         _p(bob["id"], "bob")), c, monkeypatch)
    assert (st, end["ended_by"]) == (200, "owner")


def test_a_pull_is_asked_by_the_host(tmp_path, monkeypatch):
    c, travis, bob, shared, private, agent = world(tmp_path)
    st, v = _call(daemon.visits_http,
                  _req("POST", {}, {"agent": "architect", "owner": "bob",
                                    "rooms": [shared["id"]]},
                       _p(travis["id"], "travis")), c, monkeypatch)
    assert (st, v["direction"]) == (200, "pull")
    # ... and decided by the owner, not by the host who asked
    st, _ = _call(daemon.visit_http,
                  _req("POST", {"vid": v["id"], "verb": "accept"}, {},
                       _p(travis["id"], "travis")), c, monkeypatch)
    assert st == 403
    st, acc = _call(daemon.visit_http,
                    _req("POST", {"vid": v["id"], "verb": "accept"}, {},
                         _p(bob["id"], "bob")), c, monkeypatch)
    assert st == 200 and acc["secret"]


def test_a_reject_mints_nothing_and_says_so(tmp_path, monkeypatch):
    c, travis, bob, shared, private, agent = world(tmp_path)
    v = ask(c, bob, travis, shared, agent)
    st, out = _call(daemon.visit_http,
                    _req("POST", {"vid": v["id"], "verb": "reject"}, {},
                         _p(travis["id"], "travis")), c, monkeypatch)
    assert (st, out["state"], out.get("secret")) == (200, "reject", None)
    assert c.execute("SELECT count(*) FROM tokens").fetchone()[0] == 0
    assert "rejected" in msgs(c, shared["id"])[-1]


# ---- the accept screen ---------------------------------------------------

PAGE = daemon._ui_read("index.html")


def test_the_accept_screen_consents_to_a_sentence():
    """DES-012 s7: the human consents to what travels, not to a button."""
    start = PAGE.index("// ---- DES-012: a visit is a body swap")
    panel = PAGE[start:PAGE.index("async function openTokens()")]
    for phrase in ("stays theirs", "KEEPS WORKING until this one joins", "and in nothing else",
                   "YOUR Claude account and bill", "state notes travel",
                   "container harbor" if "container harbor" in panel else "recommended",
                   "evict it at any time", "recall it at any time"):
        assert phrase in panel, phrase
    # The harbor never RUNS the native install -- same rule as DES-008 item D.
    for sink in ("href", "window.open", "exec", "child_process"):
        assert sink not in panel, sink
    assert "Visits" in PAGE and "visits:()=>openVisits()" in PAGE
