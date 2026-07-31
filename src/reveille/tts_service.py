#!/usr/bin/env python3
"""DES-009 commit 1: the synthesizer, which answers exactly one caller.

  broker --(server side, compose network, by name)--> this service

It is never a browser origin (DES-009 section 3), publishes no host port, and
holds no state. `POST /speak {text, voice, knobs} -> audio/wav` is the whole
contract, and it is a contract between two agents' branches -- senior-dev's
worker is the only client -- so it is not changed here without telling them
first (architect, msg 8936).

ONE WORKER, ONE QUEUE, FOR FREE. This is a plain single-threaded HTTPServer, so
requests serialize by construction rather than by a lock somebody has to
maintain. A GPU that is asked to synthesize two utterances at once is slower at
both; the queue is the point, and the simplest queue is the one the stdlib
already has.

THE MODEL IS LOADED LAZILY and through a module-level hook, because the import
costs gigabytes of torch and this module has to be importable by a test suite
that has neither. Tests replace SYNTH; production calls _chatterbox().
"""
import hashlib
import hmac
import http.server
import json
import os
import pathlib

# Trust boundary. The worker caps every utterance already (DES-009 section 6 --
# audio is real time and the bus is not), so anything past this is either a bug
# in the caller or a caller that is not the worker. Refuse rather than truncate:
# a service that silently speaks half a request makes the caller's cap look like
# it worked.
MAX_CHARS = 800
# ...and a cap on the BODY, applied before a byte of it is read. MAX_CHARS is a
# property of the parsed text, so enforcing only that reads the whole request
# first: a caller declaring a gigabyte gets a gigabyte pulled into memory, and
# this server is single-threaded, so that request IS the service (msg 8941).
# Room for the cap plus json overhead and a long clip path, and nothing more.
MAX_BODY = 4096

# The default knobs; a bank voice gets a deterministic offset from these (§5).
DEFAULT_KNOBS = {"exaggeration": 0.5, "cfg_weight": 0.5}

SYNTH = None            # set by _chatterbox() on first use, or by a test
# WHICH DEVICE THIS PROCESS WILL SYNTHESIZE ON, decided once at startup and
# reported by /health. A container with no GPU reservation has no /dev/nvidia*,
# so torch falls back to the CPU and everything still WORKS -- correct-looking
# service, green healthcheck, ten times the latency, and nothing anywhere able to
# answer "is it using the GPU". A degradation that reports nothing is the class
# this fleet has ruled on twice; this is the report (msg 8941).
DEVICE = "unknown"


def pick_device():
    """cuda if this process can actually see one, else cpu. Separated from the
    model load on purpose: it costs a torch import and no download, so startup
    can answer the device question minutes before the first utterance exists."""
    try:
        import torch                                  # noqa: PLC0415
    except ImportError:
        return "unavailable: torch is not installed in this image"
    return "cuda" if torch.cuda.is_available() else "cpu"


def _digest(name):
    """A STABLE hash. Not Python's hash(): it is salted per process (PEP 456), so
    the same agent would get a different voice after every restart and two
    browsers would disagree about who sounds like what -- the exact property §5
    says must hold with no state kept. sha256 is stable across processes, hosts
    and versions, which is the whole requirement."""
    return int(hashlib.sha256(name.encode("utf-8")).hexdigest()[:16], 16)


def resolve_voice(name, *, samples, bank, overrides=None):
    """Which clip speaks for this name, and with what knobs (DES-009 section 5).

    A dropped sample wins; otherwise the bank is indexed by the name's digest and
    the SAME digest offsets the knobs. The offset is what stops two agents that
    land on one bank clip sounding identical -- a bank alone runs out at about a
    dozen agents, and this fleet already has five.
    """
    over = (overrides or {}).get(name) or {}
    if over.get("clip"):
        return {"clip": over["clip"], "source": "override",
                "knobs": {**DEFAULT_KNOBS, **(over.get("knobs") or {})}}
    if name in samples:
        return {"clip": samples[name], "source": "sample",
                "knobs": {**DEFAULT_KNOBS, **(over.get("knobs") or {})}}
    if not bank:
        return None
    h = _digest(name)
    knobs = {"exaggeration": round(0.30 + 0.10 * ((h >> 16) % 5), 2),
             "cfg_weight": round(0.30 + 0.10 * ((h >> 32) % 5), 2)}
    return {"clip": bank[h % len(bank)], "source": "bank",
            "knobs": {**knobs, **(over.get("knobs") or {})}}


def read_voices(root):
    """(samples, bank, overrides) from a directory, plus an OPTIONAL voices.json.

    Dropping a WAV in a directory is the interface (§5): voices/<name>.wav is
    that name's voice and needs no edit anywhere. voices.json exists for the case
    a file cannot express -- a clip shared by two names, or knobs someone wants
    to pin -- and its absence is normal rather than an error.
    """
    root = pathlib.Path(root)
    samples = {p.stem: str(p) for p in sorted(root.glob("*.wav"))}
    bank = [str(p) for p in sorted((root / "bank").glob("*.wav"))]
    overrides = {}
    cfg = root / "voices.json"
    if cfg.is_file():
        overrides = json.loads(cfg.read_text()) or {}
    return samples, bank, overrides


