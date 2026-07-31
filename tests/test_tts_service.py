#!/usr/bin/env python3
"""DES-009 commit 1: the synthesizer service.

The container has never been built and the model has never been loaded -- no
agent in this fleet has a docker socket or a GPU. So what is gated here is
everything that does NOT need them, which is more than it sounds: the service is
driven over a real socket with the model replaced by a stub, and the compose
shape is asserted by enumerating every published port in the file rather than by
looking at the one service this commit adds.
"""
import http.client
import json
import os
import pathlib
import re
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from reveille import tts_service  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parent.parent
COMPOSE = (REPO / "docker" / "compose.yml").read_text()


# ---- voice resolution (DES-009 section 5) ------------------------------------

def test_a_dropped_sample_wins_and_the_bank_is_deterministic(tmp_path):
    bank = [f"/voices/bank/{i:02d}.wav" for i in range(6)]
    got = tts_service.resolve_voice("architect", samples={"architect": "/voices/architect.wav"},
                                    bank=bank)
    assert got["source"] == "sample" and got["clip"] == "/voices/architect.wav"

    # SAME ANSWER IN A DIFFERENT PROCESS. This is the property §5 rests on -- every
    # browser and every restart agree on who sounds like what with no state kept --
    # and Python's own hash() would break it silently, being salted per process.
    # Pinning the literal result is what makes that regression fail here rather
    # than sounding like a new agent after a restart.
    a = tts_service.resolve_voice("senior-ui-ux", samples={}, bank=bank)
    b = tts_service.resolve_voice("senior-ui-ux", samples={}, bank=bank)
    assert a == b and a["source"] == "bank"
    import subprocess
    out = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0,'src');"
         "from reveille import tts_service as t;"
         "print(t.resolve_voice('senior-ui-ux', samples={}, bank=["
         "'/voices/bank/00.wav','/voices/bank/01.wav','/voices/bank/02.wav',"
         "'/voices/bank/03.wav','/voices/bank/04.wav','/voices/bank/05.wav'])['clip'])"],
        capture_output=True, text=True, cwd=str(REPO),
        env={**os.environ, "PYTHONHASHSEED": "random"})
    assert out.stdout.strip() == a["clip"], \
        "the voice moved between processes -- a salted hash reached the bank index"


def test_two_names_on_one_bank_clip_still_differ_by_knob():
    """Bank alone runs out at about a dozen agents; bank plus knobs does not. If
    the knob offset ever stops depending on the name, this is the failure -- two
    developers become indistinguishable by ear, which is the whole feature."""
    bank = ["/voices/bank/only.wav"]          # forced collision: one clip, many names
    voices = [tts_service.resolve_voice(n, samples={}, bank=bank)
              for n in ("alice", "bob", "carol", "dave", "erin", "frank")]
    assert {v["clip"] for v in voices} == {"/voices/bank/only.wav"}
    assert len({(v["knobs"]["exaggeration"], v["knobs"]["cfg_weight"]) for v in voices}) > 1


def test_no_voices_installed_is_an_answer_not_a_crash():
    assert tts_service.resolve_voice("x", samples={}, bank=[]) is None


def test_read_voices_reads_a_directory_and_survives_no_json(tmp_path):
    (tmp_path / "bank").mkdir()
    (tmp_path / "tmelhiser.wav").write_bytes(b"RIFF")
    (tmp_path / "bank" / "02.wav").write_bytes(b"RIFF")
    (tmp_path / "bank" / "01.wav").write_bytes(b"RIFF")
    samples, bank, over = tts_service.read_voices(tmp_path)
    assert list(samples) == ["tmelhiser"]
    assert [pathlib.Path(b).name for b in bank] == ["01.wav", "02.wav"], "bank must be ordered"
    assert over == {}, "an absent voices.json is normal, not an error"
    (tmp_path / "voices.json").write_text(json.dumps({"bob": {"knobs": {"exaggeration": 0.9}}}))
    assert tts_service.read_voices(tmp_path)[2]["bob"]["knobs"]["exaggeration"] == 0.9


# ---- the service, driven over a real socket ----------------------------------

