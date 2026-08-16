"""DES-013 slice 3b (rulings 11104/11106): the bank travels by PUSH, one path on
one box or two.

Bounds under test: a bank clip goes to the synthesizer over ITS API (upstream
POST /upload_reference, multipart) under the versioned name
bank-<id>-<updated_ns>.wav; a replace is a NEW name; the worker reconciles at
start (list theirs, push what the bank has and they lack); an assigned clip
missing from the listing triggers a reconcile and the utterance clones the
pushed name; a synthesizer that refuses the push leaves the message spoken with
the digest pick, never stalled; compose no longer bind-mounts the broker's
voices dir into the synthesizer -- its reference dir is its own volume.

Measured on tts-vet before writing (11115): arbitrary sanitized filename
accepted, duplicate is a no-op 200, /tts clones by the pushed name.

Proven RED on main @ 1272ca1: clip_name / _tts_push / _tts_reconcile do not
exist and compose still mounts TTS_VOICES_DIR :ro.
"""
import asyncio
import http.server
import io
import pathlib
import json
import os
import re
import sys
import threading
import wave

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")


def wav(seconds=6.0, rate=24000):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)  # noqa: E702
        w.writeframes(b"\x00" * int(seconds * rate) * 2)
    return buf.getvalue()


class _Synth(http.server.BaseHTTPRequestHandler):
    """Upstream's shape: /get_reference_files lists, /upload_reference takes
    multipart `files` and skips duplicates, /tts clones by name."""
    files = []          # what the synthesizer holds
    uploads = []        # every name it was asked to store, in order
    tts = []            # every /tts body
    refuse_upload = False

    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/get_reference_files":
            return self._json(list(_Synth.files))
        if self.path == "/v1/audio/voices":
            return self._json({"voices": ["A.wav", "B.wav"]})
        if self.path == "/api/model-info":
            return self._json({"device": "stub", "loaded": True})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        if self.path == "/upload_reference":
            if _Synth.refuse_upload:
                return self._json({"error": "nope"}, 500)
            ct = self.headers.get("content-type", "")
            assert ct.startswith("multipart/form-data; boundary="), ct
            names = re.findall(rb'filename="([^"]+)"', raw)
            assert names, "multipart with a filename"
            name = names[0].decode()
            _Synth.uploads.append(name)
            assert b"\r\n\r\nRIFF" in raw, "the wav bytes follow the part header"
            if name not in _Synth.files:
                _Synth.files.append(name)
            return self._json({"uploaded_files": [name], "all_reference_files": list(_Synth.files),
                               "errors": []})
        if self.path == "/tts":
            _Synth.tts.append(json.loads(raw))
            self.send_response(200)
            self.send_header("content-type", "audio/wav")
            self.send_header("content-length", "8")
            self.end_headers()
            self.wfile.write(b"RIFFstub")
            return
        self.send_error(404)


@pytest.fixture
def synth():
    _Synth.files, _Synth.uploads, _Synth.tts, _Synth.refuse_upload = ["Gianna.wav"], [], [], False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Synth)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def broker(tmp_path, monkeypatch, synth):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    vd = tmp_path / "voices"
    vd.mkdir()
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_db_path", path)
    monkeypatch.setattr(daemon, "_worker_local", threading.local())
    monkeypatch.setattr(daemon, "_voices_dir", vd)
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_on", True)
    monkeypatch.setattr(daemon, "_tts_url", synth)
    monkeypatch.setattr(daemon, "_tts_token", "")
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: None)

    class P:
        kind, name, user_id, is_admin, rooms = "user", "travis", admin["id"], True, {}
    monkeypatch.setattr(daemon, "_principal", lambda request: P())
    return dict(c=c, vd=vd, admin=admin, url=synth)


def _put_req(vid, data, name=""):
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": data, "more_body": False}
    req = Request({"type": "http", "method": "PUT", "path": f"/voices/{vid}/clip",
                   "headers": [(b"content-type", b"application/octet-stream"),
                               (b"content-length", str(len(data)).encode())],
                   "query_string": f"name={name}".encode(), "path_params": {"vid": vid}}, receive)
    return req


def _put_clip(vid, data, name=""):
    r = asyncio.run(daemon.voice_clip_http(_put_req(vid, data, name)))
    return r.status_code, json.loads(r.body)


def test_a_clip_put_is_pushed_under_its_versioned_name_and_a_replace_is_a_new_name(broker):
    st, row = _put_clip("quark", wav(6))
    assert st == 200 and row["pushed"] is True
    n1 = daemon.clip_name(row)
    assert n1 == f"bank-quark-{row['updated_ns']}.wav" and _Synth.uploads == [n1]
    assert n1 in _Synth.files
    st, row2 = _put_clip("quark", wav(7))
    n2 = daemon.clip_name(row2)
    assert row2["updated_ns"] > row["updated_ns"] and n2 != n1
    assert _Synth.uploads == [n1, n2] and n1 in _Synth.files and n2 in _Synth.files
    # Local: still ONE file, replaced in place -- the versioned name is the wire name.
    assert sorted(p.name for p in broker["vd"].iterdir()) == ["bank-quark.wav"]


