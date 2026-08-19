# DES-020: A body runs its broker's code -- the toolchain converges by itself

Status: RULED 2026-08-19; slice 1 = #138 (0.2.185, waked converge); slice 2 = s6 guard + s7 visibility + uv bootstrap + source-shape measurement (12141). (operator 12126, 12134 source = main: "the MCP needs to upgrade itself as it
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

## 3. Source: main at the org repo, never a local copy (operator 12134)

- Source is the one `reveille init` already persists with: `GIT_SOURCE =
  git+https://github.com/secretzer0/reveille` (cli.py) -- our org repo over
  TLS, never "whatever answers a URL" (devops 12127 risk 1), and NEVER a
  checkout on the body's disk: a pulled copy is whatever branch someone was
  reviewing (the launcher learned this at 8568).
- **main IS the release.** The operator's contract (12134): code on main has
  been tested and has a matching broker deployed live -- main and the broker
  move in lock step. So the body installs main HEAD, no tags, no pin:
  `uv tool install --force --from <GIT_SOURCE> reveille`. Non-interactive by
  construction; uv and git are present natively (init required them) and in
  the image (Dockerfile installs via `uv tool install`).
- **One line installs AND upgrades** (operator 12146): `curl -LsSf
  https://raw.githubusercontent.com/secretzer0/reveille/main/install.sh | sh`
  -- repo-root `install.sh`: uv missing -> astral's installer; `uv tool
  install --force --from <GIT_SOURCE> reveille`; `reveille init "$@"`.
  Idempotent; run it again any time. Windows twin `install.ps1` (DES-021).
  Replaces the README's `uvx --from git+...` line. Slice 2.
- **Comparison is `installed < broker`** (devops 12131). A body AHEAD of the
  broker is the normal state for the minutes between a merge and its deploy
  and is converged, not pathological; `!=` against a moving HEAD would
  reinstall every hour forever.
- **Record what was installed.** `git ls-remote <repo> main` (one call, no
  clone) gives the sha the install resolved; converge.json keeps it. That sha
  is what s6 falls back to -- `--from <GIT_SOURCE>@<sha>` -- so a rollback
  needs no tag, only memory of the last body that attached.

## 4. Trigger: waked, before it dials, once an hour (as built, #138)

- The check runs in **reveille-waked**, at the top of its reconnect loop,
  native AND container alike -- waked runs in both, so one code path. Rate:
  `UPGRADE_INTERVAL_S = 3600`, burned even when the probe throws (a broker
  outage must not become a probe-per-reconnect storm). GET /version, 5 s,
  unauthenticated; unreachable or unparsable = nothing.
- The WHOLE call is shielded: it sits inside the wake loop, and an exception
  escaping there kills the wake path -- going deaf to fix a version number is
  never the trade. Every failure = one stderr line, old code keeps running.
- NOT the Stop hook (its header rule, 8573: anything slow or remote at a turn
  boundary costs the session); NOT wake-watch (stateless, secretless). The
  architect's first cut (12132) put it in the hook for turn-boundary safety;
  the built waked shape was accepted at 12141: one long-lived process that
  already dials the broker and can fail open with nobody waiting. The
  mid-turn rewrite hazard (devops 12127 c) is bounded by the hour rate and
  the install's seconds-long window; accepted.
- The container entrypoint may call the same check once at start (free of any
  in-flight hazard); not required, since waked starts with the agent.

## 5. Restart: an upgrade that restarts nothing changed nothing

- On an install whose version MOVED, waked `os.execv`s itself via the console
  script path (`shutil.which("reveille-waked")`, the durable ~/.local/bin
  entry -- never `-m reveille.waked`, which leaves sys.argv[0] a module file
  and breaks the next hour's probe): env carries the credential, the flock is
  retaken, a ring landing mid-swap waits in the spool and fires at the next
  arm.
- Did not move (exit 0, same version) = log once, no re-exec, no retry until
  the broker's version changes.
- Hook, cli, headers, upload, wake-watch are fresh processes: new code on
  their next invocation. The claude session is untouched (MCP = HTTP).

## 6. Unattended means it must survive its own failure (devops 12127 risks 3/4)

- **One attempt per target version per hour.** State in
  `~/.reveille/converge.json`: `{installed, sha, last_good: {version, sha},
  target, tried_ns, bad: [..]}`. `last_good` = the version+sha whose waked
  ATTACHED (s7 reports it).
- **Did not move = hold** (devops 12131): an install that exits 0 but leaves
  the version unchanged is logged once and not retried until the broker's
  version CHANGES. A failed-but-silent install must not become a storm.
- **Crash-loop guard:** if, within 10 min of converging, waked has not
  attached (no `last_good == installed` mark), the next hook run reinstalls
  `last_good.sha`, appends the target to `bad`, and will not try that target again
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
  minimum 0.2.179; run `uv tool upgrade reveille`"}`
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
- Slice 1 (#138): waked converge + `<` + did-not-move hold + tests. Slice 2
  (own PR): converge.json guard + sha rollback; hook block + entrypoint call; waked `?v=`
  + headers version; broker `agents_seen.version` + `behind` + MIN_BODY_VERSION
  (initial value = the version that ships this, so nothing live is refused);
  one test per: converge decision table (equal skips, behind installs,
  AHEAD skips, unreachable fail-open, did-not-move hold, rollback to sha),
  too_old refusal.
- Gate (architect verifies live): laptop deliberately pinned one release back
  -> next turn boundary converges + waked respawns on new code, one log line;
  container same; broker row shows `behind` then clears; a version below
  MIN_BODY_VERSION is refused by name.
