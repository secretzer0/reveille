"""DES-014 slice 1: the ear. A signed-in person's take goes to the STT upstream
and the words come back; nothing is stored, nothing is sent, one take at a time,
tokens have no microphone, and the config refusal is the one every upstream
wears. Driven in-process against a stub /v1/audio/transcriptions.
"""
import asyncio
import pathlib
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
    """DES-014 section 4 + slice 2 (ruling 11355): hidden until the ear is on;
    the mic is a LISTENING TOGGLE over a page-side VAD; every take goes through
    the ONE POST /stt as audio/wav; the words APPEND to the textarea, caret at
    the end; the handler never touches send."""
    assert '<button type="button" id="mic" hidden aria-pressed="false"' in UI
    assert "if($('mic'))$('mic').hidden=!(me&&me.ear);" in UI, "the mic exists only where the ear does"
    ear = UI[UI.index("let talkBusy=false;"):UI.index("async function openVoices(){")]
    # PUSH TO TALK (slice 1, kept -- operator 11364): the shared recorder, hold on
    # a pointer, tap-toggle on touch, 60 s at a time, silence refused before the wire.
    assert "await vRecStart();" in ear and "const r=vRecStop();" in ear, "the shared recorder"
    assert UI.count("function vRecStart(){") == 1 and UI.count("function vRecStop(){") == 1
    assert "if(r.silent){$('micState').textContent='';toast(REC_SILENT_MSG);return;}" in ear
    assert "if(e.pointerType==='touch'){touchToggle=true;return;}" in ear
    assert "m.setPointerCapture(e.pointerId);talkStart();" in ear
    assert "if(sec>=60){talkStop();return;}" in ear, "the ear takes 60 s at a time"
    assert "take=await talkResample(r.blob,rate);" in ear and "return wavBlob(pcm,16000);" in ear
    # BOTH controls post to the ONE route as audio/wav and land through the ONE earLand.
    assert ear.count("fetch('/stt'+qs(),{method:'POST',credentials:'same-origin',") == 2 and \
        ear.count("headers:{'content-type':'audio/wav'},body:take") == 2
    assert "const take=wavBlob(pcm,16000);" in ear, "the VAD hands 16 kHz float; the wire is the same 16 kHz WAV"
    assert ear.count("earHeard(d.text||'')") == 2 and UI.count("function earLand(text){") == 1, \
        "every take lands through earHeard: a command runs, anything else is text"
    assert "if(typeof listenStop==='function')listenStop();" in ear and "if(listenVad||vRec)return;" in ear, \
        "one ear at a time"
    land = ear[ear.index("function earLand(text){"):]
    land = land[:land.index("\n}\n")]
    assert "const b=$('body');" in land and "b.value=had+" in land and \
        "b.setSelectionRange(b.value.length,b.value.length);" in land
    # THE ONE WAY THE EAR EVER SENDS is the spoken "send" inside earRun (slice 4,
    # 11355 s5) -- the human said the word. Nowhere else in the ear.
    run = ear[ear.index("function earRun(c){"):ear.index("function earHeard(text){")]
    rest = ear.replace(run, "")
    for forbidden in ("send(", "$('send')", "sendMsg", "requestSubmit", ".submit("):
        assert forbidden not in rest, f"the ear must never send outside earRun ({forbidden})"
    assert run.count("requestSubmit") == 1 and "if(!$('body').value.trim()){toast('nothing to send');return;}" in run
    assert ear.count("try{vCtxUp();}catch(e){}") == 2, "either mic gesture doubles as the audio unlock"