def test_the_worker_reconciles_at_start_and_the_utterance_clones_the_pushed_name(broker):
    # A bank voice that exists in the broker (row + local clip) but the synthesizer
    # never saw -- a fresh synthesizer host, or a broker that came up before it.
    daemon._tts_on = False                     # no push at PUT time: simulate "they never got it"
    st, row = _put_clip("quark", wav(6))
    daemon._tts_on = True
    name = daemon.clip_name(row)
    assert _Synth.uploads == [] and name not in _Synth.files
    daemon._tts_q.put((5, "r1", "picard", "hello", name))
    daemon._tts_q.put(None)
    daemon._tts_worker(broker["url"], "", 5)
    assert _Synth.uploads == [name], "reconciled at worker start"
    assert _Synth.tts[-1]["voice_mode"] == "clone"
    assert _Synth.tts[-1]["reference_audio_filename"] == name


def test_an_assigned_clip_missing_from_the_listing_triggers_a_push_then_clones_it(broker):
    daemon._tts_on = False
    st, row = _put_clip("quark", wav(6))
    daemon._tts_on = True
    name = daemon.clip_name(row)
    # No worker start here: the trigger is the utterance itself.
    chunks = daemon._tts_speak(broker["url"], "", "picard", "hello", 5, assigned=name)
    assert b"".join(chunks) == b"RIFFstub"
    assert _Synth.uploads == [name]
    assert _Synth.tts[-1] == {"text": "hello", "output_format": "wav", "split_text": True,
                              "stream": True, "voice_mode": "clone",
                              "reference_audio_filename": name}


def test_a_synthesizer_that_refuses_the_push_leaves_the_message_spoken_not_stalled(broker, caplog):
    daemon._tts_on = False
    st, row = _put_clip("quark", wav(6))
    daemon._tts_on = True
    name = daemon.clip_name(row)
    _Synth.refuse_upload = True
    with caplog.at_level("WARNING"):
        chunks = daemon._tts_speak(broker["url"], "", "picard", "hello", 5, assigned=name)
    assert b"".join(chunks) == b"RIFFstub"
    assert _Synth.tts[-1]["voice_mode"] == "predefined", "digest pick, not silence"
    assert "push of " in caplog.text and "not visible" in caplog.text
    # And a PUT while the synthesizer refuses still lands locally, reporting pushed=False.
    st, row = _put_clip("rom", wav(6))
    assert st == 200 and row["pushed"] is False
    assert (broker["vd"] / "bank-rom.wav").exists()


def test_compose_no_longer_mounts_the_brokers_voices_dir_into_the_synthesizer():
    compose = open(os.path.join(ROOT, "docker", "compose.yml")).read()
    assert "TTS_VOICES_DIR" not in compose, "one path: the clip travels by push, never by mount"
    assert "/app/reference_audio" in compose, "the synthesizer's reference dir is its own volume"
    tts = compose[compose.index("reveille-tts:"):]
    assert re.search(r"-\s+tts-reference:/app/reference_audio\b", tts), tts[:400]


def test_a_blackholed_synthesizer_costs_the_put_its_timeout_and_nobody_else_anything(broker, monkeypatch):
    """Verdict 11119 BLOCKING 1: the push is blocking urllib; on the event loop
    a synthesizer that accepts and never answers would stall every request for
    the timeout. Off the loop (asyncio.to_thread), the PUT alone waits and
    returns pushed:false; another route answers meanwhile."""
    import socket
    hole = socket.socket()
    hole.bind(("127.0.0.1", 0))
    hole.listen(1)                       # accepts, never reads, never answers
    monkeypatch.setattr(daemon, "_tts_url", f"http://127.0.0.1:{hole.getsockname()[1]}")
    monkeypatch.setattr(daemon, "VOICE_PUSH_TIMEOUT", 1.0)
    order = []

    async def go():
        # The PUT (slow) and a listing (fast) start together on ONE loop.
        async def slow():
            r = await daemon.voice_clip_http(_put_req("quark", wav(6)))
            order.append("put")
            return json.loads(r.body)

        async def fast():
            await asyncio.sleep(0.05)
            r = await daemon.voices_http(Request({"type": "http", "method": "GET", "path": "/voices",
                                                  "headers": [], "query_string": b"",
                                                  "path_params": {}}))
            order.append("list")
            return r.status_code
        return await asyncio.gather(slow(), fast())
    row, st = asyncio.run(go())
    hole.close()
    assert st == 200 and order == ["list", "put"], order
    assert row["pushed"] is False
    assert (broker["vd"] / "bank-quark.wav").exists()


