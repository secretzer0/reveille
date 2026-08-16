# The script writer host (DES-013 section 8, slice 6)

One VM (Proxmox, two Tesla P40 passed through, Debian 13, LAN only), running
`llama-server` from llama.cpp with a Qwen3.8 GGUF. The broker reaches it over
`REVEILLE_SCRIPT_URL=http://<vm>:8080` in the clear on the operator's LAN with
`REVEILLE_LAN_PLAINTEXT=1` (DES-009 section 3; the boot banner and `/version`
name it). Nothing here is deployed by the broker's compose: this is its own box.

Order of operations, each a script here:

1. `build-llama.sh` -- driver + CUDA 12.8 prerequisites named (not installed:
   the driver is the operator's, R580 proprietary, never nvidia-open, never 590),
   llama.cpp cloned at the pinned tag, built for sm_61 (`CMAKE_CUDA_ARCHITECTURES=61`).
   FIRST GATE: `llama-server --help` runs and the build lists the Qwen3.8 arch.
2. `fetch-model.sh` -- both candidate quants (bartowski, Q6_K and Q4_K_M) into
   `/opt/writer/models`, sha256-pinned in `models.sha256`.
3. `bench.sh` + `measure.py` -- the number picks the pin: for each quant and
   flag set, start the server, measure time-to-first-sentence and tok/s with
   the writer's own prompt shape, stop it. The FIRST-SOUND budget is 2.0 s
   send-to-sound (ruled 11036); the writer's share is `REVEILLE_SCRIPT_TIMEOUT`
   = time to first sentence, 1.5 s. A quant that misses it is not the pin; if
   none makes it, a smaller Qwen3.8 sibling is (the role does not need 27B).
4. `llama-server.service` -- the systemd unit for the chosen pin; edit MODEL and
   FLAGS from the bench, `systemctl enable --now llama-server`.

The broker side is already there (slice 5): set `REVEILLE_SCRIPT_URL`,
`REVEILLE_SCRIPT_MODEL` (the GGUF's model id as the server reports it, or
empty for the server default), `REVEILLE_LAN_PLAINTEXT=1`, restart, and the
boot log says `scripts ON`.
