#!/usr/bin/env bash
# Register the bus, then run the agent inside tmux session "agent" with the ttyd
# sidecar serving the browser plane (DES-002 T1). The registration is the same
# `reveille init` a laptop runs -- same installer, same three artifacts. If this
# ever needs something a laptop cannot do, the container has become special and
# that is the bug.
set -euo pipefail

: "${REVEILLE_AGENT_ROLE:?set REVEILLE_AGENT_ROLE (your bus name)}"
: "${REVEILLE_TOKEN:?set REVEILLE_TOKEN (your bus credential)}"
: "${REVEILLE_URL:=http://127.0.0.1:8765}"

# ---- R1: NOTHING BEFORE waked MAY EXIT THE ENTRYPOINT (architect 12851; -----
# hoisted per 12882). The wake socket holder (DES-003 2.1): one daemon, one
# attachment, rings become spool files. In a container the entrypoint is the
# supervisor -- it dies and restarts with the container; the flock makes a
# double-start a no-op. Token rides this shell's env, never argv.
#
# IMMEDIATELY AFTER THE `:?` CHECKS, AND NOWHERE LOWER. 12851 R1 says "before
# ANYTHING that can refuse"; the first hoist put it "before the one step we
# already know refused", which left every slow or fatal step in front of the
# daemon to be discovered in the field (ruled 12882). The `:?` checks stay in
# front -- they are not a step before the daemon, they are the daemon's own
# inputs: a boot with no REVEILLE_TOKEN has nothing for waked to hold, and
# failing loudly there is correct. waked needs only these three values, never
# init's artifacts; it parks on a spent secret and trades it for a live one at
# the return ticket (DES-012 s14), so putting it behind anything that can
# refuse makes recovery depend on the very thing that failed. The trade,
# accepted at ruling: waked.log is the daemon's evidence -- the boot report
# below does not exist yet at this point, and that is fine.
ws_url="${REVEILLE_URL/http/ws}/wake"
mkdir -p "$HOME/.reveille/spool/${REVEILLE_AGENT_ROLE}"
# `|| true`: A SUPERVISOR THAT DIES WITH ITS CHILD IS NOT A SUPERVISOR
# (ruling 13094). The subshell inherits this file's `set -euo pipefail`, so a
# waked that exits non-zero -- a crash, a SIGTERM's 143 -- used to take the
# whole loop with it: one "Terminated" in docker logs and no respawn, ever.
# The loop's entire purpose is to outlive the thing it starts.
( while :; do
    reveille-waked --url "$ws_url" --name "$REVEILLE_AGENT_ROLE" \
      >>"$HOME/.reveille/spool/${REVEILLE_AGENT_ROLE}/waked.log" 2>&1 || true
    sleep 2
  done ) &

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
# THE ROLL RECORD REACHES A READER (rulings 13273/13335): the launcher writes
# .claude/roll-record.md BEFORE it replaces a body -- what rolled, when, and
# the observed state of the tree, read from the host so it works even when
# the old body was stopped. A record nobody is told about is a file; this
# line is what makes it a mechanism.
if [ -f /home/agent/.claude/roll-record.md ]; then
  # THE POINTER CARRIES THE RECORD'S OWN TIMESTAMP (13340 rider): the file is
  # never cleared -- deliberately, a body that took no turns must not lose
  # the only account of what it lost -- so a bare present-tense sentence
  # would be printed forever and read as current. The observed `- when:` is
  # what lets a reader judge freshness instead of trusting a tense.
  rolled_when="$(grep -m1 '^- when: ' /home/agent/.claude/roll-record.md | cut -c9-)"
  say "- THIS BODY WAS ROLLED${rolled_when:+ at ${rolled_when}}: read"
  say "  ~/.claude/roll-record.md -- the launcher's observation of what the"
  say "  previous body left (dirty files, unpushed commits), taken before the"
  say "  old container was replaced"
fi
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
if [ -f "$HOME/.claude/.reveille-cred-report" ]; then
  say "$(cat "$HOME/.claude/.reveille-cred-report")"
