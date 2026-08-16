#!/usr/bin/env bash
# Build llama-server for two Tesla P40 (Pascal, sm_61) on Debian 13.
# Prerequisites the OPERATOR installs first (named, not done here -- driver
# choices are the operator's): NVIDIA R580 datacenter driver, PROPRIETARY
# kernel module (nvidia-open is Turing+; 590+ dropped Pascal), the newest
# 580.x on the download page; cuda-toolkit-12-8 (CUDA 13 dropped sm_61);
# nvidia-smi -e 0 (ECC off), nvidia-smi -pm 1. Then this script.
set -euo pipefail
LLAMA_TAG="${LLAMA_TAG:-b10453}"          # pinned; move deliberately, re-run the gate
PREFIX="${PREFIX:-/opt/writer}"
command -v nvcc >/dev/null || { echo "nvcc not on PATH -- install cuda-toolkit-12-8 and export PATH=/usr/local/cuda-12.8/bin:\$PATH" >&2; exit 1; }
nvcc --version | grep -q "release 12.8" || { echo "REFUSING: CUDA toolkit is not 12.8 (13 dropped sm_61)" >&2; exit 1; }
sudo mkdir -p "$PREFIX" && sudo chown "$USER" "$PREFIX"
cd "$PREFIX"
if [ ! -d llama.cpp ]; then git clone --depth 1 --branch "$LLAMA_TAG" https://github.com/ggml-org/llama.cpp.git; fi
cd llama.cpp
git fetch --depth 1 origin tag "$LLAMA_TAG" && git checkout -q "$LLAMA_TAG"
cmake -B build -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=61 -DLLAMA_CURL=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release -j "$(nproc)" --target llama-server llama-cli
# FIRST GATE (DES-013 section 8): the build knows the architecture and runs.
./build/bin/llama-server --help >/dev/null
if ! grep -rqi "qwen3.8\|qwen38\|qwen3_8" src/ 2>/dev/null; then
  echo "REFUSING: this llama.cpp tag does not name the Qwen3.8 architecture -- move LLAMA_TAG forward" >&2
  exit 1
fi
echo "built $PREFIX/llama.cpp/build/bin/llama-server at $LLAMA_TAG for sm_61"
