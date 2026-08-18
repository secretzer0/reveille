"""DES-014 slice 1: the ear. A signed-in person's take goes to the STT upstream
and the words come back; nothing is stored, nothing is sent, one take at a time,
tokens have no microphone, and the config refusal is the one every upstream
wears. Driven in-process against a stub /v1/audio/transcriptions.
"""
import asyncio
import http.server
import io
import json
import os
import struct
import sys
import threading
import time
import wave

import pytest
from starlette.requests import Request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import daemon, store  # noqa: E402


def _take(seconds=2.0, rate=16000, silent=False):
    n = int(seconds * rate)
    amp = 0 if silent else 8000
    pcm = b"".join(struct.pack("<h", amp if (i // 20) % 2 else -amp) for i in range(n))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)  # noqa: E702
        w.writeframes(pcm)
    return buf.getvalue()


class _Stt(http.server.BaseHTTPRequestHandler):
    calls = []
    hold = None          # an Event: the handler waits on it before answering
    down = False

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        raw = self.rfile.read(n)
        _Stt.calls.append({"path": self.path, "ctype": self.headers.get("content-type", ""),
                           "raw": raw, "auth": self.headers.get("authorization", "")})
        if _Stt.hold is not None:
            _Stt.hold.wait(10)
        if _Stt.down:
            self.send_error(500)
            return
        body = json.dumps({"text": "  make it so  "}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


@pytest.fixture
def stt():
    _Stt.calls, _Stt.hold, _Stt.down = [], None, False
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stt)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}"
    srv.shutdown()


class _User:
    kind = "user"
    name = "travis"
    user_id = "u1"
    agent_id = ""


def _req(body, headers=None, query=b""):
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": "POST", "path": "/stt", "headers": hdrs,
             "query_string": query, "path_params": {}}

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request(scope, receive)


def _call(req):
    r = asyncio.run(daemon.stt_http(req))
    return r.status_code, json.loads(r.body)


@pytest.fixture
def ear(stt, monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "_user_principal", lambda request: _User())
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_stt_on", True)
    monkeypatch.setattr(daemon, "_stt_url", stt)
    monkeypatch.setattr(daemon, "_stt_token", "shh")
    monkeypatch.setattr(daemon, "_stt_model", "whisper-turbo")
    monkeypatch.setattr(daemon, "_stt_timeout", 5.0)
    return stt


def test_the_refusal_is_the_one_every_upstream_wears():
    R = daemon.stt_config_refusal
    assert R("", "") is None, "unset = the ear is off, the broker boots"
    assert R("http://127.0.0.1:8000", "") is None
    assert R("http://stt:8000", "") is None, "a compose name is on-host"
    why = R("http://192.168.90.50:8000", "")
    assert why and "REVEILLE_STT_URL" in why and "REVEILLE_LAN_PLAINTEXT" in why
    assert R("http://192.168.90.50:8000", "", lan_ok=True) is None
    assert R("https://stt.example.org", "") and "TOKEN" in R("https://stt.example.org", "")


def test_a_take_comes_back_as_words_and_nothing_is_stored(ear, tmp_path, caplog):
    before = sorted(p.name for p in tmp_path.iterdir())
    with caplog.at_level("INFO"):
        st, out = _call(_req(_take(2.0), {"content-type": "audio/wav"}, b"lang=en"))
    assert (st, out) == (200, {"text": "make it so"}), out
    # The upstream saw multipart with the file, the model and the language; the
    # token rode as a bearer.
    c = _Stt.calls[-1]
    assert c["path"] == "/v1/audio/transcriptions" and c["ctype"].startswith("multipart/form-data")
    assert b'name="file"; filename="take.wav"' in c["raw"] and b"RIFF" in c["raw"]
    assert b'name="model"\r\n\r\nwhisper-turbo' in c["raw"] and b'name="language"\r\n\r\nen' in c["raw"]
    assert c["auth"] == "Bearer shh"
    # NOTHING STORED, NOTHING LOGGED BUT THE LENGTH.
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    assert "make it so" not in caplog.text and "ear:" in caplog.text
    assert "10 chars" in caplog.text


def test_tokens_have_no_microphone(ear):
    st, out = _call(_req(_take(), {"content-type": "audio/wav", "authorization": "Bearer abc"}))
    assert st == 401 and "signed-in person" in out["detail"]
    assert _Stt.calls == []


def test_the_bounds_are_refused_by_name_before_the_wire(ear, monkeypatch):
    assert _call(_req(b"ID3 not a wav"))[1]["error"].startswith("not a PCM WAV")
    assert "too long" in _call(_req(_take(61.0, rate=8000)))[1]["error"]
    assert "silent" in _call(_req(_take(2.0, silent=True)))[1]["error"]
    monkeypatch.setattr(daemon, "STT_TAKE_MAX", 100)
    assert "too large" in _call(_req(_take(2.0)))[1]["error"]
    assert _Stt.calls == [], "a refused take never reaches the upstream"
    monkeypatch.setattr(daemon, "_stt_on", False)
    assert _call(_req(_take())) == (503, {"error": "the ear is off on this broker"})


def test_one_take_at_a_time_and_the_slot_comes_back(ear):
    _Stt.hold = threading.Event()
    results = {}

    def first():
        results["first"] = _call(_req(_take()))
    t = threading.Thread(target=first)
    t.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and not _Stt.calls:
        time.sleep(0.02)
    assert _Stt.calls, "the first take reached the upstream"
    st, out = _call(_req(_take()))
    assert st == 429 and "one utterance at a time" in out["error"]
    _Stt.hold.set()
    t.join(5)
    assert results["first"][0] == 200
    assert _call(_req(_take()))[0] == 200, "the slot is released after the first answers"


def test_an_upstream_that_is_down_is_a_502_that_names_it_and_frees_the_slot(ear):
    _Stt.down = True
    st, out = _call(_req(_take()))
    assert st == 502 and "did not answer" in out["error"]
    _Stt.down = False
    assert _call(_req(_take()))[0] == 200


def test_me_says_whether_the_ear_is_on(monkeypatch, tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    store.setup_first_admin(c, "travis", "hunter2hunter2")
    monkeypatch.setattr(daemon, "_conn", c)
    monkeypatch.setattr(daemon, "_user_principal",
                        lambda request: daemon.Principal(kind="user", name="travis", user_id="u"))
    for on in (False, True):
        monkeypatch.setattr(daemon, "_stt_on", on)
        r = asyncio.run(daemon.me_http(Request({"type": "http", "method": "GET", "path": "/me",
                                                 "headers": [], "query_string": b"",
                                                 "path_params": {}})))
        assert json.loads(r.body)["ear"] is on


UI = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                       "index.html")).read()


