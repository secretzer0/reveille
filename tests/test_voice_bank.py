"""DES-013 slice 2: the bank -- the directory, the clip refusal, the routes, and
resolution with an assigned voice.

Bounds under test, from section 3: WAV only (mp3 refused, no decoder), PCM,
5.0 s <= duration <= 30.0 s, <= 10 MiB, multipart refused; replace is atomic
(mtime moves, no .tmp left) and keeps the id; governance = anyone adds, uploader
or admin replaces/edits; tts_voice clones bank-<id>.wav when assigned and
visible, logs and falls through when assigned but unseen, and never lets a
hand-dropped <name>.wav match a bank-* file.

Proven RED on feat/des-013-schema-bank-assignment-script @ 8b08642:
voice_clip_refusal, voices_http, _voices_dir do not exist and tts_voice takes
bank=, not predefined=.
"""
import asyncio
import io
import json
import os
import struct
import sys
import wave

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


def wav(seconds, rate=24000, sampwidth=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(_tone(int(seconds * rate), sampwidth))
    return buf.getvalue()


# ---- the pure refusal --------------------------------------------------------

def test_the_clip_refusal_names_the_bound_it_hit():
    R = daemon.voice_clip_refusal
    assert R(wav(5.0)) is None
    assert R(wav(30.0)) is None
    assert "too short" in R(wav(4.9))
    assert "too long" in R(wav(30.1))
    assert "not a PCM WAV" in R(b"ID3\x03\x00\x00\x00" + b"\xff\xfb" * 400)   # mp3
    assert "not a PCM WAV" in R(b"")
    # A float WAV (format tag 3) is refused: the stdlib reads PCM only.
    flt = (struct.pack("<4sI4s", b"RIFF", 36 + 8, b"WAVE")
           + struct.pack("<4sIHHIIHH", b"fmt ", 16, 3, 1, 24000, 96000, 4, 32)
           + struct.pack("<4sI", b"data", 8) + b"\0" * 8)
    assert "not a PCM WAV" in R(flt)
    # SILENCE (ruling 11213): a peak under -40 dBFS is a recorder that heard
    # nothing, refused by name; every PCM width the stdlib reads is measured.
    for sw in (1, 2, 3, 4):
        assert R(wav(6.0, sampwidth=sw)) is None, sw
        quiet = io.BytesIO()
        with wave.open(quiet, "wb") as w:
            w.setnchannels(1); w.setsampwidth(sw); w.setframerate(24000)  # noqa: E702
            w.writeframes((b"\x80" if sw == 1 else b"\x00" * sw) * 24000 * 6)
        assert "silent" in R(quiet.getvalue()) and "-inf dBFS" in R(quiet.getvalue()), sw
    faint = io.BytesIO()
    with wave.open(faint, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(24000)  # noqa: E702
        w.writeframes((b"\x64\x00" + b"\x9c\xff") * 24000 * 3)   # +-100 of 32768 = -50 dBFS
    assert "silent (peak -50 dBFS" in R(faint.getvalue())
    daemon.VOICE_CLIP_MAX = 100
    try:
        assert "too large" in R(wav(5.0))
    finally:
        daemon.VOICE_CLIP_MAX = 10 * 1024 * 1024


# ---- resolution ---------------------------------------------------------------

def test_an_assigned_bank_voice_is_cloned_when_visible_and_falls_through_when_not(caplog):
    # `assigned` is the VERSIONED clip name the row derives (ruling 11104):
    # bank-<id>-<updated_ns>.wav -- a replace is a new name.
    assert daemon.clip_name({"id": "quark", "updated_ns": 7}) == "bank-quark-7.wav"
    clips = ["architect.wav", "bank-quark-7.wav"]
    v = daemon.tts_voice("architect", clips=clips, predefined=["A.wav"], assigned="bank-quark-7.wav")
    assert v == {"voice_mode": "clone", "reference_audio_filename": "bank-quark-7.wav"}
    # ASSIGNED BUT NOT VISIBLE IS SILENCE (ruled 12101). It used to fall through
    # to the dropped clip or the digest pick, which sounds harmless and is not:
    # that rendition gets cached as tts-<mid>.webm and outlives the outage that
    # caused it. A speaker WITH an assignment is owed that voice or none.
    with caplog.at_level("WARNING"):
        v = daemon.tts_voice("architect", clips=clips, predefined=["A.wav"], assigned="bank-picard-1.wav")
    assert v is None, "silent and uncached beats the wrong voice kept forever"
    assert "bank clip bank-picard-1.wav not visible after push -- silent, will retry" in caplog.text


def test_the_digest_pick_survives_for_a_speaker_who_was_promised_nothing():
    """Ruled 12101 narrows the refusal to ASSIGNED speakers only. A speaker with
    no assignment was never promised a particular voice, so the predefined
    digest pick is the right answer and not a degraded one -- silencing those
    would mute most of the fleet to fix a bank problem they do not have."""
    v = daemon.tts_voice("architect", clips=[], predefined=["A.wav", "B.wav"])
    assert v is not None and v["voice_mode"] == "predefined"
    # And it is still stable and sorted: same name, same voice, every host.
    again = daemon.tts_voice("architect", clips=[], predefined=["B.wav", "A.wav"])
    assert again == v, "sorted, so readdir order cannot change who sounds like what"


def test_the_bank_prefix_is_reserved_a_name_never_matches_a_bank_file():
    v = daemon.tts_voice("bank-quark", clips=["bank-quark.wav"], predefined=["A.wav"])
    assert v["voice_mode"] == "predefined"


def test_the_worker_hands_the_assignment_to_the_synthesizer(monkeypatch, tmp_path):
    seen = {}

    def speak(url, token, speaker, text, timeout, assigned=None):
        seen.update(speaker=speaker, assigned=assigned)
        return iter([b"RIFFx"])
    monkeypatch.setattr(daemon, "_files_dir", tmp_path)
    monkeypatch.setattr(daemon, "_tts_speak", speak)
    monkeypatch.setattr(daemon, "_tts_get", lambda *a, **k: None)
    monkeypatch.setattr(daemon, "_feed_push", lambda room, msg: None)
    monkeypatch.setattr(daemon, "_tts_on", True)
    monkeypatch.setattr(store, "voice_for", lambda conn, room, key: "quark" if key == "agent:a1" else None)
    monkeypatch.setattr(store, "voice_get", lambda conn, vid: {"id": vid, "updated_ns": 7})
    monkeypatch.setattr(daemon, "_room_listening", lambda room: True)   # someone has voice on
    daemon._tts_enqueue(7, "r1", "alice", "s", "b", key="agent:a1")
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert seen == {"speaker": "alice", "assigned": "bank-quark-7.wav"}


# ---- routes ------------------------------------------------------------------

class _P:
    def __init__(self, name, user_id, is_admin=False):
        self.kind, self.name, self.user_id, self.is_admin = "user", name, user_id, is_admin
        self.rooms = {}


def _db(tmp_path):
    path = str(tmp_path / "b.db")
    c = store.connect(path)
    store.migrate(c, path)
    admin = store.setup_first_admin(c, "travis", "hunter2hunter2")
    user = store.create_user(c, "vyzon", "hunter2hunter2")
    other = store.create_user(c, "randy", "hunter2hunter2")
    return c, admin, user, other


def _req(method, path, body=b"", query="", params=None, ctype="application/octet-stream"):
    headers = [(b"content-type", ctype.encode()), (b"content-length", str(len(body)).encode())]
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    scope = {"type": "http", "method": method, "path": path, "headers": headers,
             "query_string": query.encode(), "path_params": params or {}}
    return Request(scope, receive)


def _call(fn, req):
    resp = asyncio.run(fn(req))
    return resp.status_code, json.loads(resp.body)


def _harness(monkeypatch, tmp_path):
    c, admin, user, other = _db(tmp_path)
    monkeypatch.setattr(daemon, "_conn", c)
    vd = tmp_path / "voices"
    vd.mkdir()
    monkeypatch.setattr(daemon, "_voices_dir", vd)
    who = {"p": _P("vyzon", user["id"])}
    monkeypatch.setattr(daemon, "_principal", lambda request: who["p"])
    return c, admin, user, other, vd, who


def test_put_creates_then_replaces_atomically_and_lists(monkeypatch, tmp_path):
    c, admin, user, other, vd, who = _harness(monkeypatch, tmp_path)
    st, out = _call(daemon.voice_clip_http,
                    _req("PUT", "/voices/quark/clip", wav(6.0), "name=Quark", {"vid": "quark"}))
    assert st == 200 and out["id"] == "quark" and out["name"] == "Quark"
    assert abs(out["seconds"] - 6.0) < 0.01 and out["uploaded_by"] == user["id"]
    f = vd / "bank-quark.wav"
    assert f.exists() and not (vd / "bank-quark.wav.tmp").exists()
    m1 = f.stat().st_mtime_ns
    os.utime(f, ns=(m1 - 5_000_000_000, m1 - 5_000_000_000))   # make the move visible
    m1 = f.stat().st_mtime_ns
    # Replace: same id, new bytes, mtime moves, no .tmp left, name kept when unnamed.
    st, out = _call(daemon.voice_clip_http,
                    _req("PUT", "/voices/quark/clip", wav(9.0), "", {"vid": "quark"}))
    assert st == 200 and out["name"] == "Quark" and abs(out["seconds"] - 9.0) < 0.01
    assert f.stat().st_mtime_ns > m1 and not (vd / "bank-quark.wav.tmp").exists()
    with wave.open(str(f)) as w:
        assert w.getnframes() == 9 * 24000
    st, out = _call(daemon.voices_http, _req("GET", "/voices"))
    assert st == 200 and out["llm"] is False
    assert [v["id"] for v in out["voices"]] == ["quark"]
    assert out["voices"][0]["editable"] is True


def test_the_refusals_reach_the_door(monkeypatch, tmp_path):
    c, admin, user, other, vd, who = _harness(monkeypatch, tmp_path)
    P = daemon.voice_clip_http
    st, out = _call(P, _req("PUT", "/voices/x/clip", wav(4.0), "", {"vid": "x"}))
    assert st == 400 and "too short" in out["error"]
    st, out = _call(P, _req("PUT", "/voices/x/clip", wav(31.0), "", {"vid": "x"}))
    assert st == 400 and "too long" in out["error"]
    st, out = _call(P, _req("PUT", "/voices/x/clip", b"ID3" + b"\xff\xfb" * 500, "", {"vid": "x"}))
    assert st == 400 and "not a PCM WAV" in out["error"]
    st, out = _call(P, _req("PUT", "/voices/x/clip", wav(6.0), "", {"vid": "x"},
                            ctype="multipart/form-data; boundary=abc"))
    assert st == 400 and "RAW BYTES" in out["error"]
    monkeypatch.setattr(daemon, "VOICE_CLIP_MAX", 1000)
    st, out = _call(P, _req("PUT", "/voices/x/clip", wav(6.0), "", {"vid": "x"}))
    assert st == 413 and "too large" in out["error"]
    monkeypatch.setattr(daemon, "VOICE_CLIP_MAX", 10 * 1024 * 1024)
    assert not list(vd.iterdir()), "a refused clip leaves nothing on disk"
    assert store.voices(c) == []
    # A bad id is the store's refusal, surfaced as 400 by the guard.
    st, out = _call(P, _req("PUT", "/voices/../x/clip", wav(6.0), "", {"vid": "../x"}))
    assert st == 400


def test_anyone_adds_only_the_uploader_or_an_admin_replaces_or_edits(monkeypatch, tmp_path):
    c, admin, user, other, vd, who = _harness(monkeypatch, tmp_path)
    P, E = daemon.voice_clip_http, daemon.voice_http
    st, _ = _call(P, _req("PUT", "/voices/quark/clip", wav(6.0), "name=Quark", {"vid": "quark"}))
    assert st == 200
    who["p"] = _P("randy", other["id"])
    st, out = _call(P, _req("PUT", "/voices/quark/clip", wav(7.0), "", {"vid": "quark"}))
    assert st == 403
    st, out = _call(E, _req("PATCH", "/voices/quark", json.dumps({"persona": "x"}).encode(),
                            "", {"vid": "quark"}, ctype="application/json"))
    assert st == 403
    st, out = _call(P, _req("PUT", "/voices/rom/clip", wav(6.0), "name=Rom", {"vid": "rom"}))
    assert st == 200, "a stranger to quark still adds his own"
    st, out = _call(daemon.voices_http, _req("GET", "/voices"))
    assert {v["id"]: v["editable"] for v in out["voices"]} == {"quark": False, "rom": True}
    who["p"] = _P("travis", admin["id"], is_admin=True)
    st, out = _call(E, _req("PATCH", "/voices/quark", json.dumps({"persona": "Ferengi bartender."}).encode(),
                            "", {"vid": "quark"}, ctype="application/json"))
    assert st == 200 and out["persona"] == "Ferengi bartender."
    st, out = _call(P, _req("PUT", "/voices/quark/clip", wav(8.0), "", {"vid": "quark"}))
    assert st == 200 and out["uploaded_by"] == user["id"], "replace keeps the uploader"
    st, out = _call(E, _req("PATCH", "/voices/nope", b"{}", "", {"vid": "nope"},
                            ctype="application/json"))
    assert st == 404


def test_the_sample_line_travels_with_the_voice_and_has_a_cap(monkeypatch, tmp_path):
    c, admin, user, other, vd, who = _harness(monkeypatch, tmp_path)
    P, E = daemon.voice_clip_http, daemon.voice_http
    st, _ = _call(P, _req("PUT", "/voices/quark/clip", wav(6.0), "name=Quark", {"vid": "quark"}))
    assert st == 200
    st, out = _call(E, _req("PATCH", "/voices/quark", json.dumps({"sample": "  Rule one.  "}).encode(),
                            "", {"vid": "quark"}, ctype="application/json"))
    assert st == 200 and out["sample"] == "Rule one."
    st, out = _call(daemon.voices_http, _req("GET", "/voices"))
    assert out["voices"][0]["sample"] == "Rule one."
    st, out = _call(E, _req("PATCH", "/voices/quark",
                            json.dumps({"sample": "x" * (daemon.VOICE_SAMPLE_MAX + 1)}).encode(),
                            "", {"vid": "quark"}, ctype="application/json"))
    assert st == 400 and "sample" in out["error"]
    ui = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                           "index.html")).read()
    assert "JSON.stringify({sample:" in ui and "esc(v.sample||'')" in ui


