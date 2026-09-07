# Installing the persona writer and the ear (the GPU host)

One machine serves both language workers the broker calls: the **persona
writer** (DES-013, script generation — an OpenAI-compatible
`/v1/chat/completions`) and **speech-to-text** (STT, an OpenAI-compatible
`/v1/audio/transcriptions` — DES-014 and this fleet call it **the ear**:
the listening half of the voice loop, the TTS being the mouth; the
container is literally named `ear`). The broker holds only their URLs;
nothing on this host knows reveille exists.

Live deployment this document was read from (2026-09-08, `reveille-gpu`,
192.168.85.101, Debian 13, 2x RTX 3060 12GB):

| container | image | host port | serves |
|---|---|---|---|
| `writer` | `vllm/vllm-openai:v0.27.1` | 18080 | Qwen3.8-27B AWQ-INT4, TP=2, alias `writer` |
| `ear` (speech-to-text) | `ghcr.io/speaches-ai/speaches:latest-cuda` | 18090 | `deepdml/faster-whisper-large-v3-turbo-ct2` |

> HISTORY, so nobody restores the wrong recipe: the writer's FIRST engine
> was llama.cpp (`scripts/writer/README.md` — build, GGUF, systemd unit).
> Bench C (2026-08-17, rulings 11322/11340) moved it to vLLM TP=2 on the
> pair; the GGUFs in `~/models/qwen3.8-27b` (33G) and `~/gpu/writer.sh`
> remain as the FALLBACK engine on the same port, not the live one.

## Linux (verified on the live host)

Assumes the OS, the NVIDIA driver and the GPUs are already in place.

1. **Docker + NVIDIA container toolkit.**

       sudo apt install docker.io
       # toolkit per NVIDIA's install page for your distro, then:
       sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
       docker run --rm --gpus all nvidia/cuda:12.4.0-base-ubuntu22.04 nvidia-smi   # proves GPUs reach containers

2. **The model.** The writer serves an AWQ-INT4 quant of Qwen3.8-27B from a
   read-only models directory:

       mkdir -p ~/models/hf
       # hf CLI (uv tool install huggingface_hub) or git-lfs; the live host holds:
       hf download abihsoro/Qwen3.8-27B-AWQ-INT4 --local-dir ~/models/hf/abihsoro/Qwen3.8-27B-AWQ-INT4

   ~17G on disk. AWQ-INT4 with fp8 KV is what fits TWO 12GB cards at
   `--gpu-memory-utilization 0.82` while the ear shares GPU 0 — measured,
   not chosen (bench C).

3. **The writer.** The run script is the install — keep it at `~/gpu/writer-vllm.sh`
   (its authoritative copy, with the WHY of every flag, is what this repeats):

       docker run -d --name writer --gpus all --ipc=host --restart unless-stopped \
         -p 0.0.0.0:18080:8000 \
         -v "$HOME/models/hf:/models:ro" -v "$HOME/.cache/vllm:/root/.cache/vllm" \
         -e NCCL_P2P_DISABLE=1 \
         vllm/vllm-openai:v0.27.1 /models/abihsoro/Qwen3.8-27B-AWQ-INT4 \
         --served-model-name writer --tensor-parallel-size 2 \
         --max-model-len 6144 --max-num-seqs 2 --max-num-batched-tokens 1024 \
         --kv-cache-dtype fp8 --gpu-memory-utilization 0.82 \
         --reasoning-parser qwen3 \
         --default-chat-template-kwargs '{"enable_thinking": false}' \
         --enable-prefix-caching

   Flag notes that are deployment facts, not taste: `--max-model-len 6144`
   is architect 11347's arithmetic (9000 chars of code/JSON/CJK tokenize
   2-3 chars/tok); `NCCL_P2P_DISABLE=1` because consumer 3060s have no
   P2P; `enable_thinking: false` because the writer speaks, it does not
   reason aloud; `--restart unless-stopped` is deliberate — these are
   INFRASTRUCTURE, not agent bodies, and infra restarts itself.

   PROVE IT:

       curl -sf http://127.0.0.1:18080/health && echo writer-ok
       curl -s http://127.0.0.1:18080/v1/models | grep -o '"id":"writer"'

