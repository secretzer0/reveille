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

claude mcp remove reveille --scope user >/dev/null 2>&1 || true
claude mcp add --transport http --scope user reveille "${REVEILLE_URL}/mcp" \
  --header "Authorization: Bearer ${REVEILLE_TOKEN}" \
  --header "X-Agent: ${REVEILLE_AGENT_ROLE}" >/dev/null

# Provision step 3.2.4: clone the repo the launcher named, into the work dir, if it
# is empty. Best-effort -- a private repo with no creds in the volume fails here and
# the agent clones it by hand; the health gate is join+arm, not the checkout. Auth
# rides whatever the claude-home volume carries (gh/git creds), same as a laptop.
if [ -n "${REVEILLE_REPO_URL:-}" ] && [ -z "$(ls -A /home/agent/work 2>/dev/null)" ]; then
  git clone "${REVEILLE_REPO_URL}" /home/agent/work \
    || echo "reveille: repo clone failed (${REVEILLE_REPO_URL}) -- clone by hand" >&2
fi

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

# Browser plane: ttyd execs `attach-gate attach` with the ?arg= token appended.
# The literal "attach" is the security boundary: -a gives the CLIENT argv, and
# only this pinned first arg keeps the trusted-side subcommands (mint, verify)
# out of the browser's reach -- without it, ?arg=mint&arg=driver is an
# unauthenticated signing oracle. -W passes client input over the wire; whether
# tmux HONORS it is the gate's exec choice (-r for viewers).
# ponytail: while-loop supervisor -- ttyd is a sidecar, its death must not take
# the agent down (4.5); replace with a real supervisor if restart churn appears.
( while :; do
    ttyd -W -a -p "${REVEILLE_TTYD_PORT:-7681}" attach-gate attach >/dev/null 2>&1
    sleep 1
  done ) &

echo "reveille: ${REVEILLE_AGENT_ROLE} -> ${REVEILLE_URL} (tmux: agent, ttyd: ${REVEILLE_TTYD_PORT:-7681})"

if [ -t 0 ]; then
  # Interactive run: the operator lands straight in the session (created if
  # absent). Laptop parity holds -- same claude, just inside tmux.
  exec tmux new-session -A -s agent -c /home/agent/work "$@"
fi

# Detached run (the launcher's path): this shell is PID 1. On `docker stop` PID 1
# gets SIGTERM -- WITHOUT a trap it would exit immediately, docker would SIGKILL the
# tmux server, and claude would die mid-turn never dropping its waiter or acking its
# inbox, manufacturing exactly the stale-presence confusion the fleet keeps paying
# for. So: trap TERM, forward it to the agent process in the pane, and let docker's
# grace window give claude a clean shutdown before the eventual SIGKILL.
tmux new-session -d -s agent -c /home/agent/work "$@"

forward_term() {
  pid="$(tmux list-panes -t agent -F '#{pane_pid}' 2>/dev/null | head -1)"
  [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
}
trap forward_term TERM INT

# sleep 1 (not 5) so the trap is serviced promptly: the signal interrupts the sleep,
# the trap forwards TERM, and this loop exits as soon as claude tears the session down.
while tmux has-session -t agent 2>/dev/null; do sleep 1; done
