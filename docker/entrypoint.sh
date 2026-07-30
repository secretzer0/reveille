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

# Provision step 3.2.4: clone the repo the launcher named, into ~/repos -- the
# path the DES-005 data root persists (pre-P3 this was ~/work, which the bind
# never covered, so checkouts silently died with the container). Best-effort --
# a private repo with no creds fails here and the agent clones it by hand; the
# health gate is join+arm, not the checkout. Auth rides the claude home
# (gh/git creds) or $GITHUB_TOKEN from the user's P2 profile.
if [ -n "${REVEILLE_REPO_URL:-}" ] && [ -z "$(ls -A /home/agent/repos 2>/dev/null)" ]; then
  git clone "${REVEILLE_REPO_URL}" /home/agent/repos/work \
    || echo "reveille: repo clone failed (${REVEILLE_REPO_URL}) -- clone by hand" >&2
fi

# DES-005 P3 / architect ruling msg 8607: the chosen role's prompt lives in the
# agent's CLAUDE.md between a DELIMITER PAIR, and the entrypoint rewrites what
# is between them on EVERY boot. The old form appended once behind a
# presence-of-marker check -- which asks "has this ever been written" when the
# question is "does this still match" -- so provision_agent(replace=True), which
# keeps the data root, preserved the marker and every re-provision with a new
# role SILENTLY kept the old one. An edit form would report success and change
# nothing. The role prompt is DERIVED from the environment, so it tracks the
# environment; everything OUTSIDE the delimiters is the agent's own working
# memory and survives untouched. Rewrite is atomic (same discipline as patch()
# above): a torn CLAUDE.md is an agent that boots confused.
if [ -n "${REVEILLE_ROLE_PROMPT:-}" ]; then
  python3 - <<'PY'
import os, pathlib
path = pathlib.Path.home() / ".claude" / "CLAUDE.md"
OPEN, CLOSE = "<!-- reveille role -->", "<!-- /reveille role -->"
block = f"{OPEN}\n# reveille role\n{os.environ['REVEILLE_ROLE_PROMPT']}\n{CLOSE}"
try:
    text = path.read_text()
except OSError:
    text = ""
if OPEN in text and CLOSE in text:
    head, _, rest = text.partition(OPEN)
    _, _, tail = rest.partition(CLOSE)
    text = head + block + tail
elif "# reveille role" in text:
    # Migration: the pre-0.2.7 single-marker form has no closing delimiter, so
    # the region must be INFERRED: marker to the next markdown heading, else
    # EOF. Sound because no shipped role prompt contains a line-start "#"
    # (checked against ROLE_PROMPTS), while an agent appending its own notes
    # after the role block plausibly starts with one -- those notes are its
    # working memory and must survive. One block either way: an agent reading
    # contradictory role text in its own CLAUDE.md is worse than a stale role.
    head, _, rest = text.partition("# reveille role")
    tail_lines = rest.splitlines(keepends=True)[1:]   # drop the marker line
    tail = ""
    for i, ln in enumerate(tail_lines):
        if ln.startswith("#"):
            tail = "".join(tail_lines[i:])
            break
    text = (head.rstrip("\n") + ("\n\n" if head.strip() else "")
            + block + "\n" + ("\n" + tail if tail else ""))
else:
    text = text.rstrip("\n") + ("\n\n" if text.strip() else "") + block + "\n"
path.parent.mkdir(parents=True, exist_ok=True)   # a fresh volume has no ~/.claude yet
tmp = path.with_suffix(".tmp")
tmp.write_text(text)
os.replace(tmp, path)
PY
fi

# Claude Code first-run wizard: a provisioned agent boots into `claude` with no
# human at the keyboard, so an interactive theme picker is a hang, not a prompt
# -- the operator's first agent sat on "Choose the text style" forever and never
# reached the bus. Seed only the keys that are ABSENT (the CLAUDE.md marker
# discipline): an agent that has evolved its own config keeps it, and a restart
# never rewrites a choice someone made. ~/.claude.json is container-local (the
# bind covers ~/.claude and ~/repos), so every new container needs this.
python3 - <<'PY'
import json, os, pathlib
home = pathlib.Path.home()


def patch(path, updates):
    """setdefault-merge one JSON file, atomically. Only ABSENT keys are written:
    an agent that changed a setting keeps its choice across restarts."""
    try:
        d = json.loads(path.read_text())
    except (OSError, ValueError):
        d = {}
    def merge(dst, src):
        for k, v in src.items():
            if isinstance(v, dict):
                merge(dst.setdefault(k, {}), v)
            else:
                dst.setdefault(k, v)
    merge(d, updates)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, path)   # atomic: a torn config is a wizard on next boot


