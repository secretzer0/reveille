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
    kind = "user"            # a web user: not a token, so _act passes it
    agent_id = ""
    name = "travis"

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


def _ask(mid, query=b""):
    req = Request({"type": "http", "method": "POST", "path": f"/audio/{mid}", "headers": [],
                   "query_string": query, "path_params": {"mid": str(mid)}})
    r = asyncio.run(daemon.audio_make_http(req))
    return r.status_code, json.loads(r.body)


def test_audio_is_made_on_demand_for_a_message_that_has_none(tmp_path, monkeypatch):
    """Operator directive 2026-08-17: every message can be spoken later, on
    the click -- POST /audio/<id> queues it through the same enqueue a live send
    takes (script first when the writer is on), the ROOM GATE PASSED because the
    click is the listener; auth is the message's room; a second ask while it is
    queued or in flight is answered, not queued twice; existing audio is 'ready';
    voices off is a 503 by name."""
    c, r1, r2, mid = _seed(tmp_path, monkeypatch)
    who = {"p": _P({r1: "bridge"})}
    who["p"].name = "travis"
    monkeypatch.setattr(daemon, "_principal", lambda request: who["p"])
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_room_listening", lambda room: False)   # nobody has voice on
    daemon._tts_requested.clear()
    queued = []
    monkeypatch.setattr(daemon, "_tts_enqueue",
                        lambda *a, **k: queued.append((a, k)))
    monkeypatch.setattr(daemon, "_tts_on", False)
    assert _ask(mid) == (503, {"error": "voices are off on this broker"})
    monkeypatch.setattr(daemon, "_tts_on", True)
    assert _ask("x")[0] == 404 and _ask(mid + 9)[0] == 404
    st, out = _ask(mid, f"room={r2}".encode())
    assert (st, out) == (202, {"state": "queued"}), "?room= is ignored; the message's room rules"
    (args, kw), = queued
    assert args[:2] == (mid, r1) and args[2] == "picard" and args[4] == "terse"
    assert kw["asked"] is True, "the click IS the listener: the room gate is passed"
    assert kw["key"] is None, "an unbound sender (no sender_agent_id, no such user) = digest pick"
    assert _ask(mid) == (200, {"state": "queued"}), "a second ask is answered, not queued twice"
    assert len(queued) == 1
    # In flight (the registry) and ready (the file) answer without queueing.
    daemon._tts_requested.discard(mid)
    daemon._tts_inflight[mid] = object()
    assert _ask(mid) == (200, {"state": "in flight"})
    del daemon._tts_inflight[mid]
    (tmp_path / f"tts-{mid}.webm").write_bytes(b"\x1a\x45\xdf\xa3")
    assert _ask(mid) == (200, {"state": "ready"})
    assert len(queued) == 1
    # A stranger to the room: 404, nothing queued.
    who["p"] = _P({r2: "ten-forward"})
    (tmp_path / f"tts-{mid}.webm").unlink()
    assert _ask(mid)[0] == 404 and len(queued) == 1


def test_the_speaker_key_of_a_stored_message_comes_from_the_row(tmp_path, monkeypatch):
    """No credential is on the wire for backlog: agent:<sender_agent_id> when the
    send recorded one; user:<id> for a web user by its unique name; None otherwise."""
    c, r1, r2, mid = _seed(tmp_path, monkeypatch)
    row = c.execute("SELECT room, sender, subject, body, sender_agent_id FROM messages "
                    "WHERE id=?", (mid,)).fetchone()
    assert daemon._speaker_key_of(row) is None, "picard sent with no identity: digest pick"
    m2 = store.send(c, "travis", "*", "as a human", room=r1)
    row = c.execute("SELECT room, sender, subject, body, sender_agent_id FROM messages "
                    "WHERE id=?", (m2["id"],)).fetchone()
    uid = c.execute("SELECT id FROM users WHERE name='travis'").fetchone()["id"]
    assert daemon._speaker_key_of(row) == f"user:{uid}"
    c.execute("UPDATE messages SET sender_agent_id='agent-7' WHERE id=?", (mid,))
    row = c.execute("SELECT room, sender, subject, body, sender_agent_id FROM messages "
                    "WHERE id=?", (mid,)).fetchone()
    assert daemon._speaker_key_of(row) == "agent:agent-7"