def test_the_default_meets_a_bank_voice_named_like_the_speaker(broker):
    """Ruling 11119 (a0): an agent named quark and a bank voice uploaded as quark
    meet without a click -- before the elsewhere/first-free steps."""
    c = broker["c"]
    for vid in ("mr-scott", "picard", "quark"):
        store.voice_put(c, vid, name=vid, uploaded_by=broker["admin"]["id"], seconds=6, nbytes=1)
    room = store.create_room(c, broker["admin"]["id"], "lab")
    a = store.mint_agent(c, broker["admin"]["id"], "quark")
    assert store.voice_for(c, room["id"], f"agent:{a['id']}") == "quark"
    b = store.mint_agent(c, broker["admin"]["id"], "worf")   # no such voice: first free
    assert store.voice_for(c, room["id"], f"agent:{b['id']}") == "mr-scott"
    D = store.voice_default
    assert D(elsewhere=[("picard", "default")], taken=set(), bank=["picard", "quark"], name="quark") == "quark"
    assert D(elsewhere=[("picard", "default")], taken={"quark"}, bank=["picard", "quark"], name="quark") == "picard"


def _say(vid, text):
    req = Request({"type": "http", "method": "GET", "path": f"/voices/{vid}/say",
                   "headers": [], "query_string": ("text=" + text).encode(),
                   "path_params": {"vid": vid}})
    return asyncio.run(daemon.voice_say_http(req))


async def _drain(resp):
    out = b""
    async for b in resp.body_iterator:
        out += b
    return out


def test_the_audition_speaks_a_line_in_that_bank_voice_and_keeps_nothing(broker):
    st, row = _put_clip("quark", wav(6))
    assert st == 200
    _Synth.tts.clear()
    r = _say("quark", "Rule%20of%20Acquisition%20number%20one.")
    assert r.status_code == 200 and r.media_type == "audio/wav"
    assert asyncio.run(_drain(r)) == b"RIFFstub"
    assert len(_Synth.tts) == 1
    assert _Synth.tts[0]["text"] == "Rule of Acquisition number one."
    assert _Synth.tts[0]["reference_audio_filename"] == daemon.clip_name(row)
    # NOTHING KEPT: no tts-*.wav in files, no scripts row, no message.
    assert not [p for p in daemon._files_dir.iterdir() if p.name.startswith("tts-")]
    assert broker["c"].execute("SELECT count(*) FROM messages").fetchone()[0] == 0
    # Refusals are named: no such voice, empty line, too long, voices off.
    assert _say("nobody", "hi").status_code == 404
    assert _say("quark", "%20%20").status_code == 400
    assert _say("quark", "x" * (daemon.VOICE_SAY_MAX + 1)).status_code == 400
    daemon._tts_on = False
    assert _say("quark", "hi").status_code == 503


def test_the_original_clip_plays_back_as_it_was_uploaded(broker):
    data = wav(6)
    assert _put_clip("quark", data)[0] == 200
    req = Request({"type": "http", "method": "GET", "path": "/voices/quark/clip",
                   "headers": [], "query_string": b"", "path_params": {"vid": "quark"}})
    r = asyncio.run(daemon.voice_clip_get_http(req))
    assert r.status_code == 200 and r.media_type == "audio/wav"
    assert pathlib.Path(r.path).read_bytes() == data
    req = Request({"type": "http", "method": "GET", "path": "/voices/nobody/clip",
                   "headers": [], "query_string": b"", "path_params": {"vid": "nobody"}})
    assert asyncio.run(daemon.voice_clip_get_http(req)).status_code == 404


def test_the_audition_is_the_right_voice_or_none_and_one_at_a_time(broker):
    st, row = _put_clip("quark", wav(6))
    assert st == 200
    # The clip vanishes from the synthesizer and it refuses the re-push: 409,
    # never the digest voice (verdict 11144), and no /tts call is made.
    _Synth.files.remove(daemon.clip_name(row))
    _Synth.refuse_upload = True
    _Synth.tts.clear()
    r = _say("quark", "hello")
    assert r.status_code == 409 and json.loads(r.body)["error"] == "clip not on the synthesizer yet"
    assert _Synth.tts == []
    assert daemon._say_slot.acquire(blocking=False), "a refusal returns the slot"
    daemon._say_slot.release()
    # The re-push succeeds when the synthesizer allows it: reconciled, then spoken.
    _Synth.refuse_upload = False
    r = _say("quark", "hello")
    assert r.status_code == 200
    # ONE AT A TIME: while a stream is open the next caller is told to wait.
    assert not daemon._say_slot.acquire(blocking=False), "the open stream holds the slot"
    assert _say("quark", "again").status_code == 429
    asyncio.run(_drain(r))
    assert daemon._say_slot.acquire(blocking=False), "draining the stream returns the slot"
    daemon._say_slot.release()
    assert len(_Synth.tts) == 1
