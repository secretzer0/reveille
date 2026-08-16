"""DES-013 slice 4: the artifacts surface -- /script/<mid>, has_audio/has_script
on every listing, and the client's play/script icons.

Bounds under test (section 6, section 7): the script route mirrors audio_http
(room from the message, ?room= ignored, stranger 404s, none 404s); every
listing carries has_script (one IN query) and has_audio (wav or .part on disk);
the page renders the icons from the flags, paints them on the audio/script
frames, plays through the ONE audio-URL builder, and toggles body <-> script.

Proven RED on main @ d70a43c: script_http does not exist and listings carry no
flags.
"""
import asyncio
import json
import os
import sys

from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

UI = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                       "index.html")).read()


class _P:
    def __init__(self, rooms):
        self.rooms = rooms


def _req(mid, query=b""):
    return Request({"type": "http", "method": "GET", "path": f"/script/{mid}", "headers": [],
                    "query_string": query, "path_params": {"mid": str(mid)}})


def _call(req):
    r = asyncio.run(daemon.script_http(req))
    return r.status_code, json.loads(r.body)


def _seed(tmp_path, monkeypatch):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    r1 = store.create_room(c, admin["id"], "bridge")
    r2 = store.create_room(c, admin["id"], "ten-forward")
    m = store.send(c, "picard", "*", "terse", room=r1["id"])
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(store, "AUDIO_DIR", str(tmp_path))
    return c, r1["id"], r2["id"], m["id"]


def test_the_script_route_mirrors_audio_http(tmp_path, monkeypatch):
    c, r1, r2, mid = _seed(tmp_path, monkeypatch)
    who = {"p": _P({r1: "bridge"})}
    monkeypatch.setattr(daemon, "_principal", lambda request: who["p"])
    assert _call(_req(mid))[0] == 404, "no script yet = a defined 404"
    store.script_put(c, mid, "Make it so.", "picard", "qwen", 900)
    st, out = _call(_req(mid))
    assert st == 200 and out == {"id": mid, "text": "Make it so.", "voice_id": "picard",
                                 "model": "qwen", "ts_ns": out["ts_ns"]}
    # ?room= is ignored: the message's room is the authority.
    assert _call(_req(mid, f"room={r2}".encode()))[0] == 200
    who["p"] = _P({r2: "ten-forward"})
    assert _call(_req(mid))[0] == 404, "a stranger to the room sees nothing"
    assert _call(_req(mid, f"room={r1}".encode()))[0] == 404, "naming the room buys nothing"
    assert _call(_req("x"))[0] == 404 and _call(_req(mid + 9))[0] == 404


def test_every_listing_carries_the_artifact_flags(tmp_path, monkeypatch):
    c, r1, r2, mid = _seed(tmp_path, monkeypatch)
    rows = store.tail(c, rooms=[r1])
    assert rows[0]["has_script"] is False and rows[0]["has_audio"] is False
    store.script_put(c, mid, "x", None, "m", 1)
    (tmp_path / f"tts-{mid}.wav.part").write_bytes(b"RIFF")
    for fn in (lambda: store.tail(c, rooms=[r1]),
               lambda: store.thread(c, mid, [r1]),
               lambda: store.search(c, keywords=["terse"], rooms=[r1])):
        row = fn()[0]
        assert row["has_script"] is True and row["has_audio"] is True, fn
    os.rename(tmp_path / f"tts-{mid}.wav.part", tmp_path / f"tts-{mid}.wav")
    assert store.tail(c, rooms=[r1])[0]["has_audio"] is True
    with store.tx(c):
        store._delete_messages(c, [mid])
    assert store.tail(c, rooms=[r1]) == []


def test_the_page_paints_icons_from_the_flags_and_the_frames():
    assert "function artIcons(m)" in UI and "m.has_audio?" in UI and "m.has_script?" in UI
    assert 'data-play="' in UI and 'data-scr="' in UI
    # Frames: audio sets has_audio and repaints before queueing; script caches the text.
    case = UI[UI.index("case 'audio':"):]
    case = case[:case.index("\n")]
    assert "mm.has_audio=true" in case and "paintIcons(mm)" in case and "vPush(mm)" in case
    case = UI[UI.index("case 'script':"):]
    case = case[:case.index("\n")]
    assert "mm.has_script=true" in case and "mm.script=m.text" in case
    # Play is an explicit gesture: stops what sounds, plays through the ONE url builder.
    play = UI[UI.index("function playOne(id){"):]
    play = play[:play.index("\n}\n")]
    assert "vStop();" in play and "vPlay(id,vUrl(id))" in play
    assert UI.count("'/audio/'") == 1
    # The script view replaces the body in place and a second click restores esc(m.body).
    tog = UI[UI.index("async function toggleScript(row,m){"):]
    tog = tog[:tog.index("\n}\n")]
    assert "body.innerHTML=esc(m.body)" in tog and "mdToHtml(m.script)" in tog
    assert "'/script/'+encodeURIComponent(m.id)" in tog
    assert "generated, in character" in tog
    # Row click ignores the icons (they own their click), like .mid does.
    assert "if(e.target.closest('.play')){playOne(m.id);return;}" in UI
    assert "if(e.target.closest('.scr')){toggleScript(row,m);return;}" in UI
