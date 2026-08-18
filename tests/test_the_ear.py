"""DES-014 slice 1: the ear. A signed-in person's take goes to the STT upstream
and the words come back; nothing is stored, nothing is sent, one take at a time,
tokens have no microphone, and the config refusal is the one every upstream
wears. Driven in-process against a stub /v1/audio/transcriptions.
"""
import asyncio
import pathlib
import shutil
import subprocess
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
    text = "  make it so  "
    segments = [{"avg_logprob": -0.21, "no_speech_prob": 0.02}]

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
        # A verbose_json answer, as speaches gives it: text + segments with the numbers.
        body = json.dumps({"text": _Stt.text, "segments": _Stt.segments}).encode()
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
    _Stt.text, _Stt.segments = "  make it so  ", [{"avg_logprob": -0.21, "no_speech_prob": 0.02}]
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
    assert (st, out) == (200, {"text": "make it so", "compression_ratio": 0.556,
                               "no_speech_prob": 0.02, "avg_logprob": -0.21}), out
    # The upstream saw multipart with the file, the model and the language; the
    # token rode as a bearer.
    c = _Stt.calls[-1]
    assert c["path"] == "/v1/audio/transcriptions" and c["ctype"].startswith("multipart/form-data")
    assert b'name="file"; filename="take.wav"' in c["raw"] and b"RIFF" in c["raw"]
    assert b'name="model"\r\n\r\nwhisper-turbo' in c["raw"] and b'name="language"\r\n\r\nen' in c["raw"]
    assert b'name="response_format"\r\n\r\nverbose_json' in c["raw"], "the segments' numbers ride back (11572)"
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
    assert "const take=wavBlob(pcm,rate||16000);" in ear, \
        "the VAD hands 16 kHz float; the fallback hands the device's rate, said in the header"
    assert ear.count("earHeard(d)") - ear.count("function earHeard(d)") == 2 and \
        UI.count("function earLand(text){") == 1, \
        "every take lands through earHeard: a command runs, anything else is text"
    assert "if(typeof listenStop==='function')listenStop();" in ear and \
        "if(listenVad||listenSimple||vRec)return;" in ear, "one ear at a time"
    land = ear[ear.index("function earLand(text){"):]
    land = land[:land.index("\n}\n")]
    assert "const b=$('body');" in land and "b.value=had+" in land and \
        "b.setSelectionRange(b.value.length,b.value.length);" in land
    # THE ONE WAY THE EAR EVER SENDS is the spoken "send" inside earRun (slice 4,
    # 11355 s5) -- the human said the word. Nowhere else in the ear.
    run = ear[ear.index("function earRun(c){"):ear.index("const EAR_RATIO_MAX=2.4")]
    rest = ear.replace(run, "")
    for forbidden in ("send(", "$('send')", "sendMsg", "requestSubmit", ".submit("):
        assert forbidden not in rest, f"the ear must never send outside earRun ({forbidden})"
    assert run.count("requestSubmit") == 1 and "if(!$('body').value.trim()){toast('nothing to send');return;}" in run
    assert ear.count("try{vCtxUp();}catch(e){}") == 1 and \
        ear.count("try{vCtxUp();earconLoad();}catch(e){}") == 1, "either mic gesture doubles as the audio unlock"


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
    assert "m.addEventListener('click',()=>{if(listenVad||listenSimple)listenStop();else listenStart();});" in ear
    assert "document.addEventListener('visibilitychange',()=>{if(document.hidden)listenStop();});" in ear
    assert "window.addEventListener('pagehide',listenStop);" in ear
    assert "listenPaint('');toast(micWhy(e));return;}" in ear, "a mic error leaves listening OFF"
    # 11559 (iPhone/Chrome): a NotAllowedError is the phone's answer, not the page's --
    # the toast names where to allow the microphone instead of quoting WebKit.
    assert "function micWhy(e){" in UI and "e.name==='NotAllowedError'" in UI and \
        "Settings > (Safari or Chrome) > Microphone" in UI
    assert "catch(e){toast(micWhy(e));return;}" in UI, "talk says the same"
    assert "const on=!!(listenVad||listenSimple);" in ear and \
        "m.classList.toggle('on',on);m.setAttribute('aria-pressed',on?'true':'false');" in ear
    listen = ear[:ear.index("const EARCON_GAIN=")]        # the listen toggle's code; the sounds SETTING after it is per browser by ruling (11577)
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
    heard = ear[ear.index("const EAR_RATIO_MAX=2.4"):ear.index("function earLand(text){")]
    assert "if(c){earRun(c);return false;}" in heard and "return earLand(text);" in heard, \
        "a command is consumed, text lands (and says so, for the pause-to-send window)"
    run = ear[ear.index("function earRun(c){"):ear.index("const EAR_RATIO_MAX=2.4")]
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
        "if(earHeard(d)){earconRing();if(listenVad&&pauseSendOn())pauseSendArm();}" in ear
    talk = ear[:ear.index("const EAR_SILENCE_MS=")]
    assert "autosend" not in talk, "push-to-talk never auto-sends"
    # Counted from landing, shown, aborted by cancel / keystroke / listening off / empty box.
    assert "listenPaint('sending in '+left+'...')" in b
    assert "if(!listenVad||!$('body').value.trim()){pauseSendAbort();return;}" in b
    assert "$('body').addEventListener('keydown',()=>pauseSendAbort());" in b
    run = ear[ear.index("function earRun(c){"):ear.index("const EAR_RATIO_MAX=2.4")]
    assert "if(typeof pauseSendAbort==='function')pauseSendAbort();" in run, "a spoken cancel aborts"
    assert "if(typeof pauseSendAbort==='function')pauseSendAbort(true);" in ear, "listening off aborts"
    # Speech RESUMING inside the window aborts it (architect 11392): sentence two
    # started at second three must not see sentence one leave at second five; the
    # next landing re-arms.
    assert "onSpeechStart:()=>{listenPaint('hearing you...');\n    if(typeof pauseSendAbort==='function')pauseSendAbort(true);" in ear
    # The send is still the one send.
    assert "earRun({cmd:'send'});" in b and run.count("requestSubmit") == 1 and b.count("requestSubmit") == 0