elif [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ] || [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  say "- claude credential: from the environment"
else
  note "- claude credential: **MISSING** -- claude will stop at a login prompt"
fi
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
  if ! command -v claude >/dev/null 2>&1; then
    # Not FAILED: there is no claude to install into. The old wording sent a
    # reader chasing the plugin while the binary was the missing thing.
    note "- ${mk}: skipped -- no claude binary on PATH"
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

# ONE INSTALLER, BOTH SHAPES (operator directive 2026-08-15). This block used
# to be the pre-0.2.90 registration -- user-scope, with LITERAL
# Authorization/X-Agent headers -- while every laptop had moved to per-directory
# .mcp.json + headersHelper (0.2.91). Two patterns meant every registration fix
# shipped twice or drifted, and a literal header bakes a superseded token into
# the config until the next recreate; the helper reads the credential fresh on
# every connect. `reveille init` writes the same three artifacts here as on any
# laptop: ~/repos/.mcp.json (registration, headersHelper), ~/repos/.claude/
# settings.local.json (credential, converged on re-provision), and the Stop
# hook + mcp__reveille allow in ~/.claude/settings.json. It also converges away
# a stale user-scope registration a pre-cutover home may still carry. The
# container-only seeds below (bypassPermissions, onboarding, trust) are NOT
# init's business and stay exactly as they are.
#
# init VERIFIES the credential against the broker before writing anything; a
# boot that races the broker must still come up configured -- the values are
# the launcher's own provision env, correct by construction, and waked retries
# the bus forever -- so the retry carries --force. A SECOND FAILURE NO LONGER
# EXITS (ruling 12851 R1): waked is already up by the time either invocation
# runs, the body stays reachable and recallable unregistered, and the boot
# report plus a DEGRADED row carry the reason. This sentence used to end "and
# set -e makes it loud" -- it was true until the daemon moved in front of it,
# and a comment that outlives its code is the defect R3 is about.
# THE REPORT SAYS WHAT VERIFY SAID, never a cause this script did not
# establish (architect BLOCKING 1, msg 10875). init exiting non-zero covers
# two different worlds -- "the broker answered and refused this token" and
# "the broker did not answer" -- and cli.verify() already speaks the
# difference on stderr; discarding it left the one reader who cannot reach
# docker logs holding the wrong half. Mint-supersede is the migration
# mechanism between shapes now, so a REFUSED credential at boot is a state we
# deliberately create, and a report saying "network" at that moment aims the
# re-provision at the wrong thing.
mcp_force_note() {
  note "- mcp: registered via reveille init --force -- the credential is UNVERIFIED"
  say "  init said:"
  # THE WHOLE CAPTURED OUTPUT, INDENTED -- never one line of it (ruling 12944
  # R-B b2). Under 2>&1 capture the stream's ORDER is the buffering's, not the
  # cause's: the field run put the true verdict ("credential no longer works,
  # HTTP 401") LAST while the first line said "no sign-in stored", and quoting
  # by position handed the reader the wrong remedy twice. No line-position
  # read survives anywhere in this file; the boot report is the one surface a
  # body can read about its own broken boot (8691), and truncating it is how
  # the body learned the wrong sentence.
  printf '%s\n' "$1" | sed 's/^/      /' >> "$BOOT_REPORT"
  say "  waked will keep retrying the bus"
}
# The wake socket holder is spawned at the TOP of this file, immediately after
# the `:?` checks -- see the R1 block there (12851, hoisted by 12882).
# GENERALISED (ruling 12851 R1, extending 12401): every step from there down
# may mark DEGRADED, and none of them may exit.

BOOT_DEGRADED=""
# Its own heading: these lines are the REGISTRATION story, and they used to
# print under "## plugins" while "## repo" stood empty two sections up --
# an empty heading reads as MISSING to exactly the reader the preamble
# addresses (red-shirt, 12908).
say ""
say "## mcp"
say ""
# R3: STDOUT IS CAPTURED TOO. This read `2>&1 >/dev/null`, which keeps stderr and
# throws stdout away -- and the sentence naming the actual cause,
# "this directory's credential no longer works (HTTP 401 ...)", is a print(), so
# it went to /dev/null on both invocations. docker logs and this report were left
# holding the refusal's SECOND sentence ("no sign-in stored"), which sends the
# reader to `reveille login` for a credential problem. The comment above already
# promised the report says what verify said; now it does.
init_rc=0
init_said="$(reveille init --no-prompt --dir /home/agent/repos 2>&1)" || init_rc=$?
if [ "$init_rc" -eq 0 ]; then
  if printf '%s' "$init_said" | grep -q "DEGRADED"; then
    # Ruling 12401: a configured directory with the claude binary transiently
    # absent BOOTS, degraded and saying so -- a crash-looping entrypoint was
    # how a bricked claude (interrupted self-update) turned one bad stop into
    # a container that could never come back. The row reads this through the
    # same file the repo status rides; no new state, no new file.
    # The whole output, same rule as mcp_force_note (12944 R-B b2): no
    # line-position read survives anywhere in this file.
    note "- mcp: DEGRADED -- init said:"
    printf '%s\n' "$init_said" | sed 's/^/      /' >> "$BOOT_REPORT"
    BOOT_DEGRADED="failed: claude binary missing -- init ran degraded; re-provision this container (an interrupted claude self-update leaves no binary)"
  else
    # THE GOOD PATH SAYS A FACT, NOT NOTHING (red-shirt 12908): a boot inside
    # the post-supersede handover grace also lands here with a clean rc, so
    # silence-means-verified was unreadable. The line states what was
    # OBSERVED -- init's exit code and when -- never a "verified" this script
    # did not establish.
    say "- mcp: registered via reveille init (project scope, headersHelper) in ~/repos -- init exit 0 at $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  fi
else
  force_rc=0
  force_said="$(reveille init --no-prompt --force --dir /home/agent/repos 2>&1)" || force_rc=$?
  if [ "$force_rc" -eq 0 ]; then
    mcp_force_note "$init_said"
  else
    # R1 AGAIN, AT ITS SHARPEST: init has now failed twice and the boot CARRIES
    # ON. waked is already up; the agent may be unregistered and still be
    # reachable, recallable and able to say what is wrong with it. An exit here
    # would take all three away.
    note "- mcp: **FAILED** -- reveille init refused twice; this agent is not"
    say "  registered with the bus in ~/repos. waked is running regardless, so"
    say "  the return ticket and every ring still reach this container."
    say "  init said (first run):"
    printf '%s\n' "$init_said" | sed 's/^/      /' >> "$BOOT_REPORT"
    say "  init said (--force):"
    printf '%s\n' "$force_said" | sed 's/^/      /' >> "$BOOT_REPORT"
    # R4: the row says DEGRADED, and the reason names the CREDENTIAL -- not the
    # repo, which is what that field usually carries. Same file, same state, no
    # new machinery; a repo failure still outranks it below.
    if printf '%s' "$init_said$force_said" | grep -q "no longer works"; then
      BOOT_DEGRADED="failed: the broker REFUSED this container's credential -- it is superseded or revoked. The body is reachable and unregistered; beam it back (the return ticket) or re-provision"
    else
      BOOT_DEGRADED="failed: reveille init refused twice -- see boot-report.md; the body is reachable and unregistered"
    fi
  fi
fi

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

# R2 (ruling 11938/11942): GUARD ON THE TARGET, NOT THE PARENT'S EMPTINESS.
# This asked whether ~/repos was empty -- and `reveille init --dir
# /home/agent/repos` runs ABOVE, writing .mcp.json and .claude/ into exactly
# that directory. So by the time this ran the answer was always "not empty",
# the clone was SKIPPED on every boot that had a repo URL, and the report said
# "already had content" as though a human had put it there. red-shirt came up
# with a repo URL and no repo and nothing said otherwise. The question this
# means to ask is whether the WORK TREE exists.
say ""
say "## repo"
say ""
REPO_STATUS=none
if [ -n "${REVEILLE_REPO_URL:-}" ] && [ ! -e /home/agent/repos/work ]; then
  if git clone "${REVEILLE_REPO_URL}" /home/agent/repos/work 2>/tmp/clone.err; then
    # THE SHA, NOT JUST THE FACT (r2). "cloned" is a claim; a sha is evidence,
    # and it is what tells the reader whether the tree is the one they meant.
    REPO_SHA="$(git -C /home/agent/repos/work rev-parse --short HEAD 2>/dev/null || echo unknown)"
    REPO_STATUS=ok
    say "- repo: ${REVEILLE_REPO_URL} @ ${REPO_SHA} -> ~/repos/work"
  else
    REPO_STATUS="failed: $(head -1 /tmp/clone.err 2>/dev/null | tr -d '\n' | cut -c1-160)"
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
  REPO_SHA="$(git -C /home/agent/repos/work rev-parse --short HEAD 2>/dev/null || echo unknown)"
  REPO_STATUS=ok
  say "- repo: ${REVEILLE_REPO_URL} @ ${REPO_SHA} -> ~/repos/work (already present)"
else
  say "- no REVEILLE_REPO_URL was passed; nothing to clone"
fi
# A FACT THE LAUNCHER CAN READ. The boot report is prose for a human; the pane
# needs one word, and health that ignores the repo is the hole r2 names -- an
# agent whose repo never arrived looked identical to one that never wanted a
# repo. The data root is bind-mounted, so this file is the launcher's window.
# UNDER ~/.claude, BECAUSE THAT IS WHAT IS MOUNTED (found 2026-08-19): when the
# home mount became two subdir mounts this file kept landing on container-local
# FS, so the launcher read a host path nothing wrote and repo failures could
# never show state=degraded. A degraded INIT rides the same file -- the row has
# one degraded reason, and a repo failure outranks it only because it is rarer.
if [ -n "$BOOT_DEGRADED" ] && [ "$REPO_STATUS" = ok ]; then
  REPO_STATUS="$BOOT_DEGRADED"
fi
printf '%s\n' "$REPO_STATUS" > /home/agent/.claude/.reveille-repo-status 2>/dev/null || true

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


def patch(path, updates, converge=None):
    """setdefault-merge one JSON file, atomically. Only ABSENT keys are written:
    an agent that changed a setting keeps its choice across restarts.

    converge: keys written UNCONDITIONALLY, replacing whatever is present.
    Reserved for values that are a reachability contract, where present-but-
    wrong is fatal and silent -- a persisted settings.json carrying a stale
    Stop hook otherwise keeps it across every re-provision, and re-provisioning
    is the one remedy anyone reaches for. Same rule install.py's hook half
    already enforces: converge on correctness, never on presence. Dicts
    recurse (sibling keys survive); anything else is assigned, and a wrong-
    TYPE intermediate is replaced rather than crashed on."""
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
    def assign(dst, src):
        for k, v in src.items():
            if isinstance(v, dict):
                if not isinstance(dst.get(k), dict):
                    dst[k] = {}
                assign(dst[k], v)
            else:
                dst[k] = v
    if converge:
        assign(d, converge)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(d, indent=2))
    os.replace(tmp, path)   # atomic: a torn config is a wizard on next boot


