#!/usr/bin/env bash
# Register the bus, then run the agent inside tmux session "agent" with the ttyd
# sidecar serving the browser plane (DES-002 T1). The registration is the same
# `claude mcp add` a standalone agent runs (see `make register`) -- same URL, same two
# headers. If this ever needs something a laptop cannot do, the container has become
# special and that is the bug.
set -euo pipefail

: "${REVEILLE_AGENT_ROLE:?set REVEILLE_AGENT_ROLE (your bus name)}"
: "${REVEILLE_TOKEN:?set REVEILLE_TOKEN (your bus credential)}"
: "${REVEILLE_URL:=http://127.0.0.1:8765}"

claude mcp remove agentbus --scope user >/dev/null 2>&1 || true
claude mcp add --transport http --scope user agentbus "${REVEILLE_URL}/mcp" \
  --header "Authorization: Bearer ${REVEILLE_TOKEN}" \
  --header "X-Agent: ${REVEILLE_AGENT_ROLE}" >/dev/null

# Per-container gate secret (DES-002 4.3): injected by the launcher at provision
# (T2); a hand-run container gets a random one, never printed -- mint grant
# tokens from inside: `docker exec <name> attach-gate mint viewer`.
if [ -z "${REVEILLE_GATE_SECRET:-}" ]; then
  REVEILLE_GATE_SECRET="$(openssl rand -hex 32)"
fi
export REVEILLE_GATE_SECRET
# Persisted to the container's own FS (never the volume) so `docker exec ...
# attach-gate mint` works too: exec sees create-time env only, not this shell's.
# Dies with the container -- same lifetime as the grants it signs.
umask 077
printf '%s' "$REVEILLE_GATE_SECRET" > /home/agent/.gate-secret
umask 022

# Browser plane: ttyd execs attach-gate with the ?arg= token from the URL and
# nothing else. -W passes client input over the wire; whether tmux HONORS it is
# the gate's exec choice (-r for viewers), which no client can reach.
# ponytail: while-loop supervisor -- ttyd is a sidecar, its death must not take
# the agent down (4.5); replace with a real supervisor if restart churn appears.
( while :; do
    ttyd -W -a -p "${REVEILLE_TTYD_PORT:-7681}" attach-gate >/dev/null 2>&1
    sleep 1
  done ) &

echo "reveille: ${REVEILLE_AGENT_ROLE} -> ${REVEILLE_URL} (tmux: agent, ttyd: ${REVEILLE_TTYD_PORT:-7681})"

if [ -t 0 ]; then
  # Interactive run: the operator lands straight in the session (created if
  # absent). Laptop parity holds -- same claude, just inside tmux.
  exec tmux new-session -A -s agent -c /home/agent/work "$@"
fi

# Detached run: the session carries the agent; the container lives exactly as
# long as the session does, so restart policy sees the agent's real exit.
tmux new-session -d -s agent -c /home/agent/work "$@"
while tmux has-session -t agent 2>/dev/null; do sleep 5; done