4. **Speech-to-text (the ear).** `~/gpu/ear.sh` on the live host; the run
   it performs:

       docker run -d --name ear --gpus all --restart unless-stopped \
         -p 0.0.0.0:18090:8000 \
         -v hf-hub-cache:/home/ubuntu/.cache/huggingface/hub \
         -e STT_MODEL_TTL=-1 -e WHISPER__TTL=-1 -e ENABLE_UI=false -e LOG_LEVEL=info \
         -e WHISPER__INFERENCE_DEVICE=cuda -e WHISPER__DEVICE_INDEX=0 \
         -e WHISPER__COMPUTE_TYPE=int8_float16 \
         -e PRELOAD_MODELS='["deepdml/faster-whisper-large-v3-turbo-ct2"]' \
         ghcr.io/speaches-ai/speaches:latest-cuda

   TTL=-1 keeps the model resident; device index 0 shares GPU 0 with the
   writer's first shard (the writer's 0.82 utilization leaves it room —
   change one and re-measure the other). First start downloads the model
   into the named volume (~1.5G).

   PROVE IT:

       curl -sf http://127.0.0.1:18090/health && echo ear-ok

5. **Point the broker at them** — on the BROKER host, in
   `$SERVER_DATA/reveille.env` (see INSTALL-broker.md):

       REVEILLE_SCRIPT_URL=http://192.168.85.101:18080
       REVEILLE_SCRIPT_MODEL=writer
       REVEILLE_STT_URL=http://192.168.85.101:18090
       REVEILLE_STT_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2

   then `make up` there. `/version` names each feature it found.

6. **Benching a candidate model** without losing the slot:
   `~/gpu/bench_sib.sh` (llama.cpp) and `~/gpu/vllm.sh` (vLLM) stop the
   writer for the window and restore it after; `bench_timings.py` /
   `bench_oai.py` print the numbers the ruling wants before any swap.

## macOS, Apple Silicon (UNVERIFIED — written from vendor docs; no Mac in this fleet, operator 2026-09-08)

vLLM has no Metal backend — the Linux engine does not port. The native
M-series path is the FALLBACK engine promoted to primary: **llama.cpp with
the Metal backend**, which uses unified memory directly (no TP, no NCCL —
one address space is the whole point of the hardware).

1. **llama.cpp**: `brew install llama.cpp` (or build from source with
   `-DGGML_METAL=ON`, the default on Apple Silicon).

2. **The model** — GGUF, not AWQ (AWQ is a CUDA-kernel format):

       hf download unsloth/Qwen3.8-27B-UD-Q4_K_XL-GGUF --local-dir ~/models/qwen3.8-27b

   ~17G resident. A 32GB machine holds it beside the OS; 16GB does not —
   use a smaller quant or model and say so in the broker's SCRIPT_MODEL.

3. **Serve** (mirrors `~/gpu/writer.sh`, minus the CUDA-only flags):

       llama-server -m ~/models/qwen3.8-27b/Qwen3.8-27B-UD-Q4_K_XL.gguf \
         --host 0.0.0.0 --port 18080 -ngl 999 -c 6144 -np 2 --jinja \
         --reasoning-budget 0 --chat-template-kwargs '{"enable_thinking": false}' \
         --alias writer

   `-ngl 999` offloads every layer to Metal; unified memory means there is
   no "fits on the card" question separate from "fits in RAM".
   Keep-alive: a launchd plist with `KeepAlive=true` is the systemd
   `Restart=always` equivalent — `~/Library/LaunchAgents/org.reveille.writer.plist`
   running exactly the command above.

4. **The ear**: the CUDA image is wrong here. Two native options, in order:
   - `uvx speaches` (bare, CTranslate2 CPU with `WHISPER__COMPUTE_TYPE=int8`)
     — same API, same env names, CPU int8 turbo-v3 transcribes near real
     time on M-series CPU cores;
   - whisper.cpp's `server` (Metal) if speaches-on-CPU proves too slow —
     but its API differs from OpenAI's shape, so the broker side would
     need checking first. Start with speaches.

5. **Prove** with the same two curls as Linux; point the broker at the
   Mac's LAN address.

CAPABILITY NOTE, stated rather than implied: throughput on one M-series
SoC will not match 2x3060 vLLM with continuous batching. The writer is
single-user bursty (one script per message), so latency, not throughput,
is the number to check: run `bench_timings.py` against the Mac before
declaring it live.