patch(home / ".claude.json", {
    "hasCompletedOnboarding": True,
    "theme": "dark",
    "autoMode": True,
    "autoModeOptInDismissed": True,
    # The cwd's trust dialog is the same class of hang, one step later.
    "projects": {p: {"hasTrustDialogAccepted": True}
                 for p in ("/home/agent/repos", "/home/agent")},
})
# settings.json lives in the PERSISTED home, so this must never clobber: same
# setdefault discipline, and the agent may edit it freely afterwards.
#
# skipDangerousModePermissionPrompt is Claude Code's OWN record of the bypass
# acknowledgement -- it is what the CLI writes here when a human accepts the
# warning, found by accepting it once and diffing the home. Seeding it is the
# operator declaring that acceptance for containers they own, once, instead of
# per agent: without it every new agent boots into a modal nobody is watching
# and never reaches the bus. What justifies it is the container itself -- own
# fs, own data root, cpu/memory/pid caps, and NO docker socket (that stays with
# the launcher), so the sandbox the warning asks for is the thing the agent is
# already inside.
#
# THE STOP HOOK IS THE WATCHER BACKSTOP, and containers were shipping without
# it: the image seeded permissions and onboarding but never the hook, so nothing
# ever blocked a containerised agent's stop to say "you are not armed". Host
# agents had that backstop from `make register` / join-here; container agents had
# only their own discipline, and discipline does not survive an outage --
# reveille-senior-ui-ux ended a turn unarmed during a broker blackout and sat
# deaf for 21 HOURS with 44 rings in its spool, on a bus that was healthy again
# after the first few minutes (msg 8573). The hook needs no bus to run and is
# most needed when there is none.
patch(home / ".claude" / "settings.json",
      {"permissions": {"defaultMode": "bypassPermissions"},
       "skipDangerousModePermissionPrompt": True,
       "hooks": {"Stop": [{"hooks": [{"type": "command",
                                      "command": "/usr/local/bin/agent-stop-hook"}]}]}})
PY

# Unattended git: permission mode silences the PROMPTS, but these three are
# what actually fail a commit or a push, and no permission setting fixes them.
git config --global --get user.email >/dev/null 2>&1 || \
  git config --global user.email "${REVEILLE_GIT_EMAIL:-${REVEILLE_AGENT_ROLE}@reveille.local}"
git config --global --get user.name >/dev/null 2>&1 || \
  git config --global user.name "${REVEILLE_GIT_NAME:-${REVEILLE_AGENT_ROLE}}"
git config --global --get safe.directory >/dev/null 2>&1 || \
  git config --global --add safe.directory '*'
# gh reads GH_TOKEN first; the P2 profile supplies GITHUB_TOKEN. Wiring gh as
# git's credential helper is what makes `git push` over HTTPS non-interactive --
# without it the push asks for a username on a terminal nobody is watching.
if [ -n "${GITHUB_TOKEN:-}" ]; then
  export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"
  gh auth setup-git >/dev/null 2>&1 || true
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

# The wake socket holder (DES-003 2.1): one daemon, one attachment, rings
# become spool files. In a container the entrypoint is the supervisor -- it
# dies and restarts with the container; the flock makes a double-start a
# no-op. Token rides this shell's env, never argv.
ws_url="${REVEILLE_URL/http/ws}/wake"
mkdir -p "$HOME/.reveille/spool/${REVEILLE_AGENT_ROLE}"
( while :; do
    reveille-waked --url "$ws_url" --name "$REVEILLE_AGENT_ROLE" \
      >>"$HOME/.reveille/spool/${REVEILLE_AGENT_ROLE}/waked.log" 2>&1
    sleep 2
  done ) &

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
  exec tmux new-session -A -s agent -c /home/agent/repos "$@"
fi

# Detached run (the launcher's path): this shell is PID 1. On `docker stop` PID 1
# gets SIGTERM -- WITHOUT a trap it would exit immediately, docker would SIGKILL the
# tmux server, and claude would die mid-turn never dropping its waiter or acking its
# inbox, manufacturing exactly the stale-presence confusion the fleet keeps paying
# for. So: trap TERM, forward it to the agent process in the pane, and let docker's
# grace window give claude a clean shutdown before the eventual SIGKILL.
tmux new-session -d -s agent -c /home/agent/repos "$@"

forward_term() {
  pid="$(tmux list-panes -t agent -F '#{pane_pid}' 2>/dev/null | head -1)"
  [ -n "$pid" ] && kill -TERM "$pid" 2>/dev/null || true
}
trap forward_term TERM INT

# sleep 1 (not 5) so the trap is serviced promptly: the signal interrupts the sleep,
# the trap forwards TERM, and this loop exits as soon as claude tears the session down.
while tmux has-session -t agent 2>/dev/null; do sleep 1; done
