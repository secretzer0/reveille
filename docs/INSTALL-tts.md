# Installing the TTS (the voice host)

The synthesizer (DES-009): the Chatterbox-TTS-Server fork, one caller (the
broker), no auth, never a public port. The broker holds only
`REVEILLE_TTS_URL`; the synthesizer knows nothing about reveille.

Live deployment this document was read from (2026-09-08, `titan`,
192.168.89.104, Debian 13, RTX 3050 Laptop 4GB — yes, 4GB is enough):

| unit | state | runs |
|---|---|---|
| `reveille-tts.service` | active | container `reveille-tts:0.2.5` on 192.168.89.104:18004 |
| `reveille-tts-watchdog.timer` | active, every minute | probe `/api/model-info`, escalate 3/6/9 |

## Linux (verified on the live host)

Assumes OS + NVIDIA driver + container toolkit (see INSTALL-persona-writer.md
step 1 for the toolkit).

1. **Build the image** — on a machine with this repo (the image is built
   locally and pushed nowhere; every build takes its own incrementing tag,
   never a reused one — doctrine 14518):

       make tts-image        # builds the fork at the SHA docker/tts.upstream pins -> reveille-tts:0.2.x

   ~12GB. Move it to the voice host with `docker save | ssh ... docker load`
   if you build elsewhere.

2. **Install the unit** — `deploy/reveille-tts.service` IS the
   documentation; its header carries the full why. The act:

       sudo cp deploy/reveille-tts.service /etc/systemd/system/
       sudo systemctl edit reveille-tts     # the per-host override, below
       sudo systemctl daemon-reload
       sudo systemctl enable --now reveille-tts

   The override for the live deployment (a LAN IP, NEVER 0.0.0.0 — the
   server is unauthenticated by design, so it binds exactly the address
   the broker was told to call):

       [Service]
       Environment=TTS_BIND=192.168.89.104
       Environment=TTS_PUBLISH_PORT=18004
       Environment=TTS_REFERENCE=/home/vyzon/tts-measure/reference_audio
       Environment=TTS_CACHE=/home/vyzon/tts-measure/hf_cache
       User=vyzon

   A same-host broker needs no override at all (defaults: 127.0.0.1:8004,
   named volumes).

3. **The watchdog** — self-heal with a bounded ladder (probe asserts the
   model is LOADED and the device is not CPU; escalation restarts only as
   far as needed):

       sudo cp deploy/reveille-tts-watchdog.{sh,service,timer} /etc/systemd/system/  # .sh to /usr/local/bin per its header
       sudo systemctl enable --now reveille-tts-watchdog.timer

4. **PROVE IT** (the watchdog's own probe, run by hand):

       curl -s http://192.168.89.104:18004/api/model-info   # model loaded, device cuda
       journalctl -u reveille-tts -n 5

5. **Point the broker**: `REVEILLE_TTS_URL=http://192.168.89.104:18004` in
   `$SERVER_DATA/reveille.env`, then `make up` there. First synthesis after
   a cold start downloads ~1G of weights into TTS_CACHE.

## macOS, Apple Silicon (UNVERIFIED — written from vendor docs; no Mac in this fleet, operator 2026-09-08)

The image is `Dockerfile.cu128` — CUDA, it will not run on a Mac. The
native path runs the fork BARE with PyTorch's MPS backend on unified
memory:

1. **Clone the fork at the pinned SHA** (same provenance as the image):

       git clone https://github.com/secretzer0/Chatterbox-TTS-Server
       cd Chatterbox-TTS-Server && git checkout "$(cat /path/to/reveille/docker/tts.upstream)"

2. **Env + deps** (their own recipe, mac variant):

       uv venv && uv pip install -r requirements.txt

   PyTorch installs its Metal (MPS) build by default on Apple Silicon.

3. **Run** with the device pinned and the same bind discipline:

       # config.yaml (their file): device: mps ; host: <the LAN IP the broker calls> ; port: 18004
       uv run python server.py

   Keep-alive: launchd plist with `KeepAlive=true`, the unit's macOS
   equivalent. The watchdog script is plain bash + curl and ports as-is;
   schedule it with a launchd StartInterval=60 in place of the timer.

4. **Prove** with the same `/api/model-info` curl — expect `device: mps`.

CAPABILITY NOTES, honest ones:
- Chatterbox upstream targets CUDA first; MPS runs but some ops may fall
  back to CPU (PyTorch prints which). If `model-info` reports cpu, it
  still works — slower. The watchdog's device!=cpu assertion must be
  RELAXED on a Mac you accept CPU fallback on, or it will restart a
  working server forever.
- The four fork patches (OOM guard, divider chunking, pad-aware batched
  decode, first-chunk streaming) are device-independent Python and ride
  along unchanged.
- Unified memory sizes the model question away (~4G resident fits any
  M-series); latency is the number to measure before pointing the broker
  at it.