class _Server:
    """The real handler on a real port, with the model replaced. Everything but
    the synthesis itself executes -- routing, auth, the caps, the error bodies."""

    def __init__(self, tmp_path, token=""):
        import http.server
        (tmp_path / "bank").mkdir(exist_ok=True)
        (tmp_path / "bank" / "01.wav").write_bytes(b"RIFF")
        cls = type("H", (tts_service.Handler,),
                   {"voices_root": str(tmp_path), "token": token})
        self.calls = []
        tts_service.SYNTH = lambda text, clip, knobs: (
            self.calls.append((text, clip, knobs)) or b"RIFF....fake wav")
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), cls)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def call(self, method, path, body=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request(method, path, json.dumps(body) if body is not None else None, headers or {})
        r = c.getresponse()
        return r.status, r.getheader("Content-Type"), r.read()

    def close(self):
        # shutdown() waits for the handler loop, and a handler blocked on a body
        # that will never arrive never returns -- so a defect in the read path
        # would hang TEARDOWN rather than fail a test. Do not wait on it: a gate
        # whose failure mode is a stuck suite is a gate someone deletes.
        threading.Thread(target=self.srv.shutdown, daemon=True).start()
        tts_service.SYNTH = None


def test_speak_returns_wav_and_health_is_plain(tmp_path):
    s = _Server(tmp_path)
    try:
        assert json.loads(s.call("GET", "/health")[2])["status"] == "ok"
        code, ctype, body = s.call("POST", "/speak", {"text": "hello", "voice": "architect"})
        assert code == 200 and ctype == "audio/wav" and body.startswith(b"RIFF")
        text, clip, knobs = s.calls[0]
        assert text == "hello" and clip.endswith("01.wav")
        assert set(knobs) == {"exaggeration", "cfg_weight"}
        # A caller may override knobs per utterance; the resolved voice is the default.
        s.call("POST", "/speak", {"text": "hi", "voice": "architect",
                                  "knobs": {"exaggeration": 0.9}})
        assert s.calls[1][2]["exaggeration"] == 0.9
    finally:
        s.close()


def test_the_service_refuses_what_it_should_and_says_why(tmp_path):
    s = _Server(tmp_path)
    try:
        for body, code, why in (
                ({"voice": "a"}, 400, "text is required"),
                ({"text": "x"}, 400, "voice is required"),
                ({"text": "x" * (tts_service.MAX_CHARS + 1), "voice": "a"}, 400, "cap is"),
        ):
            got, _, out = s.call("POST", "/speak", body)
            assert got == code and why in json.loads(out)["error"], out
        assert s.call("GET", "/nope")[0] == 404
        assert s.call("POST", "/nope", {})[0] == 404
        assert not s.calls, "a refused request must never reach the model"
    finally:
        s.close()


def test_a_configured_token_is_required_and_an_unset_one_is_not(tmp_path):
    s = _Server(tmp_path, token="s3cret")
    try:
        req = {"text": "x", "voice": "a"}
        assert s.call("POST", "/speak", req)[0] == 401, "no token, token configured"
        assert s.call("POST", "/speak", req,
                      {"Authorization": "Bearer wrong"})[0] == 401
        assert s.call("POST", "/speak", req,
                      {"Authorization": "Bearer s3cret"})[0] == 200
        assert len(s.calls) == 1, "an unauthorized request reached the model"
    finally:
        s.close()
    s = _Server(tmp_path)                      # no token configured: the network is the boundary
    try:
        assert s.call("POST", "/speak", {"text": "x", "voice": "a"})[0] == 200
    finally:
        s.close()


def test_a_synthesis_failure_answers_rather_than_hanging(tmp_path):
    """A synth that raises must produce a body the worker can log. A dead socket
    would leave the worker waiting, and DES-009 section 2 says a synthesizer that
    is down means silent messages, never a stuck room."""
    s = _Server(tmp_path)
    try:
        def boom(*a):
            raise RuntimeError("cuda is on fire")
        tts_service.SYNTH = boom
        code, _, body = s.call("POST", "/speak", {"text": "x", "voice": "a"})
        assert code == 500 and "cuda is on fire" in json.loads(body)["error"]
    finally:
        s.close()


# ---- the compose shape -------------------------------------------------------

def test_no_service_in_compose_publishes_a_port_except_the_broker():
    """Enumerated, not spot-checked: the claim is about EVERY service, so the
    assertion reads every ports: key in the file rather than looking at the one
    this commit added. A future service that publishes a port fails here."""
    services = {}
    cur = None
    for line in COMPOSE.splitlines():
        m = re.match(r"^  ([a-z][\w-]*):\s*$", line)
        if m:
            cur = m.group(1)
            services[cur] = []
        elif cur and re.match(r"^    \w", line):
            services[cur].append(line.strip().split(":")[0])
    assert {"broker", "tts", "proxy"} <= set(services), services
    published = {name for name, keys in services.items() if "ports" in keys}
    assert published == {"broker"}, \
        f"services publishing a host port: {published} -- only the broker may"
    assert "ports" not in services["tts"], "the synthesizer must not be reachable off-network"


def test_the_broker_reaches_the_synthesizer_by_name_on_the_shared_network():
    tts = COMPOSE[COMPOSE.index("\n  tts:"):COMPOSE.index("\n  proxy:")]
    assert "aliases: [reveille-tts]" in tts, "no network alias -- nothing can dial it"
    assert "profiles: [voices]" in tts, \
        "the synthesizer must be opt-in, or a deploy without the model image stalls"
    broker = COMPOSE[COMPOSE.index("\n  broker:"):COMPOSE.index("\n  # DES-009 commit 1")]
    assert "REVEILLE_TTS_URL" in broker, "the broker is never told where the service is"


# ---- the device, said out loud (msg 8941) ------------------------------------

def _fake_torch(cuda):
    import types
    t = types.ModuleType("torch")
    t.cuda = types.SimpleNamespace(is_available=lambda: cuda)
    return t


def test_the_service_says_which_device_it_took(monkeypatch, tmp_path):
    """A container with no device reservation lands on the CPU and works: correct
    service, green healthcheck, ten times the latency, three idle GPUs, and the
    only symptom is "it feels slow". So the device is REPORTED. Driven with torch
    stubbed both ways, because this machine has neither a GPU nor torch."""
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(True))
    assert tts_service.pick_device() == "cuda"
    monkeypatch.setitem(sys.modules, "torch", _fake_torch(False))
    assert tts_service.pick_device() == "cpu"
    # ...and the two answers must not render the same downstream, or reporting it
    # buys nothing. /health is where the operator's question gets answered.
    seen = []
    for dev in ("cuda", "cpu"):
        monkeypatch.setattr(tts_service, "DEVICE", dev)
        s = _Server(tmp_path)
        try:
            tts_service.SYNTH = None          # nothing has been spoken yet
            seen.append(json.loads(s.call("GET", "/health")[2]))
        finally:
            s.close()
    assert [h["device"] for h in seen] == ["cuda", "cpu"], seen
    # `loaded` separates "cpu because that is all there is" from "nothing has been
    # synthesized yet" -- two states that would otherwise read identically to
    # whoever is asking why the first utterance took a minute.
    assert seen[0]["loaded"] is False


