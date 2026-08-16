"""The broker's synthesizer client, against a stub of devnen/Chatterbox-TTS-Server
(DES-009 section 4.1). Nobody in this fleet runs the model in the suite; what is
measured here is that the caller speaks the server's shape and resolves a voice
the way section 5 rules -- a dropped clip wins and is cloned, otherwise the
bank by digest, the same answer across restarts.
"""
import http.server
import json
import threading

import pytest

from reveille import daemon

BANK = ["Abigail.wav", "Adrian.wav", "Alexander.wav", "Alice.wav"]


def test_a_dropped_clip_wins_and_is_cloned():
    v = daemon.tts_voice("architect", clips=["architect.wav"], predefined=BANK)
    assert v == {"voice_mode": "clone", "reference_audio_filename": "architect.wav"}


def test_the_bank_is_indexed_by_digest_and_the_same_digest_offsets_the_knobs():
    a = daemon.tts_voice("architect", clips=[], predefined=BANK)
    assert a["voice_mode"] == "predefined" and a["predefined_voice_id"] in BANK
    assert 0.30 <= a["exaggeration"] <= 0.70 and 0.30 <= a["cfg_weight"] <= 0.70
    # Stable: sha256, not hash() -- the same name speaks with the same voice
    # after every restart (section 5).
    assert daemon.tts_voice("architect", clips=[], predefined=BANK) == a
    h = daemon._digest("architect")
    assert a["predefined_voice_id"] == BANK[h % len(BANK)]


def test_the_bank_order_the_server_lists_does_not_change_the_voice():
    """Verdict 11010 BLOCKING 1: the server lists its directory in filesystem
    order, which differs across hosts and upstream bumps. Section 5 says every
    host agrees, so the index runs over the SORTED bank -- same bank in two
    orders, one answer."""
    a = daemon.tts_voice("architect", clips=[], predefined=BANK)
    b = daemon.tts_voice("architect", clips=[], predefined=list(reversed(BANK)))
    assert a == b


class _OddStub(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = json.dumps([] if self.path == "/get_reference_files"
                          else {"voices": [{"filename": "Abigail.wav"}]}).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        raise AssertionError("a bank of dicts must never reach /tts")


def test_a_bank_of_the_wrong_shape_is_a_named_silence_not_a_dict_in_the_request():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _OddStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert daemon._tts_speak(f"http://127.0.0.1:{srv.server_address[1]}",
                                 "", "a", "x", 5) is None
    finally:
        srv.shutdown()


def test_nothing_to_speak_with_is_silence_not_an_error():
    assert daemon.tts_voice("architect", clips=[], predefined=[]) is None


class _Stub(http.server.BaseHTTPRequestHandler):
    """The three routes the client uses, answering the way upstream does."""
    seen = []
    clips = ["architect.wav"]

    def log_message(self, *a):
        pass

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/get_reference_files":
            return self._json(self.clips)
        if self.path == "/v1/audio/voices":
            return self._json({"status": "ok", "voices": BANK})
        self.send_error(404)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        req = json.loads(self.rfile.read(n))
        _Stub.seen.append((self.path, self.headers.get("authorization"), req))
        self.send_response(200)
        self.send_header("content-type", "audio/wav")
        self.send_header("content-length", "8")
        self.end_headers()
        self.wfile.write(b"RIFFstub")


@pytest.fixture
def stub():
    _Stub.seen = []
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Stub)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_the_client_speaks_upstreams_tts_route(stub):
    """POST /tts with the resolved voice and knobs, wav out, and the bearer only
    when configured -- what a proxy in front of a remote server checks (§3)."""
    assert b"".join(daemon._tts_speak(stub, "sekrit", "architect", "hello", 5)) == b"RIFFstub"
    assert b"".join(daemon._tts_speak(stub, "", "devops", "hi", 5)) == b"RIFFstub"
    (p1, auth1, r1), (p2, auth2, r2) = _Stub.seen
    assert p1 == p2 == "/tts"
    assert auth1 == "Bearer sekrit" and auth2 is None
    # stream=true: the bytes exist before the message is finished, and the
    # worker writes them as they land (section 2 as amended).
    assert r1 == {"text": "hello", "output_format": "wav", "split_text": True,
                  "stream": True,
                  "voice_mode": "clone", "reference_audio_filename": "architect.wav"}
    assert r2["voice_mode"] == "predefined" and r2["predefined_voice_id"] in BANK
    assert {"exaggeration", "cfg_weight"} <= set(r2)


def test_a_server_that_is_down_returns_none(monkeypatch):
    """Section 7: silent, never a raise, and no retry -- the worker owns the wait."""
    assert daemon._tts_speak("http://127.0.0.1:9", "", "a", "x", 1) is None
