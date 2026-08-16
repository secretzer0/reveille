#!/usr/bin/env bash
# Both candidate quants from bartowski, sha256-pinned (models.sha256 beside this
# script; pins read from the HF API on 2026-08-16). The bench picks one.
set -euo pipefail
REPO="${REPO:-bartowski/Qwen3.8-27B-GGUF}"
DEST="${DEST:-/opt/writer/models}"
here="$(cd "$(dirname "$0")" && pwd)"
sudo mkdir -p "$DEST" && sudo chown "$USER" "$DEST"
cd "$DEST"
for f in Qwen3.8-27B-Q6_K.gguf Qwen3.8-27B-Q4_K_M.gguf; do
  [ -f "$f" ] || curl -L --fail --retry 5 -C - -o "$f" "https://huggingface.co/$REPO/resolve/main/$f"
done
sha256sum -c "$here/models.sha256"
ls -la "$DEST"