def test_a_missing_torch_is_named_rather_than_guessed(monkeypatch):
    """An image without torch is broken, and it must say so rather than reporting
    a device it never checked."""
    import builtins
    real = builtins.__import__

    def no_torch(name, *a, **k):
        if name == "torch":
            raise ImportError("no module named torch")
        return real(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", no_torch)
    assert "torch is not installed" in tts_service.pick_device()


def test_an_oversized_body_is_refused_before_it_is_read(tmp_path):
    """MAX_CHARS is a property of the PARSED text, so enforcing only that means
    the whole declared body is already in memory -- and this server is
    single-threaded, so that one request is the entire service."""
    s = _Server(tmp_path)
    try:
        c = http.client.HTTPConnection("127.0.0.1", s.port, timeout=5)
        # Declare far more than the cap and send nothing: a server that reads
        # first would block here until the timeout, not answer 413.
        c.putrequest("POST", "/speak")
        c.putheader("Content-Length", str(tts_service.MAX_BODY * 1000))
        c.endheaders()
        # PREDICTED RED, and it must be an assertion rather than a hang: a server
        # that reads first blocks here until the client timeout, so the timeout is
        # caught and turned into the sentence that names the defect.
        try:
            r = c.getresponse()
        except (TimeoutError, OSError) as e:
            raise AssertionError(
                f"the server began reading a body it should have refused ({e!r})") from e
        assert r.status == 413, f"read the body before refusing it: {r.status}"
        assert "cap is" in json.loads(r.read())["error"]
    finally:
        s.close()


def test_the_token_is_compared_in_constant_time():
    """Off the compose network this token is the only boundary there is, so the
    comparison must not leak its own prefix and length to whoever is timing it.
    Asserted on the SOURCE because a timing property cannot be measured reliably
    in a unit test -- and stating that is better than a flaky measurement."""
    src = (REPO / "src" / "reveille" / "tts_service.py").read_text()
    do_post = src[src.index("    def do_POST("):src.index("\ndef main(")]
    assert "hmac.compare_digest" in do_post, "the token is compared with =="
    assert "!= f\"Bearer" not in do_post