def test_the_earcon_rings_once_when_words_land_in_listen_mode_only():
    """0.2.134 (ruling 11465): one bell at landing, from the listen chain only;
    never over an utterance (queued until vDone); no bell in push-to-talk;
    the WAV ships with the page at /ui/earcon.wav. Since 0.2.146 the bell is the
    "ding" of the vocabulary (11577); the same rules hold through earcon()."""
    page = daemon._ui_read("index.html")
    assert "if(earHeard(d)){earconRing();if(listenVad&&pauseSendOn())pauseSendArm();}" in page
    assert page.count("earconRing()") == 2, "the listen chain, and the definition"
    talk = page[page.index("function talkStop"):page.index("function talkStop") + 3000]
    assert "earconRing" not in talk, "no bell in push-to-talk"
    assert "function earconRing(){if(listenVad)earcon('ding');}" in page
    assert " if(vBusy){earconQ.push(name);return;}" in page, "never over an utterance"
    assert "function vDone(){vCtl=null;vBusy=false;paintStop();earconDrain();vPump();}" in page
    assert "fetch('/ui/earcon.wav'" in page
    routes = {r.path for r in daemon.build_app().routes if hasattr(r, "path")}
    assert "/ui/earcon.wav" in routes
    import wave
    w = wave.open(io.BytesIO(pathlib.Path(daemon._UI_PACKAGED, "earcon.wav").read_bytes()))
    assert w.getframerate() == 44100 and w.getnchannels() == 1 and w.getnframes() / 44100 < 1.0


# Operator 11569, verbatim: whisper's answer to a non-speech take, auto-sent.
OH_OH_OH = ", ".join(["oh"] * 118).capitalize()