def test_the_bank_cap_is_a_named_refusal_at_the_door(monkeypatch, tmp_path):
    c, admin, user, other, vd, who = _harness(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "VOICE_BANK_MAX", 1)
    st, _ = _call(daemon.voice_clip_http, _req("PUT", "/voices/a/clip", wav(6.0), "", {"vid": "a"}))
    assert st == 200
    st, out = _call(daemon.voice_clip_http, _req("PUT", "/voices/b/clip", wav(6.0), "", {"vid": "b"}))
    assert st == 400 and "VOICE_BANK_MAX" in out["error"]
    assert not (vd / "bank-b.wav").exists(), "a refused row leaves no clip"


# ---- compose and the tab -----------------------------------------------------


def test_the_voices_tab_speaks_the_routes_shape():
    ui = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                           "index.html")).read()
    assert 'data-tab="voices"' in ui and "voices:()=>openVoices()" in ui
    assert "'/voices/'+encodeURIComponent(id)+'/clip?name='" in ui   # PUT raw bytes
    assert "method:'PATCH',body:JSON.stringify({persona:" in ui
    assert "accept=\"audio/wav,.wav\"" in ui
    # Cards (operator): every control inside the border of its voice; two add
    # flows; personal decided at creation (the PUT carries it); delete confirms
    # in place; rename = PATCH name + PUT /rename when the id moved.
    assert 'class="vCard' in ui and "'&personal=1'" in ui
    assert "ADD TO THE BANK" in ui and "MY PERSONAL VOICE" in ui
    # Personal voices are listed FIRST with their own add flow; an empty sample
    # box still auditions with a default line (operator: audition area).
    assert ui.index('<div class="pSec">MY PERSONAL VOICE</div>') < ui.index('<div class="pSec">THE BANK</div>') \
        < ui.index('<div class="pSec">ADD TO THE BANK</div>')
    assert "const t=inp.value.trim()||('This is '+vname+' speaking on reveille." in ui
    assert "confirming={kind:'voice',id:b.dataset.vdel}" in ui
    assert "'/rename',\n     {method:'PUT',body:JSON.stringify({id:nid})}" in ui


