"""DES-013 slice 3: who speaks with what, per room.

Bounds under test, from sections 2 and 4: ONE derivation of the speaker key
from the credential (agent:<id> for a bound token, user:<id> for a web user,
None unbound); the routes run the pure rule -- room owner sets over nothing /
default / room, never over owner; the speaker's owner overrides; a stranger and
an admin alike get 403; a collision names the holder; an unbound agent is
unassignable; a voice carries across rooms when free; the listing materializes
defaults so an owner sees them before the first message; the send path resolves
the assignment through the same key and hands it to the worker.

Proven RED on feat/des-013-the-bank @ a56b175: Principal has no agent_id,
speaker_key / room_voices_http do not exist, _tts_enqueue takes assigned=.
"""
import asyncio
import json
import os
import sys

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402


def test_the_speaker_key_comes_from_the_credential_and_nowhere_else():
    K = daemon.speaker_key
    assert K(daemon.Principal(kind="agent", name="picard", user_id="u1", agent_id="a1")) == "agent:a1"
    assert K(daemon.Principal(kind="agent", name="picard", user_id="u1")) is None
    assert K(daemon.Principal(kind="user", name="travis", user_id="u1")) == "user:u1"
    # The ONE derivation: the send paths and the routes call this function.
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "daemon.py")).read()
    assert src.count("key=speaker_key(p)") == 2, "both send paths pass the key"