def test_hands_free_is_a_deliberate_visible_state_over_the_same_route():
    """Ruling 11355, binding for slice 2: toggle per tab, never persisted; audio
    leaves the page only for a VAD-closed take; silence closes a take at 3 s;
    a hard cap of 30 s closes-and-reopens; hidden tab / page gone / mic error =
    OFF; the model ships with the page (no CDN)."""
    ear = UI[UI.index("const EAR_SILENCE_MS="):UI.index("async function openVoices(){")]
    assert '<button type="button" id="listen" hidden aria-pressed="false"' in UI
    assert "if($('listen'))$('listen').hidden=!(me&&me.ear);" in UI, "the toggle exists only where the ear does"
    assert "const EAR_SILENCE_MS=3000;" in ear and "redemptionMs:EAR_SILENCE_MS" in ear
    assert "const EAR_TAKE_CAP_MS=30000;" in ear and "setTimeout(listenCap,EAR_TAKE_CAP_MS)" in ear
    assert "submitUserSpeechOnPause:true," in ear and "await v.pause();if(listenVad===v)await v.start();" in ear, \
        "the cap closes the open take (pause submits it) and reopens"
    assert "onSpeechEnd:audio=>{clearTimeout(listenCapTimer);listenPaint('listening');listenTake(audio);}" in ear, \
        "only a VAD-closed take becomes a request"
    assert "m.addEventListener('click',()=>{if(listenVad)listenStop();else listenStart();});" in ear
    assert "document.addEventListener('visibilitychange',()=>{if(document.hidden)listenStop();});" in ear
    assert "window.addEventListener('pagehide',listenStop);" in ear
    assert "toast('microphone: '+(e.message||e));return;}" in ear, "a mic error leaves listening OFF"
    assert "m.classList.toggle('on',!!listenVad);m.setAttribute('aria-pressed',listenVad?'true':'false');" in ear
    listen = ear[:ear.index("const EAR_AUTOSEND_MS=")]
    for persisted in ("localStorage", "sessionStorage", "document.cookie"):
        assert persisted not in listen, "listening is per tab, never persisted"
    assert "baseAssetPath:'/ui/vad/',onnxWASMBasePath:'/ui/vad/'" in ear and "model:'v5'" in ear
    assert "cdn." not in UI.lower() and "jsdelivr" not in UI, "the model ships with the page"
    assert '<script src="/ui/vad/ort.wasm.min.js"></script>' in UI and \
        '<script src="/ui/vad/vad.bundle.min.js"></script>' in UI
    assert "let listenChain=Promise.resolve();" in ear and "listenChain=listenChain.then(async()=>{" in ear, \
        "takes post one at a time -- the broker holds one slot"


def test_the_vad_ships_with_the_page_from_a_table(tmp_path):
    """/ui/vad/<name>: six files by table, right media types, an unknown name is
    a 404 and never a path; the sums file pins what was vendored."""
    from starlette.requests import Request
    def get(name):
        req = Request({"type": "http", "method": "GET", "path": f"/ui/vad/{name}", "headers": [],
                       "query_string": b"", "path_params": {"name": name}})
        return asyncio.run(daemon.vad_asset_http(req))
    for name, media in daemon._VAD_FILES.items():
        r = get(name)
        assert r.status_code == 200 and r.media_type == media and len(r.body) > 1000, name
        assert r.headers["cache-control"] == "public, max-age=86400"
    assert set(daemon._VAD_FILES) == {"vad.bundle.min.js", "vad.worklet.bundle.min.js", "ort.wasm.min.js",
                                      "ort-wasm-simd-threaded.mjs", "ort-wasm-simd-threaded.wasm",
                                      "silero_vad_v5.onnx"}
    assert get("../index.html").status_code == 404 and get("SHA256SUMS").status_code == 404
    sums = pathlib.Path(daemon._UI_PACKAGED, "vad", "SHA256SUMS").read_text()
    for name in daemon._VAD_FILES:
        assert name in sums


