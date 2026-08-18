"""DES-013 personal voices, delete and rename (ruling 11155).

ONE INVARIANT: a personal voice EXISTS only for its uploader. Every route reads
the voice through _voice_in_reach; anyone else -- admin included -- gets the
same answer as a nonexistent id. The default resolver may pick it only for
user:<uploader>. personal is immutable after creation. DELETE drops assignments
and the clip; RENAME carries assignments, the scripts label and the clip.
"""
import asyncio
import io
import json
import os
import sys
import wave

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402


def _tone(n, sampwidth=2):
    """n samples of a 500 Hz square wave at a quarter of full scale (-12 dBFS):
    a clip that has SIGNAL, which the peak gate demands of every upload."""
    amp = (1 << (8 * sampwidth - 1)) // 4
    if sampwidth == 1:
        pos, neg = bytes([128 + amp]), bytes([128 - amp])
    else:
        pos, neg = amp.to_bytes(sampwidth, "little", signed=True), (-amp).to_bytes(sampwidth, "little", signed=True)
    return ((pos * 24 + neg * 24) * (n // 48 + 1))[:n * sampwidth]


def wav(seconds=6.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)  # noqa: E702
        w.writeframes(_tone(int(seconds * rate)))
    return buf.getvalue()


class _P:
    def __init__(self, kind, name, user_id, rooms, is_admin=False, agent_id=""):
        self.kind, self.name, self.user_id, self.is_admin = kind, name, user_id, is_admin
        self.rooms, self.agent_id = rooms, agent_id


def _req(method, path, params, body=None, raw=None, query=""):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else b"")
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": data, "more_body": False}
    ctype = b"application/octet-stream" if raw is not None else b"application/json"
    return Request({"type": "http", "method": method, "path": path,
                    "headers": [(b"content-type", ctype), (b"content-length", str(len(data)).encode())],
                    "query_string": query.encode(), "path_params": params}, receive)


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
    r1 = store.create_room(c, user["id"], "engineering")
    a_user = store.mint_agent(c, user["id"], "scotty")
    for vid in ("mr-scott", "picard"):
        store.voice_put(c, vid, name=vid, uploaded_by=admin["id"], seconds=8, nbytes=1)
    vd = tmp_path / "voices"
    vd.mkdir()
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_voices_dir", vd)
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_on", False)
    who = {}
    monkeypatch.setattr(daemon, "_principal", lambda request: who["p"])
    rooms = {r1["id"]: "engineering"}
    P = {"vyzon": _P("user", "vyzon", user["id"], rooms),
         "travis": _P("user", "travis", admin["id"], rooms, is_admin=True),
         "randy": _P("user", "randy", other["id"], rooms)}
    return dict(c=c, r1=r1["id"], ks=f"agent:{a_user['id']}", who=who, P=P, user=user,
                admin=admin, other=other, vd=vd)


def _as(w, actor):
    w["who"]["p"] = w["P"][actor]


def _put_clip(w, actor, vid, query=""):
    _as(w, actor)
    return _call(daemon.voice_clip_http, _req("PUT", f"/voices/{vid}/clip", {"vid": vid},
                                              raw=wav(6.0), query=query))


def _voices(w, actor):
    _as(w, actor)
    return _call(daemon.voices_http, _req("GET", "/voices", {}))[1]


def test_a_personal_voice_exists_only_for_its_uploader(world):
    w = world
    st, out = _put_clip(w, "vyzon", "vyzon", "personal=1")
    assert st == 200 and out["personal"] == 1
    # The uploader sees it; the admin and a stranger do not -- in the bank list,
    # the room list, and on every per-voice route, the SAME answer as a bad id.
    assert {v["id"] for v in _voices(w, "vyzon")["voices"]} == {"mr-scott", "picard", "vyzon"}
    assert {v["id"] for v in _voices(w, "travis")["voices"]} == {"mr-scott", "picard"}
    assert {v["id"] for v in _voices(w, "randy")["voices"]} == {"mr-scott", "picard"}
    for actor in ("travis", "randy"):
        _as(w, actor)
        st, out = _call(daemon.voice_http, _req("PATCH", "/voices/vyzon", {"vid": "vyzon"},
                                                {"persona": "x"}))
        assert st == 404 and out["error"] == "no such bank voice"
        st, out = _call(daemon.voice_delete_http, _req("DELETE", "/voices/vyzon", {"vid": "vyzon"}))
        assert st == 404
        st, out = _call(daemon.voice_say_http, _req("GET", "/voices/vyzon/say", {"vid": "vyzon"},
                                                    query="text=hi"))
        assert st == 404
        st, out = _call(daemon.voice_clip_get_http, _req("GET", "/voices/vyzon/clip", {"vid": "vyzon"}))
        assert st == 404
        st, out = _call(daemon.voice_rename_http, _req("PUT", "/voices/vyzon/rename",
                                                       {"vid": "vyzon"}, {"id": "mine"}))
        assert st == 404
        st, out = _call(daemon.room_voice_http, _req("PUT", f"/rooms/{w['r1']}/voices/{w['ks']}",
                                                     {"rid": w["r1"], "speaker": w["ks"]},
                                                     {"voice_id": "vyzon"}))
        assert st == 404, actor
        # A stranger's PUT onto the id neither replaces it nor names it: taken.
        st, out = _put_clip(w, actor, "vyzon")
        assert st == 409 and "taken" in out["error"]
    _as(w, "randy")
    st, out = _call(daemon.room_voices_http, _req("GET", f"/rooms/{w['r1']}/voices", {"rid": w["r1"]}))
    assert st == 200 and "vyzon" not in {v["id"] for v in out["voices"]}
    # The uploader assigns it to their own agent (owner rule) -- the reach is
    # normal, only the pool is private.
    _as(w, "vyzon")
    st, out = _call(daemon.room_voice_http, _req("PUT", f"/rooms/{w['r1']}/voices/{w['ks']}",
                                                 {"rid": w["r1"], "speaker": w["ks"]},
                                                 {"voice_id": "vyzon"}))
    assert st == 200 and out["set_by"] == "owner"
    # A stranger's room list says the agent holds a personal voice, not which.
    _as(w, "randy")
    st, out = _call(daemon.room_voices_http, _req("GET", f"/rooms/{w['r1']}/voices", {"rid": w["r1"]}))
    row = next(sp for sp in out["speakers"] if sp["speaker"] == w["ks"])
    assert row["voice_id"] is None and row["personal"] is True and "vyzon" not in out["taken"]
    _as(w, "vyzon")
    st, out = _call(daemon.room_voices_http, _req("GET", f"/rooms/{w['r1']}/voices", {"rid": w["r1"]}))
    row = next(sp for sp in out["speakers"] if sp["speaker"] == w["ks"])
    assert row["voice_id"] == "vyzon" and row["personal"] is False


def test_the_default_picks_a_personal_voice_only_for_its_uploader_and_by_name(world):
    w, c = world, world["c"]
    store.voice_put(c, "vyzon", name="Vyzon", uploaded_by=w["user"]["id"], seconds=6, nbytes=1,
                    personal=True)
    # The human who recorded "vyzon" gets it by rule 2 (name beats derived).
    assert store.voice_for(c, w["r1"], f"user:{w['user']['id']}") == "vyzon"
    # Nobody else's default ever sees it: randy (a user), scotty (an agent).
    r2 = store.create_room(c, w["other"]["id"], "lounge")["id"]
    assert store.voice_for(c, r2, f"user:{w['other']['id']}") in ("mr-scott", "picard")
    assert store.voice_for(c, r2, w["ks"]) in ("mr-scott", "picard")
    assert "vyzon" not in [v["id"] for v in store.voices(c)]
    assert "vyzon" in [v["id"] for v in store.all_voices(c)], "the reconcile pushes it"


def test_personal_is_immutable_after_creation(world):
    w = world
    st, out = _put_clip(w, "vyzon", "vyzon", "personal=1")
    assert out["personal"] == 1
    st, out = _put_clip(w, "vyzon", "vyzon", "personal=0")           # a replace
    assert st == 200 and out["personal"] == 1
    st, out = _put_clip(w, "vyzon", "shared")                          # bank by default
    assert out["personal"] == 0
    st, out = _put_clip(w, "vyzon", "shared", "personal=1")
    assert out["personal"] == 0


def test_delete_drops_assignments_and_the_clip_and_refuses_strangers(world):
    w, c = world, world["c"]
    st, _ = _put_clip(w, "vyzon", "rom")
    assert st == 200 and (w["vd"] / "bank-rom.wav").exists()
    store.assign_voice(c, w["r1"], w["ks"], "rom", set_by="owner")
    _as(w, "randy")
    st, out = _call(daemon.voice_delete_http, _req("DELETE", "/voices/rom", {"vid": "rom"}))
    assert st == 403
    _as(w, "vyzon")
    st, out = _call(daemon.voice_delete_http, _req("DELETE", "/voices/rom", {"vid": "rom"}))
    assert st == 200 and out["deleted"] == 1
    assert store.voice_get(c, "rom") is None and not (w["vd"] / "bank-rom.wav").exists()
    assert c.execute("SELECT count(*) FROM voice_assignments WHERE voice_id='rom'").fetchone()[0] == 0
    # The speaker re-defaults on the next voice_for.
    assert store.voice_for(c, w["r1"], w["ks"]) in ("mr-scott", "picard")
    # An admin deletes a bank voice; a second delete is a 404.
    _as(w, "travis")
    st, out = _call(daemon.voice_delete_http, _req("DELETE", "/voices/picard", {"vid": "picard"}))
    assert st == 200
    st, out = _call(daemon.voice_delete_http, _req("DELETE", "/voices/picard", {"vid": "picard"}))
    assert st == 404


def test_rename_carries_assignments_scripts_and_the_clip_and_refuses_collisions(world):
    w, c = world, world["c"]
    st, row = _put_clip(w, "vyzon", "rom")
    store.voice_patch(c, "rom", persona="Meek.", sample="Brother?")
    store.assign_voice(c, w["r1"], w["ks"], "rom", set_by="owner")
    m = store.send(c, w["ks"], "*", "hello", room=w["r1"])
    store.script_put(c, m["id"], "Hello, brother.", "rom", "stub", 5)
    _as(w, "vyzon")
    st, out = _call(daemon.voice_rename_http, _req("PUT", "/voices/rom/rename", {"vid": "rom"},
                                                   {"id": "picard"}))
    assert st == 409 and "taken" in out["error"]
    st, out = _call(daemon.voice_rename_http, _req("PUT", "/voices/rom/rename", {"vid": "rom"},
                                                   {"id": "Rom Ferengi"}))
    assert st == 400                       # not a valid id
    assert (w["vd"] / "bank-rom.wav").exists(), "a refused rename moves nothing"
    st, out = _call(daemon.voice_rename_http, _req("PUT", "/voices/rom/rename", {"vid": "rom"},
                                                   {"id": "rom-ferengi"}))
    assert st == 200 and out["id"] == "rom-ferengi" and out["persona"] == "Meek." \
        and out["sample"] == "Brother?" and out["created_ns"] == row["created_ns"] \
        and out["updated_ns"] > row["updated_ns"]
    assert store.voice_get(c, "rom") is None
    assert (w["vd"] / "bank-rom-ferengi.wav").exists() and not (w["vd"] / "bank-rom.wav").exists()
    assert store.voice_for(c, w["r1"], w["ks"]) == "rom-ferengi"
    assert store.script_get(c, m["id"])["voice_id"] == "rom-ferengi"
    # A stranger cannot rename; a rename to itself is a no-op 200.
    _as(w, "randy")
    st, out = _call(daemon.voice_rename_http, _req("PUT", "/voices/rom-ferengi/rename",
                                                   {"vid": "rom-ferengi"}, {"id": "x"}))
    assert st == 403
    _as(w, "vyzon")
    st, out = _call(daemon.voice_rename_http, _req("PUT", "/voices/rom-ferengi/rename",
                                                   {"vid": "rom-ferengi"}, {"id": "rom-ferengi"}))
    assert st == 200 and out["id"] == "rom-ferengi"
