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


def wav(seconds, rate=24000, sampwidth=2):
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(sampwidth)
        w.setframerate(rate)
        w.writeframes(b"\x00" * int(seconds * rate) * sampwidth)
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
    daemon.VOICE_CLIP_MAX = 100
    try:
        assert "too large" in R(wav(5.0))
    finally:
        daemon.VOICE_CLIP_MAX = 10 * 1024 * 1024


# ---- resolution ---------------------------------------------------------------

def test_an_assigned_bank_voice_is_cloned_when_visible_and_falls_through_when_not(caplog):
    clips = ["architect.wav", "bank-quark.wav"]
    v = daemon.tts_voice("architect", clips=clips, predefined=["A.wav"], assigned="quark")
    assert v == {"voice_mode": "clone", "reference_audio_filename": "bank-quark.wav"}
    with caplog.at_level("WARNING"):
        v = daemon.tts_voice("architect", clips=clips, predefined=["A.wav"], assigned="picard")
    assert v == {"voice_mode": "clone", "reference_audio_filename": "architect.wav"}
    assert "bank voice picard is not visible" in caplog.text
    assert "TTS_VOICES_DIR" in caplog.text


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
    daemon._tts_enqueue(7, "r1", "alice", "s", "b", assigned="quark")
    daemon._tts_q.put(None)
    daemon._tts_worker("http://x", "", 1)
    assert seen == {"speaker": "alice", "assigned": "quark"}


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


def test_the_bank_cap_is_a_named_refusal_at_the_door(monkeypatch, tmp_path):
    c, admin, user, other, vd, who = _harness(monkeypatch, tmp_path)
    monkeypatch.setattr(store, "VOICE_BANK_MAX", 1)
    st, _ = _call(daemon.voice_clip_http, _req("PUT", "/voices/a/clip", wav(6.0), "", {"vid": "a"}))
    assert st == 200
    st, out = _call(daemon.voice_clip_http, _req("PUT", "/voices/b/clip", wav(6.0), "", {"vid": "b"}))
    assert st == 400 and "VOICE_BANK_MAX" in out["error"]
    assert not (vd / "bank-b.wav").exists(), "a refused row leaves no clip"


# ---- compose and the tab -----------------------------------------------------

def test_compose_mounts_the_brokers_voices_dir_into_the_synthesizer_read_only():
    """Section 3: TTS_VOICES_DIR defaults to <SERVER_DATA>/voices -- the tree the
    broker sees rw at /data/voices -- and the synthesizer sees it :ro. Read from
    the shipped file: the default is what a fresh host gets."""
    compose = open(os.path.join(os.path.dirname(__file__), "..", "docker", "compose.yml")).read()
    assert "${TTS_VOICES_DIR:-${SERVER_DATA}/voices}:/app/reference_audio:ro" in compose


def test_the_voices_tab_speaks_the_routes_shape():
    ui = open(os.path.join(os.path.dirname(__file__), "..", "src", "reveille", "ui", "bus",
                           "index.html")).read()
    assert 'data-tab="voices"' in ui and "voices:()=>openVoices()" in ui
    assert "'/voices/'+encodeURIComponent(id)+'/clip?name='" in ui   # PUT raw bytes
    assert "method:'PATCH',body:JSON.stringify({persona:" in ui
    assert "accept=\"audio/wav,.wav\"" in ui