patch(home / ".claude.json", {
    "hasCompletedOnboarding": True,
    "theme": "dark",
    "autoMode": True,
    "autoModeOptInDismissed": True,
},
      # Workspace trust CONVERGES -- it is a bus-reachability contract, and
      # present-but-False is the silent-fatal case converge exists for (red-shirt
      # 2.1.240). reveille init writes projects["/home/agent/repos"] first with
      # the key already False, so a setdefault refused to change it; through claude
      # 2.1.237 the sibling /home/agent (a trusted parent) still ran the local
      # headersHelper, but claude 2.1.238 counts only the exact started folder
      # (mcp.md "Trust a folder before its headersHelper runs"), so the helper
      # stopped running and the reveille MCP connect carried no auth header.
      converge={"projects": {p: {"hasTrustDialogAccepted": True}
                             for p in ("/home/agent/repos", "/home/agent")},
                # The auto-mode-as-default OFFER (claude 2.1.237+): a modal that
                # proposes switching bypass to auto, shown even with autoMode and
                # autoModeOptInDismissed present -- the client RESET the old
                # dismissal (hasResetAutoModeOptInForDefaultOffer) to re-ask.
                # Found live on red-shirt 2026-08-22: answered "No, keep bypass
                # permissions" once and diffed the home; this is the key the
                # client wrote. Converged for the same reason as trust: any
                # modal is a hang on a terminal nobody watches, and upstream
                # has now twice invalidated a present value.
                "hasSeenAutoDefaultNudge": True})
