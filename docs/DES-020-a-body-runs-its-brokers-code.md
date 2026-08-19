# DES-020: A body runs its broker's code -- the toolchain converges by itself

Status: RULED 2026-08-19 (operator 12126: "the MCP needs to upgrade itself as it
starts ... no user constantly having to upgrade ... the same way inside the
container ... no prompts"; architect 12128, devops 12127 aligned here at
operator 12129). Supersedes ruling 11863 "no boot-time tool refresh" for the
TOOLCHAIN only; image pin + idle auto-roll (DES-006 s7.2, 0.2.165) unchanged
for everything image-level. Companion to DES-008 (native bodies), DES-010
(continuous delivery), DES-012 s14 (the return ticket -- the case that proved
the cost: a laptop six releases behind would have failed step 7 looking like a
DES-012 defect, 12124).

## 1. Problem

The broker ships every merge. A body -- native `uv tool install`, or the
container's copy of the same -- ships when a human remembers. 2026-08-19 the
operator's laptop was 0.2.178 against a 0.2.184 broker and nobody knew until
devops grepped the installed waked.py. The MCP itself never goes stale (it is
the broker over HTTP); what goes stale is the LOCAL toolchain: reveille-waked,
the Stop hook, wake-watch, reveille-headers, reveille-upload, the cli. A human
gate that catches a bad release is the same gate that leaves a good one
uninstalled for six releases.

## 2. Binding: one invariant

**A body runs the code its broker runs.** The broker's `GET /version` is the
truth -- a human deployed it deliberately; it is never ahead by accident. A body
whose installed version differs CONVERGES to it, by itself, with no prompt, on
every host the same way. Nothing else decides, nothing else checks.

## 3. Source: named, authenticated, exact

- Source is the one `reveille init` already persists with: `GIT_SOURCE =
  git+https://github.com/secretzer0/reveille` (cli.py) -- our org repo over
  TLS, never "whatever answers a URL" (devops 12127 risk 1).
- **Every version is a tag.** The publish workflow tags `v<version>` on main
  after the image push (same job, same guard). The body installs
  `--from <GIT_SOURCE>@v<broker-version>` -- EXACT match, so a broker rollback
  is followed down, not only up, and a fallback (s6) is the same command with a
  different tag. A broker version with no tag (pre-DES-020 history) = fail-open,
  one log line, no install.
- Command: `uv tool install --force --from <GIT_SOURCE>@v<ver> reveille`.
  Non-interactive by construction; uv and git are present natively (init
  required them) and in the image (Dockerfile installs via `uv tool install`).

## 4. Trigger: the turn boundary, one block, same everywhere

- The check runs in the **Stop hook** (`agent-stop-hook`), native AND
  container -- the image already ships that hook; no second mechanism. The
  container entrypoint calls the SAME function once before the agent starts
  (a start is a boundary where nothing runs yet, devops 12127).
- The hook's own rule stands: the top of the hook never probes the broker.
  This is a BLOCK BELOW the supervision blocks that probes INSIDE itself,
  3 s timeout, and fails open -- exactly the carve-out the hook's header
  names. Broker down = no opinion = no change.
- NOT at waked connect (a reconnect happens mid-turn -- a deploy restarts
  the broker exactly then -- and rewriting the package under a live session
  is "changed the code you are running, mid-turn, without telling you",
  devops 12127 (c)); NOT in wake-watch (stateless and secretless by design,
  keep it so). The Stop boundary is the point where nothing of ours runs
  except waked and the watcher, both ours to restart.
- Logic lives in Python -- `reveille converge` (cli subcommand; hook and
  entrypoint are glue) -- so it is testable and the bash stays bash.

## 5. Restart: an upgrade that restarts nothing changed nothing

- After a successful install the hook kills waked by the pid waked already
  writes into `$spool/.lock`, and the spawn block below it -- unchanged --
  starts the NEW daemon in the same hook run. Deaf window ~0, at a boundary.
- Hook, cli, headers, upload, wake-watch are fresh processes: new code on
  their next invocation. An old wake-watch in flight reads a directory;
  harmless. The claude session is untouched (MCP = HTTP to the broker).
- `reveille converge` prints ONE line per action to waked.log and stderr:
  `converged 0.2.178 -> 0.2.184` / `converge skipped: <why>`. Silence is the
  defect (12008).

## 6. Unattended means it must survive its own failure (devops 12127 risks 3/4)

- **One attempt per target version per hour.** State in
  `~/.reveille/converge.json`: `{installed, last_good, target, tried_ns,
  bad: [..]}`. `last_good` = the version whose waked ATTACHED (s7 reports it).
- **Crash-loop guard:** if, within 10 min of converging, waked has not
  attached (no `last_good == installed` mark), the next hook run reinstalls
  `last_good`, appends the target to `bad`, and will not try that target again
  until the broker's version CHANGES. Same shape as the TTS watchdog's reboot
  guard (da5b3015), same reason: a self-healer that loops is worse than one
  that stops.
- **It reports.** A body with no human watching has one channel left, the
  bus: the broker learns the body's version at attach (s7), so a rolled-back
  body is VISIBLE as "behind" on its row, and the hook's reinstall line says
  `rolled back 0.2.185 -> 0.2.184: waked did not attach in 10 min`. No bus
  POST from the hook -- the hook carries no token and must not grow one; the
  join is the report.

## 7. The broker sees, and refuses what is too old to be correct (devops ask)

- waked sends its version on the wake attach (`?v=<ver>`), the MCP headers
  helper sends `X-Reveille-Version`. `agents_seen` stores it; the agents pane
  shows `behind` beside a body whose version != broker (like `moving`,
  `bodyless`: a state, not a fault).
- `MIN_BODY_VERSION` (daemon constant, bumped DELIBERATELY with a CHANGES line
  when a wire or credential contract moves -- never on every release) gates
  attach and join: below it = `{"error":"too_old","detail":"body 0.2.178 <
  minimum 0.2.179; run `reveille converge` or `uv tool upgrade reveille`"}`
  and close 4426. A loud refusal at join, never six passing steps and a
  seventh that looks like a design defect. Belt and braces that fail in
  different directions: s2-s6 keep a body current; s7 names the one that is
  not.

## 8. Out of scope

- claude binary, node, OS packages: image pin + idle auto-roll, unchanged.
- The launcher (`reveille-launch`): pinned clone, `pin` fast-forwards main;
  unchanged here.
- A body's own checkout in ~/repos: never touched.

## 9. Order and acceptance

- Red-shirt chain (12123/12125) runs FIRST on a one-time manual
  `uv tool upgrade reveille`; this DES does not block it and is not tested by
  it.
- One PR, devops builds, bump rides: tag step in publish.yml; `reveille
  converge` + converge.json + guard; hook block + entrypoint call; waked `?v=`
  + headers version; broker `agents_seen.version` + `behind` + MIN_BODY_VERSION
  (initial value = the version that ships this, so nothing live is refused);
  one test per: converge decision table (skip/install/hold/rollback),
  too_old refusal.
- Gate (architect verifies live): laptop deliberately pinned one release back
  -> next turn boundary converges + waked respawns on new code, one log line;
  container same; broker row shows `behind` then clears; a version below
  MIN_BODY_VERSION is refused by name.
