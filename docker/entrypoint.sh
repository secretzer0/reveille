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

# ---- BOOT REPORT (architect ruling 8691) -------------------------------------
# A diagnostic is only a diagnostic if the party who needs it can reach it.
#
# The entrypoint already reported a failed clone -- to stderr, into docker logs.
# The agent has no docker socket BY DESIGN, so the only record of its own broken
# boot was in the one place it cannot look. It found an empty repos/ and had to
# ask a human to read logs for it. That is worse than silence, because it looks
# like diligence.
#
# So every boot writes what it ATTEMPTED, what SUCCEEDED and what is MISSING to a
# file in the agent's own home. Truncated per boot: this is the state of THIS
# boot, not a history, and a report that accumulates is one nobody reads.
#
# UNDER ~/.claude, which is a BIND MOUNT, not container-local (ruling 8732). A
# container-local report dies with the container -- taking the explanation of a
# failed boot with it exactly when someone is acting on that failed boot -- and
# a mounted path is readable from the host with no docker at all. Retire keeps
# the home so the report survives it; erase destroys the home, and erase means
# erase.
#
# ONE PREVIOUS COPY, because truncation destroys the prior report wherever it
# lives, and a re-provision is precisely when the PRIOR boot's report is the
# thing someone needs. One predecessor is not accumulation.
BOOT_REPORT="/home/agent/.claude/boot-report.md"
BOOT_REPORT_PREV="/home/agent/.claude/boot-report.prev.md"
mkdir -p "$(dirname "$BOOT_REPORT")"
[ -f "$BOOT_REPORT" ] && mv -f "$BOOT_REPORT" "$BOOT_REPORT_PREV"
: > "$BOOT_REPORT"
say() { printf '%s\n' "$*" >> "$BOOT_REPORT"; }
note() { printf '%s\n' "$*" >> "$BOOT_REPORT"; printf '%s\n' "reveille: $*" >&2; }

say "# reveille boot report"
say ""
say "Written by the container entrypoint every boot, because the agent cannot"
say "read docker logs. If something below says MISSING or FAILED, that is why"
say "the thing you are looking for is not there."
say ""
say "- agent: ${REVEILLE_AGENT_ROLE}"
say "- broker: ${REVEILLE_URL}"
say ""
say "## inputs"
say ""
if [ -n "${REVEILLE_ROLE_PROMPT:-}" ]; then
  say "- role prompt: present (written into ~/.claude/CLAUDE.md)"
else
  note "- role prompt: **MISSING** -- no REVEILLE_ROLE_PROMPT was passed, so"
  say "  ~/.claude/CLAUDE.md has no reveille role block. You know what you are"
  say "  only from your bus name and brief()."
fi
if [ -n "${GITHUB_TOKEN:-}" ]; then
  say "- github token: present"
else
  say "- github token: absent (private clones and pushes will not authenticate)"
fi
if [ -f /run/reveille-auth/.credentials.json ]; then
  say "- claude login: copied from the user's shared login home"
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  say "- claude credential: from the environment"
else
  note "- claude credential: **MISSING** -- claude will stop at a login prompt"
fi
say ""
say "## repo"
say ""

# ---- PLUGINS: INSTALLED AT BUILD, MASKED AT RUNTIME --------------------------
# The image installs caveman and ponytail at build time into /home/agent/.claude
# -- correct when that path was a NAMED VOLUME, because docker seeds an empty
# named volume from the image. DES-005 moved the agent home to a BIND MOUNT, and
# a bind mount is NOT seeded: it shadows whatever the image put there. So the
# marketplaces and the installs were present in the image, absent at runtime, and
# CAVEMAN_DEFAULT_MODE / PONYTAIL_DEFAULT_MODE stayed set on PID 1 -- so anything
# checking the env reported "configured" while neither skill existed.
#
# That is the same shape as the clone (~/work -> ~/repos) and the role prompt: a
# mechanism that was right under the old storage shape and silently void after it
# changed, with an env var still declaring the intent. An env var that configures
# a capability must be set by whatever INSTALLS it, or it is a claim nobody
# verified (senior-ui-ux, msg 8713; ratified).
#
# The marketplace CLONES live outside ~/.claude, so they survive the mount and the
# install is a re-registration rather than a download -- offline-safe and pinned
# to the same commits the image built.
say ""
say "## plugins"
say ""
for mk in caveman ponytail; do
  src="/home/agent/${mk}-marketplace"
  if [ ! -d "$src" ]; then
    note "- ${mk}: **MISSING** from the image (expected ${src})"
    continue
  fi
  if claude plugin list 2>/dev/null | grep -qi "^${mk}\b"; then
    say "- ${mk}: already installed in this home"
    continue
  fi
  claude plugin marketplace add "$src" >/dev/null 2>&1 || true
  if claude plugin install "${mk}@${mk}" >/dev/null 2>&1; then
    say "- ${mk}: installed from ${src}"
  else
    note "- ${mk}: **FAILED** to install from ${src} -- the plugin is in the"
    say "  image but not in this home, and its DEFAULT_MODE env var still"
    say "  claims it is configured"
  fi
