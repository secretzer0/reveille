#!/usr/bin/env bash
# Register the bus, then become whatever was asked for. The registration is the same
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

echo "reveille: ${REVEILLE_AGENT_ROLE} -> ${REVEILLE_URL}"
exec "$@"