def test_voice_commands_are_a_fixed_grammar_on_the_whole_final_transcript():
    """DES-014 slice 4 (pre-ruled 11355 s5): the table, exact words, case-folded,
    trailing punctuation dropped; a command take is consumed, not appended;
    near-misses are text; "send" on an empty box is a no-op; a dead mic ends a
    take (architect 11374)."""
    ear = UI[UI.index("const EAR_COMMANDS="):UI.index("async function openVoices(){")]
    assert "const EAR_COMMANDS=['send','cancel','stop','reply','voice on','voice off'];" in ear
    assert "const m=/^room (.+)$/.exec(t);" in ear and "return {cmd:'room',arg:m[1]};" in ear
    assert ".toLowerCase().replace(/[\\s.!?,;:]+$/,'')" in ear, "case-folded, trailing punctuation dropped"
    assert "if(EAR_COMMANDS.includes(t))return {cmd:t};" in ear and "return null;" in ear, "exact words, nothing fuzzy"
    heard = ear[ear.index("function earHeard(text){"):ear.index("function earLand(text){")]
    assert "if(c){earRun(c);return false;}" in heard and "return earLand(text);" in heard, \
        "a command is consumed, text lands (and says so, for the pause-to-send window)"
    run = ear[ear.index("function earRun(c){"):ear.index("function earHeard(text){")]
    for row in ("case 'send':", "case 'cancel':", "case 'stop':{vStop();vDone();return;}", "case 'reply':",
                "case 'room':", "case 'voice on':{if(!voiceOn)toggleVoice();return;}",
                "case 'voice off':{if(voiceOn)toggleVoice();return;}"):
        assert row in run, row
    assert "toast('nothing to reply to')" in run and "toast('no room named" in run
    assert "return mm&&mm.from!==myName;" in run and "selectRow(row,mm)" in run, \
        "reply = the newest message here that is not mine, through the click handler's own path"
    assert "pickRoom(r.id)" in run and "(x.name||'').toLowerCase()===c.arg" in run, "room by exact name"
    # A dead mic ends the take / turns listening off.
    whole = UI[UI.index("let talkBusy=false;"):UI.index("async function openVoices(){")]
    assert "for(const t of st.getAudioTracks())t.addEventListener('ended',listenStop);return st;" in whole
    assert "for(const t of vRec.stream.getAudioTracks())t.addEventListener('ended',()=>talkStop());" in whole


def test_pause_to_send_is_a_deliberate_setting_hands_free_only_with_a_visible_countdown():
    """Operator 11389 (A+B), numbers ruled 11385: hands-free ONLY; off by default;
    a localStorage setting beside listen (unlike the per-tab toggle); counted
    from the moment the words LANDED; a spoken cancel or any keystroke aborts;
    the countdown is shown; the send is earRun's "send" -- still the one way;
    push-to-talk never auto-sends."""
    ear = UI[UI.index("let talkBusy=false;"):UI.index("async function openVoices(){")]
    b = ear[ear.index("const EAR_AUTOSEND_MS="):ear.index("// ---- VOICE COMMANDS")]
    assert "const EAR_AUTOSEND_MS=5000;" in b
    assert "function pauseSendOn(){return !!localStorage.revAutosend;}" in b, "a persisted setting, off by default"
    assert '<label id="autosendWrap" class="khint" hidden' in UI and '<input type="checkbox" id="autosend">' in UI
    assert "if($('autosendWrap'))$('autosendWrap').hidden=!(me&&me.ear);" in UI
    # Armed ONLY from the hands-free take path, only when text landed, only while listening.
    assert ear.count("pauseSendArm()") == 2 and \
        "if(earHeard(d.text||'')&&listenVad&&pauseSendOn())pauseSendArm();" in ear
    talk = ear[:ear.index("const EAR_SILENCE_MS=")]
    assert "autosend" not in talk, "push-to-talk never auto-sends"
    # Counted from landing, shown, aborted by cancel / keystroke / listening off / empty box.
    assert "listenPaint('sending in '+left+'...')" in b
    assert "if(!listenVad||!$('body').value.trim()){pauseSendAbort();return;}" in b
    assert "$('body').addEventListener('keydown',()=>pauseSendAbort());" in b
    run = ear[ear.index("function earRun(c){"):ear.index("function earHeard(text){")]
    assert "if(typeof pauseSendAbort==='function')pauseSendAbort();" in run, "a spoken cancel aborts"
    assert "if(typeof pauseSendAbort==='function')pauseSendAbort(true);" in ear, "listening off aborts"
    # The send is still the one send.
    assert "earRun({cmd:'send'});" in b and run.count("requestSubmit") == 1 and b.count("requestSubmit") == 0