def test_the_page_has_one_mic_that_lands_words_in_the_box_and_never_sends():
    """DES-014 section 4: hidden until the ear is on; hold on a pointer, tap
    to start/stop on touch; the ONE recorder; POST /stt as audio/wav; the words
    APPEND to the textarea, caret at the end; the handler never touches send."""
    assert '<button type="button" id="mic" hidden aria-pressed="false"' in UI
    assert "if($('mic'))$('mic').hidden=!(me&&me.ear);" in UI, "the mic exists only where the ear does"
    ear = UI[UI.index("let earBusy=false;"):UI.index("async function openVoices(){")]
    assert "await vRecStart();" in ear and "const r=vRecStop();" in ear, "the shared recorder"
    assert UI.count("function vRecStart(){") == 1 and UI.count("function vRecStop(){") == 1
    assert "if(r.silent){$('micState').textContent='';toast(REC_SILENT_MSG);return;}" in ear, \
        "a silent take is refused before the wire"
    assert "fetch('/stt'+qs(),{method:'POST',credentials:'same-origin'," in ear and \
        "headers:{'content-type':'audio/wav'},body:take" in ear
    # 16 kHz to the ear (ruling 11333): the browser's own resampler, before the wire.
    assert "take=await earResample(r.blob,rate);" in ear
    assert "new OfflineAudioContext(1,Math.ceil(n*16000/rate),16000)" in ear and "return wavBlob(pcm,16000);" in ear
    assert "earLand(d.text||'');" in ear
    land = ear[ear.index("function earLand(text){"):]
    land = land[:land.index("\n}\n")]
    assert "const b=$('body');" in land and "b.value=had+" in land and \
        "b.setSelectionRange(b.value.length,b.value.length);" in land
    for forbidden in ("send(", "$('send')", "sendMsg", "requestSubmit", ".submit("):
        assert forbidden not in ear, f"the ear must never send ({forbidden})"
    # Pointer: hold; touch: toggle; keyboard: space/enter toggles.
    assert "if(e.pointerType==='touch'){touchToggle=true;return;}" in ear
    assert "m.setPointerCapture(e.pointerId);earStart();" in ear
    assert "if(touchToggle){if(vRec)earStop();else earStart();return;}" in ear
    assert "if(sec>=60){earStop();return;}" in ear, "the ear takes 60 s at a time"
    assert "try{vCtxUp();}catch(e){}" in ear, "the mic gesture doubles as the audio unlock"