def test_the_recorder_builds_a_pcm_wav_in_the_browser_and_uses_the_same_put():
    """A human records their own sample (operator). MediaRecorder is NOT used
    (webm/opus, no decoder on the broker); the PCM comes from the Web Audio
    graph and a 16-bit RIFF/WAVE is built client-side, then the ordinary PUT
    carries it. The id defaults to the username so the (a0) name-match makes
    it the speaker's; the length shows while recording (ruling 11121)."""
    ui = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                           "index.html")).read()
    assert "MediaRecorder" not in ui.replace("MediaRecorder is not used", "")
    assert "getUserMedia({audio:" in ui and "createScriptProcessor(" in ui
    w = ui[ui.index("function wavBlob(pcm,rate){"):]
    w = w[:w.index("\n}\n")]
    for needle in ("'RIFF'", "'WAVE'", "'fmt '", "d.setUint16(20,1,true)", "d.setUint16(22,1,true)",
                   "d.setUint16(34,16,true)", "'data'"):
        assert needle in w, needle
    # One recorder wiring serves BOTH add flows (bank / personal); the recorded
    # blob is the file the same PUT sends; the length shows while recording.
    assert "const bankSt=rec('bank',$('bankId')), persSt=rec('pers',$('persId'));" in ui
    assert "st.file=r.blob" in ui and "putVoiceClip(id,myName,persSt.file,true)" in ui
    # A silent take (no input device / no permission) is refused at stop and
    # named while recording -- never stored, never cloned.
    assert "silent:r.peak<0.01" in ui and "if(r.silent){st.file=null;" in ui
    assert "NO SIGNAL from the microphone" in ui
    assert "'recording '+sec.toFixed(1)+' s'" in ui