def test_every_listing_carries_the_artifact_flags(tmp_path, monkeypatch):
    c, r1, r2, mid = _seed(tmp_path, monkeypatch)
    rows = store.tail(c, rooms=[r1])
    assert rows[0]["has_script"] is False and rows[0]["has_audio"] is False
    store.script_put(c, mid, "x", None, "m", 1)
    (tmp_path / f"tts-{mid}.webm.part").write_bytes(b"\x1a\x45\xdf\xa3")
    for fn in (lambda: store.tail(c, rooms=[r1]),
               lambda: store.thread(c, mid, [r1]),
               lambda: store.search(c, keywords=["terse"], rooms=[r1])):
        row = fn()[0]
        assert row["has_script"] is True and row["has_audio"] is True, fn
    os.rename(tmp_path / f"tts-{mid}.webm.part", tmp_path / f"tts-{mid}.webm")
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
    # Row click ignores the icons (they own their click), like .mid does. THE
    # ICON IS ON EVERY MESSAGE (operator directive 2026-08-17): filled = play,
    # hollow = generate then play; the voice toggle only rules the AUTOMATIC path.
    # The click always ASKS first (ruling 11483): ready -> play, else queued --
    # a terse stand-in heard earlier is not a file (11476).
    assert "if(e.target.closest('.play')){genOne(m);return;}" in UI
    assert "if(r.state==='ready'){m.gen_pending=false;m.has_audio=true;paintIcons(m);playOne(m.id);return;}" in UI
    assert "if(!m.terse)mm.has_audio=true;" in UI, "a terse frame keeps the icon hollow"
    assert "if(e.target.closest('.scr')){toggleScript(row,m);return;}" in UI
    assert "(m.has_audio?'&#9654;':'&#9655;')" in UI, "filled when made, hollow when not"


def test_generate_on_demand_posts_once_and_plays_on_the_audio_frame():
    """Operator directive 2026-08-17: a message with no audio is generated on
    the click (POST /audio/<id>, the same path family, ONE url builder) and the
    `audio` frame that follows PLAYS it for the one who asked -- and only for
    them (others' tabs queue it as they would any arrival). A second click while
    it is being made does nothing. The stop button shows only while something
    sounds, and stopping hands the queue on."""
    gen = UI[UI.index("async function genOne(m){"):]
    gen = gen[:gen.index("\n}\n")]
    assert "if(m.gen_pending)return;" in gen, "one ask per message while it is being made"
    assert "api(vBase(m.id)+qs(),{method:'POST'})" in gen
    assert "vAsked.add(m.id)" in gen and "vCtxUp()" in gen, "the click is the gesture"
    assert "if(r.state==='ready')" in gen and "playOne(m.id)" in gen, "already made: just play"
    case = UI[UI.index("case 'audio':"):]
    case = case[:case.index("\n")]
    assert "if(vAsked.delete(m.id))playOne(m.id);else vPush(mm);" in case, \
        "the asker plays it now; everyone else queues it as an arrival"
    assert "const vBase=id=>'/audio/'+encodeURIComponent(id);" in UI and \
        "const vUrl=id=>vBase(id)+'.webm'+qs();" in UI
    # The stop button (operator: a long message must be escapable).
    assert 'id="vstop" hidden' in UI
    assert "$('vstop').onclick=()=>{vStop();vDone();};" in UI
    assert "function paintStop(){const b=$('vstop');if(b)b.hidden=!vCtl;}" in UI
    assert "function vDone(){vCtl=null;vBusy=false;paintStop();if(earconPending)earconRing();vPump();}" in UI
    assert " const c=vCtl;vCtl=null;paintStop();" in UI


def test_a_continuation_row_carries_its_icons_too():
    """Operator, eval box: the second of two stacked messages had no play icon
    because icons lived in the head and a continuation row has none. Every row
    has an .arts holder now -- inside the head, or beside the body."""
    ui = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                           "index.html")).read()
    assert "+(cont?'<span class=\"arts\">'+artIcons(m)+'</span>':'')" in ui
    assert "'<span class=\"arts\">'+artIcons(m)+'</span>'\n    +'</div>')" in ui
    paint = ui[ui.index("function paintIcons(m){"):]
    paint = paint[:paint.index("\n}\n")]
    assert '.arts' in paint and ".head" not in paint