def _chatterbox():
    """Load once, on first use. Imported HERE and not at module scope so this
    file stays importable without torch -- which is what lets its logic be
    gated at unit cost on a machine that could never run the model."""
    from chatterbox.tts import ChatterboxTTS          # noqa: PLC0415
    device = DEVICE if DEVICE in ("cuda", "cpu") else pick_device()
    print(f"tts: loading the model on {device} -- the first utterance pays for "
          f"this, and a download on a fresh cache volume pays much more", flush=True)
    model = ChatterboxTTS.from_pretrained(device=device)

    def synth(text, clip, knobs):
        import io                                     # noqa: PLC0415
        import torchaudio                             # noqa: PLC0415
        wav = model.generate(text, audio_prompt_path=clip,
                             exaggeration=knobs.get("exaggeration", 0.5),
                             cfg_weight=knobs.get("cfg_weight", 0.5))
        buf = io.BytesIO()
        torchaudio.save(buf, wav, model.sr, format="wav")
        return buf.getvalue()
    return synth


class Handler(http.server.BaseHTTPRequestHandler):
    voices_root = "/voices"
    token = ""

    def log_message(self, fmt, *a):     # one line per request, not two
        print("tts: " + fmt % a, flush=True)

    def _send(self, code, body, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _fail(self, code, why):
        self._send(code, json.dumps({"error": why}).encode())

    def do_GET(self):
        if self.path.split("?")[0] == "/health":
            # The DEVICE rides the health reply because "is it using the GPU" is
            # the first question a slow synthesizer raises, and latency is not an
            # answer to it. `loaded` separates "cpu because that is all there is"
            # from "nothing has been synthesized yet".
            return self._send(200, json.dumps(
                {"status": "ok", "device": DEVICE, "loaded": SYNTH is not None}).encode())
        return self._fail(404, "no such path")

    def do_POST(self):
        if self.path.split("?")[0] != "/speak":
            return self._fail(404, "no such path")
        # The token is required whenever one is CONFIGURED. On the compose
        # network it is empty and the service publishes no port, so the network
        # is the boundary; off-network it is the only boundary there is, which is
        # why the broker refuses a remote URL without one (§3, senior-dev's half).
        # compare_digest, not ==: this service is designed to be movable off the
        # compose network, and there the token stops being a formality and becomes
        # the only boundary there is. A comparison that returns early leaks its own
        # prefix and length to whoever is timing it.
        if self.token and not hmac.compare_digest(
                self.headers.get("Authorization", ""), f"Bearer {self.token}"):
            return self._fail(401, "bad or missing bearer token")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._fail(400, "content-length is not a number")
        # REFUSED BEFORE A BYTE IS READ. The MAX_CHARS check below is on parsed
        # text, so reaching it means the whole declared body is already in memory.
        if n > MAX_BODY:
            return self._fail(413, f"body declares {n} bytes, cap is {MAX_BODY}")
        try:
            req = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, json.JSONDecodeError):
            return self._fail(400, "body is not json")
        text, name = (req.get("text") or "").strip(), (req.get("voice") or "").strip()
        if not text:
            return self._fail(400, "text is required")
        if len(text) > MAX_CHARS:
            return self._fail(400, f"text is {len(text)} chars, cap is {MAX_CHARS} -- "
                                   f"the caller caps utterances, so this is a caller bug")
        if not name:
            return self._fail(400, "voice is required")
        samples, bank, overrides = read_voices(self.voices_root)
        v = resolve_voice(name, samples=samples, bank=bank, overrides=overrides)
        if not v:
            return self._fail(503, "no voices installed -- drop a wav in voices/bank")
        knobs = {**v["knobs"], **(req.get("knobs") or {})}
        global SYNTH
        if SYNTH is None:
            SYNTH = _chatterbox()
        try:
            wav = SYNTH(text, v["clip"], knobs)
        except Exception as e:          # noqa: BLE001 -- one caller, and it must
            # hear WHY rather than a dead socket: a synth failure is a silent
            # message on the bus, never a stuck worker.
            return self._fail(500, f"synthesis failed: {type(e).__name__}: {e}")
        return self._send(200, wav, "audio/wav")


def main():
    global DEVICE
    port = int(os.environ.get("TTS_PORT", "8770"))
    Handler.voices_root = os.environ.get("TTS_VOICES", "/voices")
    Handler.token = os.environ.get("TTS_TOKEN", "")
    # Decided and SAID at startup, before anything is synthesized. A container
    # with no device reservation lands on the cpu and works, which is exactly why
    # it has to announce itself: the operator has three GPUs and the only symptom
    # of missing them would be "it feels slow".
    DEVICE = pick_device()
    if DEVICE != "cuda":
        print(f"tts: NO GPU VISIBLE -- synthesizing on {DEVICE}. If that is not "
              f"intended, the compose service needs a device reservation.", flush=True)
    # 127.0.0.1 would be unreachable from the broker's container; the isolation
    # is the absence of a published port in compose, not a loopback bind here.
    srv = http.server.HTTPServer(("0.0.0.0", port), Handler)   # noqa: S104
    print(f"tts: serving on :{port} on {DEVICE}, voices at {Handler.voices_root}, "
          f"token {'required' if Handler.token else 'not set'}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