# settings.json lives in the PERSISTED home. The permission MODE, the
# bypass-prompt skip, and the Stop hook all CONVERGE -- forced on every boot,
# never setdefault -- because each is a contract the operator sets for the
# containers they own and a present-but-wrong value is silently fatal. A body
# that boots into a permission modal, or unarmed, is a body nobody is watching
# that never reaches the bus (reveille-senior-ui-ux ended a turn unarmed during
# a broker blackout and sat deaf for 21 HOURS with 44 rings in its spool, on a
# bus healthy again after minutes -- msg 8573). What justifies forcing them is
# the container itself: own fs, own data root, cpu/memory/pid caps, and NO
# docker socket (that stays with the launcher), so the isolation the prompt asks
# about is the thing the agent is already inside. The operator's standing rule
# is that every container reboots wide open and prompt-free, checked and set
# right BEFORE claude launches -- so the agent's own edits do not win here.
#
# Only these keys are forced. permissions.allow is left to MERGE, so reveille
# init and the agent keep adding tool rules; in this mode the allow list is moot
# anyway. hooks.Stop is replaced whole -- other hooks the agent added ride along
# untouched. This block is the watcher backstop the image once shipped without.
patch(home / ".claude" / "settings.json", {},
      converge={"permissions": {"defaultMode": "bypassPermissions"},
                "skipDangerousModePermissionPrompt": True,
                "hooks": {"Stop": [{"hooks": [{"type": "command",
                                    "command": "/usr/local/bin/agent-stop-hook"}]}]}})