done

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
# CREDENTIALS BEFORE THE CLONE THAT NEEDS THEM. This wiring existed, ~150 lines
# BELOW -- so every private clone ran unauthenticated, hit "could not read
# Username for 'https://github.com'", and died on a terminal nobody was watching.
# GITHUB_TOKEN was present the whole time. A public repo would have hidden this
# forever. The wiring is not new; its POSITION is the fix.
# Unattended git: permission mode silences the PROMPTS, but these are what
# actually fail a clone, a commit or a push, and no permission setting fixes
# them. Kept TOGETHER -- hoisting half an identity block is how the next reader
# concludes the other half was deliberate.
git config --global --get user.name >/dev/null 2>&1 || \
  git config --global user.name "${REVEILLE_GIT_NAME:-${REVEILLE_AGENT_ROLE}}"
git config --global --get user.email >/dev/null 2>&1 || \
  git config --global user.email "${REVEILLE_GIT_EMAIL:-${REVEILLE_AGENT_ROLE}@reveille.local}"
git config --global --get safe.directory >/dev/null 2>&1 || \
  git config --global --add safe.directory '*'
# gh reads GH_TOKEN first; the P2 profile supplies GITHUB_TOKEN. gh as git's
# credential helper is what makes clone AND push over HTTPS non-interactive.
if [ -n "${GITHUB_TOKEN:-}" ]; then
  export GH_TOKEN="${GH_TOKEN:-$GITHUB_TOKEN}"
  if gh auth setup-git >/dev/null 2>&1; then
    say "- git credentials: wired from GITHUB_TOKEN"
  else
    note "- git credentials: **FAILED** to wire despite GITHUB_TOKEN being set;"
    say "  private clones and pushes will prompt and fail"
  fi
fi

if [ -n "${REVEILLE_REPO_URL:-}" ] && [ -z "$(ls -A /home/agent/repos 2>/dev/null)" ]; then
  if git clone "${REVEILLE_REPO_URL}" /home/agent/repos/work 2>/tmp/clone.err; then
    say "- cloned ${REVEILLE_REPO_URL} -> ~/repos/work"
  else
    note "- clone of ${REVEILLE_REPO_URL} **FAILED**."
    # THE FAILURE AND THE STATE THAT CAUSED IT. "clone failed, clone by hand"
    # cost two people an hour suspecting the token, when the token was fine and
    # the credential helper simply was not wired yet. A failure without the state
    # around it sends the reader to the likeliest suspect, which is not the same
    # thing as the cause.
    say "  git credential helper at clone time:"
    if git config --global --get-regexp '^credential\..*\.helper' >/dev/null 2>&1; then
      git config --global --get-regexp '^credential\..*\.helper' \
        | sed 's/^/      /' >> "$BOOT_REPORT" 2>/dev/null || true
    else
      say "      (none configured)"
    fi
    say "  GITHUB_TOKEN: $([ -n "${GITHUB_TOKEN:-}" ] && echo present || echo absent)"
    say ""
    say "  git said:"
    say ""
    sed 's/^/      /' /tmp/clone.err >> "$BOOT_REPORT" 2>/dev/null || true
    say ""
    say "  Your ~/repos is empty for this reason, not because you have no repo."
    say "  Clone by hand once the cause above is fixed."
  fi
  rm -f /tmp/clone.err
elif [ -n "${REVEILLE_REPO_URL:-}" ]; then
  say "- ~/repos already had content; clone skipped"
else
  say "- no REVEILLE_REPO_URL was passed; nothing to clone"
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

# home-login mode (launcher mounts the user's login home read-only at
# /run/reveille-auth): COPY the credentials file into this agent's own home,
# OVERWRITING, on every boot. Overwrite is the point -- the user's login home
# is the authority for WHICH account pays, and the operator's account-rotation
# workflow is "re-login once, restart the agents"; a setdefault here would pin
# every agent to whichever account it first booted under. Everything else in
# ~/.claude stays this agent's own: the login is the ONLY shared thing, by
# copy, never by shared mount -- agents sharing a home have been observed
# bleeding identity into each other, which the hive exists to prevent.
if [ -f /run/reveille-auth/.credentials.json ]; then
  install -m 600 /run/reveille-auth/.credentials.json \
    /home/agent/.claude/.credentials.json
fi

# Git identity and credentials are wired ABOVE the clone -- see that block for
# why the order IS the fix. Nothing git-related belongs down here any more.

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
