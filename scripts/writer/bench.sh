#!/usr/bin/env bash
# The number picks the pin (DES-013 s8, rulings 11031/11035/11037): for each
# quant and flag set, start llama-server, measure, stop. Prints one line per
# config; the winner is the smallest quant whose p95 first sentence PASSES.
set -uo pipefail
BIN="${BIN:-/opt/writer/llama.cpp/build/bin/llama-server}"
MODELS="${MODELS:-/opt/writer/models}"
here="$(cd "$(dirname "$0")" && pwd)"
BASE="-c 32768 -fa on -ctk q8_0 -ctv q8_0 -np 1 --jinja -ngl 999 --host 127.0.0.1 --port 8081 --alias writer"
declare -a CONFIGS=(
  "Qwen3.8-27B-Q4_K_M.gguf|--split-mode tensor"
  "Qwen3.8-27B-Q4_K_M.gguf|--split-mode layer"
  "Qwen3.8-27B-Q4_K_M.gguf|--split-mode tensor --spec-type draft-mtp"
  "Qwen3.8-27B-Q6_K.gguf|--split-mode tensor"
  "Qwen3.8-27B-Q6_K.gguf|--split-mode layer"
  "Qwen3.8-27B-Q6_K.gguf|--split-mode tensor --spec-type draft-mtp"
)
for cfg in "${CONFIGS[@]}"; do
  m="${cfg%%|*}"; flags="${cfg#*|}"
  echo "== $m  $flags"
  "$BIN" -m "$MODELS/$m" $BASE $flags >"/tmp/llama-bench-$$.log" 2>&1 &
  pid=$!
  for _ in $(seq 1 240); do curl -sf http://127.0.0.1:8081/health >/dev/null 2>&1 && break; sleep 2; done
  if curl -sf http://127.0.0.1:8081/health >/dev/null 2>&1; then
    python3 "$here/measure.py" --url http://127.0.0.1:8081 --model writer --n 8
  else
    echo "  server did not come up (see /tmp/llama-bench-$$.log)"
  fi
  kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null
  echo
done