def test_a_degenerate_take_is_dropped_before_it_lands(ear):
    """Ruling 11572 (DES-014 s4/s5 amended): the broker returns whisper's own
    numbers with the text -- compression ratio over the whole take (zlib, the same
    heuristic whisper applies per segment), max no_speech_prob and min avg_logprob
    from verbose_json's segments when present -- and the PAGE drops the take,
    once, before append/auto-send, when ratio > 2.4 or no_speech_prob > 0.6 or
    avg_logprob < -1.0 or the text equals the previous take. Dropped: no bell, no
    post, no stub; console.debug only."""
    st = daemon.stt_take_stats
    assert st(OH_OH_OH)["compression_ratio"] > 2.4, st(OH_OH_OH)
    assert st("make it so, number one, and mind the gap")["compression_ratio"] < 1.2
    assert st("")["compression_ratio"] == 0.0 and "no_speech_prob" not in st("x")
    assert st("x", [{"no_speech_prob": 0.1}, {"no_speech_prob": 0.7, "avg_logprob": -1.4},
                    {"avg_logprob": -0.3}, "junk"]) == {"compression_ratio": 0.111,
                                                        "no_speech_prob": 0.7, "avg_logprob": -1.4}, \
        "worst segment wins: max no_speech_prob, min avg_logprob; junk rows ignored"
    # Through the route: the 11569 body comes back with its ratio, and the numbers
    # from the segments; the page reads them, the broker keeps nothing.
    _Stt.text, _Stt.segments = OH_OH_OH, [{"avg_logprob": -1.31, "no_speech_prob": 0.83}]
    code, out = _call(_req(_take(3.0), {"content-type": "audio/wav"}))
    assert code == 200 and out["text"] == OH_OH_OH and out["compression_ratio"] > 2.4
    assert out["no_speech_prob"] == 0.83 and out["avg_logprob"] == -1.31
    _Stt.text, _Stt.segments = "  make it so  ", []
    code, out = _call(_req(_take(2.0), {"content-type": "audio/wav"}))
    assert out == {"text": "make it so", "compression_ratio": 0.556}, "no segments -> no numbers, no invention"
    # The page: ONE gate, in earHeard, after the command match, before earLand.
    ear_js = UI[UI.index("const EAR_RATIO_MAX=2.4"):UI.index("function earLand(text){")]
    assert "const EAR_RATIO_MAX=2.4,EAR_NO_SPEECH_MAX=0.6,EAR_LOGPROB_MIN=-1.0;" in ear_js
    assert "function earDegenerate(d){" in ear_js and "let earPrevTake='';" in ear_js
    assert "if(d.compression_ratio>EAR_RATIO_MAX)return" in ear_js
    assert "if(d.no_speech_prob>EAR_NO_SPEECH_MAX)return" in ear_js
    assert "if(typeof d.avg_logprob==='number'&&d.avg_logprob<EAR_LOGPROB_MIN)return" in ear_js
    assert "if(t===earPrevTake)return 'same as the previous take';" in ear_js
    assert UI.count("earPrevTake='';") == 3, "the repeat memory clears with the box: init, after a send, on a spoken cancel (11581)"
    heard = ear_js[ear_js.index("function earHeard(d){"):]
    assert heard.index("if(c){earRun(c);return false;}") < heard.index("const why=earDegenerate(d);") \
        < heard.index("earPrevTake=text;") < heard.index("return earLand(text);"), \
        "command first (send twice is two sends), then the gate, then the text lands"
    assert "console.debug('ear: take dropped ('+why+'): '" in heard and "toast(" not in heard, \
        "dropped = console.debug only: no bell (earHeard returns false), no post, no stub"
    # A ratio and a text-repeat gate in JS, run: the 11569 body is dropped, speech lands.
    node = shutil.which("node")
    if not node:
        pytest.skip("node not on PATH")
    prog = ear_js.replace("function earLand(text){", "") + """
    var landed=[]; function earLand(t){landed.push(t);return true;}
    function earCommand(t){return t==='send'?{cmd:'send'}:null;} var ran=[]; function earRun(c){ran.push(c.cmd);}
    console.debug=function(){};
    if(earHeard({text:%s,compression_ratio:2.9})!==false)throw 'oh-oh landed';
    if(!earHeard({text:'make it so',compression_ratio:0.6}))throw 'speech dropped';
    if(earHeard({text:'make it so',compression_ratio:0.6})!==false)throw 'repeat landed';
    if(earHeard({text:'again',compression_ratio:0.7,no_speech_prob:0.9})!==false)throw 'no-speech landed';
    if(earHeard({text:'again',compression_ratio:0.7,avg_logprob:-1.5})!==false)throw 'low logprob landed';
    if(!earHeard({text:'again',compression_ratio:0.7,avg_logprob:-0.4}))throw 'good take dropped';
    earHeard({text:'send'});earHeard({text:'send'});
    if(ran.length!==2)throw 'send twice must be two sends, got '+ran.length;
    earPrevTake='';                                   // what a send / a cleared box does (11581)
    if(!earHeard({text:'again',compression_ratio:0.7}))throw 'the same words after a send must land';
    if(landed.join('|')!=='make it so|again|again')throw 'landed: '+landed.join('|');
    console.log('ok');
    """ % json.dumps(OH_OH_OH)
    res = subprocess.run([node, "-e", prog], capture_output=True, text=True)
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr or res.stdout