def test_a_bound_token_carries_its_agent_id_into_the_principal(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    room = store.create_room(c, admin["id"], "bridge")
    bound = store.create_token(c, admin["id"], "b", agent_name="picard", rooms=[room["id"]],
                               create=True)
    loose = store.create_token(c, admin["id"], "l", rooms=[room["id"]])
    monkeypatch.setattr(daemon, "_conn", c)

    def req(secret, name=""):
        h = [(b"authorization", f"Bearer {secret}".encode())]
        if name:
            h.append((b"x-agent", name.encode()))
        return Request({"type": "http", "method": "GET", "path": "/x", "headers": h,
                        "query_string": b""})
    p = daemon._agent_principal(req(bound["secret"]))
    assert p.agent_id == bound["agent_id"] and daemon.speaker_key(p) == f"agent:{bound['agent_id']}"
    p = daemon._agent_principal(req(loose["secret"], "wanderer"))
    assert p.agent_id == "" and daemon.speaker_key(p) is None


# ---- routes ------------------------------------------------------------------

class _P:
    def __init__(self, kind, name, user_id, rooms, is_admin=False, agent_id=""):
        self.kind, self.name, self.user_id, self.is_admin = kind, name, user_id, is_admin
        self.rooms, self.agent_id = rooms, agent_id


def _req(method, path, params, body=None):
    data = json.dumps(body).encode() if body is not None else b""
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": data, "more_body": False}
    return Request({"type": "http", "method": method, "path": path,
                    "headers": [(b"content-type", b"application/json")],
                    "query_string": b"", "path_params": params}, receive)


def _call(fn, req):
    resp = asyncio.run(fn(req))
    return resp.status_code, json.loads(resp.body)


@pytest.fixture
def world(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    user = store.create_user(c, "vyzon", "hunter2hunter2")
    other = store.create_user(c, "randy", "hunter2hunter2")
    r1 = store.create_room(c, user["id"], "engineering")     # vyzon's room
    r2 = store.create_room(c, admin["id"], "bridge")
    a_admin = store.mint_agent(c, admin["id"], "picard")     # travis's agent
    a_user = store.mint_agent(c, user["id"], "scotty")       # vyzon's agent
    for vid in ("mr-scott", "picard", "quark"):
        store.voice_put(c, vid, name=vid, uploaded_by=admin["id"], seconds=8, nbytes=1)
    monkeypatch.setattr(daemon, "_conn", c)
    who = {}
    monkeypatch.setattr(daemon, "_principal", lambda request: who["p"])
    rooms = {r1["id"]: "engineering", r2["id"]: "bridge"}
    P = {
        "vyzon": _P("user", "vyzon", user["id"], rooms),
        "travis": _P("user", "travis", admin["id"], rooms, is_admin=True),
        "randy": _P("user", "randy", other["id"], rooms),
        "picard": _P("agent", "picard", admin["id"], rooms, agent_id=a_admin["id"]),
    }
    return dict(c=c, r1=r1, r2=r2, kp=f"agent:{a_admin['id']}", ks=f"agent:{a_user['id']}",
                who=who, P=P, user=user, admin=admin)


def _put(w, actor, rid, speaker, voice):
    w["who"]["p"] = w["P"][actor]
    return _call(daemon.room_voice_http,
                 _req("PUT", f"/rooms/{rid}/voices/{speaker}", {"rid": rid, "speaker": speaker},
                      {"voice_id": voice}))


def _delete(w, actor, rid, speaker):
    w["who"]["p"] = w["P"][actor]
    return _call(daemon.room_voice_http,
                 _req("DELETE", f"/rooms/{rid}/voices/{speaker}", {"rid": rid, "speaker": speaker}))


def _get(w, actor, rid):
    w["who"]["p"] = w["P"][actor]
    return _call(daemon.room_voices_http, _req("GET", f"/rooms/{rid}/voices", {"rid": rid}))


def test_room_owner_over_default_never_over_owner_and_the_owner_overrides(world):
    w = world
    r1, kp = w["r1"]["id"], w["kp"]     # vyzon's room, travis's agent picard
    # Room owner (vyzon) sets travis's agent: allowed, set_by=room.
    st, out = _put(w, "vyzon", r1, kp, "quark")
    assert st == 200 and out["set_by"] == "room"
    # The speaker's owner (travis) overrides: set_by=owner.
    st, out = _put(w, "travis", r1, kp, "picard")
    assert st == 200 and out["set_by"] == "owner"
    # Room owner may not displace an owner-set voice.
    st, out = _put(w, "vyzon", r1, kp, "mr-scott")
    assert st == 403 and "owner" in out["detail"]
    st, out = _delete(w, "vyzon", r1, kp)
    assert st == 403
    # The owner may unset; then the room owner may set again.
    st, out = _delete(w, "travis", r1, kp)
    assert st == 200 and out["removed"] == 1
    st, out = _put(w, "vyzon", r1, kp, "mr-scott")
    assert st == 200 and out["set_by"] == "room"


def test_a_stranger_and_an_admin_alike_are_refused_and_the_agent_owner_acts_as_owner(world):
    w = world
    r1, ks = w["r1"]["id"], w["ks"]     # vyzon's room, vyzon's agent scotty
    st, out = _put(w, "randy", r1, ks, "quark")
    assert st == 403
    # travis is admin but neither room owner nor scotty's owner: no reach.
    st, out = _put(w, "travis", r1, ks, "quark")
    assert st == 403 and "admin has no reach" in out["detail"]
    # The agent principal picard (bound to travis's agent) sets ITS OWN voice
    # in vyzon's room -- an agent's owner is its user, and the credential says so.
    st, out = _put(w, "picard", r1, w["kp"], "quark")
    assert st == 200 and out["set_by"] == "owner"


def test_a_collision_names_the_holder_whoever_asks(world):
    w = world
    r1, kp, ks = w["r1"]["id"], w["kp"], w["ks"]
    assert _put(w, "vyzon", r1, ks, "quark")[0] == 200
    st, out = _put(w, "travis", r1, kp, "quark")      # picard's owner wants scotty's voice
    assert st == 400 and "held by scotty" in out["error"]
    st, out = _put(w, "vyzon", r1, kp, "quark")       # the room owner too
    assert st == 400 and "held by scotty" in out["error"]
    # Re-asserting one's own voice is not a collision.
    assert _put(w, "vyzon", r1, ks, "quark")[0] == 200


def test_an_unbound_or_unknown_speaker_is_unassignable(world):
    w = world
    st, out = _put(w, "vyzon", w["r1"]["id"], "agent:nope", "quark")
    assert st == 400 and "unbound" in out["error"]
    st, out = _put(w, "vyzon", w["r1"]["id"], w["ks"], "nope")
    assert st == 400 and "no such bank voice" in out["error"]
    w["who"]["p"] = _P("user", "vyzon", w["user"]["id"], {})   # room out of reach
    st, out = _call(daemon.room_voice_http,
                    _req("PUT", "/x", {"rid": w["r1"]["id"], "speaker": w["ks"]}, {"voice_id": "quark"}))
    assert st == 403


def test_the_listing_materializes_defaults_carries_across_rooms_and_marks_editable(world):
    w = world
    r1, r2, kp = w["r1"]["id"], w["r2"]["id"], w["kp"]
    assert _put(w, "travis", r1, kp, "quark")[0] == 200       # picard: quark in r1 (owner)
    # A message from picard in r2 (send path) resolves through the same key.
    # Ruling 11121: EXPLICIT choices travel -- the quark his owner chose in r1
    # comes to r2 ahead of the bank voice named picard. Materialized as default.
    assert store.voice_for(w["c"], r2, kp) == "quark"
    st, out = _get(w, "vyzon", r2)                              # vyzon reads r2 (travis's room)
    assert st == 200
    by = {s["speaker"]: s for s in out["speakers"]}
    assert by[kp]["voice_id"] == "quark" and by[kp]["set_by"] == "default"
    assert by[kp]["editable"] is False, "vyzon is neither room owner nor picard's owner"
    # vyzon's own row is present (a human picks their own voice before speaking),
    # editable, and materialized with the first free voice.
    me = f"user:{w['user']['id']}"
    assert by[me]["you"] is True and by[me]["editable"] is True
    assert by[me]["voice_id"] == "mr-scott" and by[me]["set_by"] == "default"
    assert out["taken"] == {"quark": "picard", "mr-scott": "vyzon"}
    assert [v["id"] for v in out["voices"]] == ["mr-scott", "picard", "quark"]
    # travis (room owner of r2) sees BOTH rows editable: picard's as its owner,
    # vyzon's because a default yields to the room owner (section 4).
    st, out = _get(w, "travis", r2)
    by = {s["speaker"]: s for s in out["speakers"]}
    assert by[kp]["editable"] is True and by[me]["editable"] is True
    # ...until vyzon sets their own: owner-set, and the room owner yields.
    assert _put(w, "vyzon", r2, me, "mr-scott")[0] == 200
    st, out = _get(w, "travis", r2)
    assert {s["speaker"]: s for s in out["speakers"]}[me]["editable"] is False


def test_the_send_path_hands_the_assigned_voice_to_the_worker(world, monkeypatch):
    w = world
    r1, kp = w["r1"]["id"], w["kp"]
    assert _put(w, "travis", r1, kp, "picard")[0] == 200
    monkeypatch.setattr(daemon, "_tts_on", True)
    while not daemon._tts_q.empty():
        daemon._tts_q.get_nowait()
    daemon._tts_enqueue(9, r1, "picard", "s", "b", key=kp)
    daemon._tts_enqueue(10, r1, "wanderer", "s", "b", key=None)
    v = store.voice_get(w["c"], "picard")
    assert daemon._tts_q.get_nowait() == (9, r1, "picard", "s. b", daemon.clip_name(v))
    assert daemon._tts_q.get_nowait() == (10, r1, "wanderer", "s. b", None)


def test_the_voices_tab_renders_who_speaks_with_what_through_the_routes():
    ui = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                           "index.html")).read()
    assert "'/rooms/'+encodeURIComponent(r.id)+'/voices'" in ui
    assert "'/rooms/'+encodeURIComponent(rid)+'/voices/'+encodeURIComponent(spk)" in ui
    assert "method:'PUT',body:JSON.stringify({voice_id:v})" in ui
    assert "{method:'DELETE'}" in ui
    assert "held by '+esc(taken[v.id])" in ui        # the holder is named, escaped
    assert "esc(sp.name)" in ui and "esc(sp.kind)" in ui


def test_a_membership_healed_by_readmit_is_still_keyed_by_its_bound_token(world):
    """Found on the eval box: an agent that never called join() (send readmits
    the membership with token_id only, agent_id NULL) listed twice -- once by
    its assignment, once as a present 'unbound' row. The key comes from the
    credential: the member's bound token names the agent."""
    w = world
    c, r2 = w["c"], w["r2"]["id"]          # travis's room, travis's agent
    tok = store.create_token(c, w["admin"]["id"], "b", agent_name="picard", rooms=[r2])
    store.readmit(c, "picard", "t", [r2], token_id=tok["id"])
    assert store.voice_for(c, r2, w["kp"]) is not None
    rows = store.room_speakers(c, r2)
    assert [r["speaker"] for r in rows] == [w["kp"]], rows
    assert rows[0]["present"] is True