PY

# home-login credential: the LAUNCHER places it into this agent's own home
# host-side before the container runs (sync_agent_credential), choosing the
# better of the agent's own credential and the shared login seed so a restart
# or roll never overwrites a freshly-rotated one with a stale seed. There is no
# boot-time copy and no shared mount here any more.

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

# The wake socket holder is started ABOVE, before `reveille init` -- see the R1
# block there for why the ORDER is the fix. Nothing wake-related belongs here
# any more.

# Browser plane: ttyd execs `attach-gate attach` with the ?arg= token appended.
# The literal "attach" is the security boundary: -a gives the CLIENT argv, and
# only this pinned first arg keeps the trusted-side subcommands (mint, verify)
# out of the browser's reach -- without it, ?arg=mint&arg=driver is an
# unauthenticated signing oracle. -W passes client input over the wire; whether
# tmux HONORS it is the gate's exec choice (-r for viewers).
# ponytail: while-loop supervisor -- ttyd is a sidecar, its death must not take
# the agent down (4.5); replace with a real supervisor if restart churn appears.
( while :; do
    # CLIENT OPTIONS, NAMED. Every one of these was a default nobody chose.
    #
    # MEASURED TWICE, and the second measurement overturned the first fix.
    # ttyd 1.7.7's bundled client defaults to rendererType "webgl" and to
    # "Consolas,Liberation Mono,Menlo,Courier,monospace" -- so neither the DOM
    # renderer nor a bare generic font was ever in play.
    #
    # canvas did not fix it. The characters coming out as underscores were
    # captured from the live pane and are ORDINARY: em dash (U+2014) and right
    # arrow (U+2192). Not box-drawing, not exotic. Every plausible system
    # monospace has them, so the font stack was never the lever it was sold as.
    #
    # WHAT IS: canvas and webgl both rasterise from a glyph atlas measured in
    # the PRIMARY font, and their fallback for a glyph that font lacks is poor.
    # The DOM renderer emits real text nodes, so the BROWSER does per-glyph
    # fallback the way it does everywhere else on the page. Slower, and correct.
    # A terminal that renders fast and wrong is not a faster terminal.
    #
    # The font is resolved in the BROWSER, not here: this is a CSS stack against
    # the fonts on the viewer's machine, and no package installed in this image
    # can change what renders. Each entry is a system monospace that carries
    # box-drawing on its own platform, so a missing face falls to another real
    # one instead of to Courier, whose box-drawing is what comes apart.
    #
    # NOT CHANGED, deliberately: tmux's `window-size largest`. With two clients
    # attached a narrow tab sees the wider client's wrapping, which also reads
    # as corruption -- but that behaviour is documented as intentional one file
    # over, and reversing an intentional choice on a hunch is how a fix becomes
    # a regression. If artifacts survive this with only the browser attached,
    # that line is the next suspect.
    ttyd -W -a -p "${REVEILLE_TTYD_PORT:-7681}" \
      -t 'fontFamily=ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", "Liberation Mono", "Noto Sans Mono", monospace' \
      -t fontSize=13 \
      -t rendererType=dom \
      -t 'theme={"background":"#0b0d10","foreground":"#c8d0d8","cursor":"#e2a63d","selectionBackground":"#2a3340"}' \
      -t scrollback=10000 \
      -t disableLeaveAlert=true \
      attach-gate attach >/dev/null 2>&1
    sleep 1
  done ) &

echo "reveille: ${REVEILLE_AGENT_ROLE} -> ${REVEILLE_URL} (tmux: agent, ttyd: ${REVEILLE_TTYD_PORT:-7681})"

# BUS-DEAF DETECTION (architect req 13797): a body can be UP and claude-alive yet
# never reach the bus -- red-shirt sat deaf for an hour and only a screenshot
# found it. This backgrounded probe waits for claude's startup join(), then asks
# the broker whether this body holds a live session; if not, it writes BUS-DEAF
# into .reveille-repo-status so the Agents row reads deaf instead of running.
# Forked HERE, before the launch, so it survives the interactive `exec tmux`.
busdeaf-probe &

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