def test_the_earcon_vocabulary_is_one_table_one_function_one_toggle():
    """DES-014 section 5 earcon amendment (operator 11576/11578, architect 11577): eight named sounds,
    each on the event it names, synthesized in-page (no new files), never over an
    utterance (queued to vDone), one "sounds" setting per browser (default ON),
    constant volume. Skipped on purpose: countdown ticks, a dropped degenerate
    take, push-to-talk start/stop."""
    page = daemon._ui_read("index.html")
    table = page[page.index("const EARCONS={"):page.index("};", page.index("const EARCONS={"))]
    for name, kind in (("whoosh", "noise"), ("bonk", "sine"), ("ding", "wav"), ("bip", "sine"), ("bop", "sine"),
                       ("plip", "sine"), ("pop", "sine"), ("clunk", "square"), ("swish", "noise")):
        assert f" {name}:" in table and kind in table[table.index(f" {name}:"):table.index("\n", table.index(f" {name}:"))], name
    assert "const EARCON_GAIN=0.06;" in page
    assert "function earcon(name){" in page and "function soundsOn(){return localStorage.revSounds!=='0';}" in page
    # each event rings its own name, once, at the place it happens
    for call, where in (("earcon('whoosh');", "send accepted"), ("earcon('bonk');", "a red toast"),
                        ("earcon('bip');", "listen ON"), ("earcon('bop');", "listen OFF"),
                        ("earcon('plip');", "auto-send cancelled"), ("earcon('pop');", "message for me / human broadcast"),
                        ("earcon('clunk');", "attach done"), ("earcon('swish');", "room switched")):
        assert call in page, where
    assert page.count("earcon('whoosh');") == 1 and page.count("earcon('swish');") == 1 and page.count("earcon('clunk');") == 1
    assert "if(!info&&typeof earcon==='function')earcon('bonk');" in page, "any red toast bonks; an info toast does not"
    assert "if(!voiceOn&&m.from&&m.from!==myName&&(m.to===myName||(m.to==='*'&&humanNames.has(m.from))))earcon('pop');" in page, \
        "pop only with voice OFF (voice ON speaks it), never my own words, a human's broadcast or a message to me"
    assert "if(!quiet){listenPaint(listenVad?'listening':'');earcon('plip');}" in page, "the cancel that a person made"
    assert 'id="miSounds"' in page and 'id="phSounds"' in page and "function setSounds(on){" in page
    # the eight ring nowhere the ruling skipped: not in the countdown tick, not in the degenerate drop, not in talk
    tick = page[page.index("function pauseSendArm(){"):page.index("(function wirePauseSend(){")]
    assert "earcon(" not in tick.replace("pauseSendAbort(true)", "")
    heard = page[page.index("function earHeard(d){"):page.index("function earLand(text){")]
    assert "earcon(" not in heard
    talk = page[page.index("async function talkStart(){"):page.index("async function talkResample(")]
    assert "earcon(" not in talk


def test_a_browser_that_cannot_run_the_vad_still_listens():
    """DES-014 s2 defect (operator 11718, ruling 11719): iOS Safari refuses
    onnxruntime's WASM memory, so the ONNX VAD never starts there. The button
    must not be dead: a loudness gate on the same PCM the push-to-talk
    recorder takes, the same 3 s silence close, the same POST /stt -- and a
    toast that says which detector is running."""
    ear = UI[UI.index("const EAR_SILENCE_MS="):UI.index("async function openVoices(){")]
    # 1. the ONNX attempt is made single-threaded and unproxied first
    assert "o.env.wasm.numThreads=1;o.env.wasm.proxy=false;" in ear and "o.env.wasm.simd=true;" in ear
    # 2. a MIC refusal is still the phone's answer and does NOT fall back
    assert "if(e&&/NotAllowed|NotFound|NotReadable|SecurityError/.test(e.name||'')){" in ear
    # 3. anything else falls back rather than dying
    assert "return listenSimpleStart(e);" in ear
    # 4. the fallback shares the ruled knobs: one silence, one cap, one route
    simple = ear[ear.index("function listenSimpleStart(why){"):ear.index("function listenSimpleStop(){")]
    assert "g.quietMs>=EAR_SILENCE_MS||g.ms>=EAR_TAKE_CAP_MS" in simple
    assert "listenTake(f32,g.rate)" in ear and "fetch('/stt'" not in simple, \
        "one take path: the fallback closes a take, listenTake posts it"
    # 5. it says what it is -- a loudness gate is not a voice detector
    assert "cannot run the voice detector" in simple and "loudness" in simple
    # 6. the mic stays off the speakers, as in the recorder
    assert "sink.gain.value=0;" in simple
    # 7. stopping stops whichever ear is running
    assert "if(listenSimple)return listenSimpleStop();" in ear
    # 8. still per tab, never persisted
    for persisted in ("localStorage", "sessionStorage", "document.cookie"):
        assert persisted not in simple
