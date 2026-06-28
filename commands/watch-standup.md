---
description: "Join the standup bus as a role and coordinate by DIRECTED messages; broadcast only when most peers care or after a silence window."
argument-hint: "--name ROLE [--fresh] [--silence MIN] | --round"
---

Standup coordination on the agent bus. Format + messaging policy are baked in — you pass a role.

`BUS=~/.claude/scripts/bus.py`, `FSWATCH=~/.claude/scripts/fswatch.py`.

**STATUS line (always exactly this shape):**
`NEED:<from peers> BLOCKED-BY:<who/what blocks you> I-BLOCK:<who waits on you> OPEN:[<tasks>] CLOSED:[<tasks>]`
Source it from your live task list (TaskList tool / your working todos).

## MESSAGING POLICY — directed by default, broadcast by exception

A **broadcast wakes EVERY agent** (each runs a full turn to decide it's not for them) — that is N turns and a lot of tokens. A **unicast wakes one** agent. So:

- **DEFAULT = unicast.** If you need something from, are blocking, or are unblocking a *specific* peer, message that peer: `send --from ROLE --to <peer> --body "<msg>"`. This covers almost everything. Do NOT broadcast your STATUS just because it changed — tell the one peer who cares.
- **BROADCAST (`--all`) ONLY when:**
  - **(A) most/all agents are affected** — a shared contract/API change, a release, something everyone must act on (one broadcast beats N unicasts); or a `STANDUP-REQUEST`; or
  - **(B) silence heartbeat** — the bus has been quiet for the silence window (default 30 min): post your full STATUS once, as liveness + state refresh.
- Never broadcast on every turn. If in doubt, unicast the specific peer, or stay quiet.

---

### If `$ARGUMENTS` is `--round`
Broadcast a standup request, then STOP (do not join, do not loop):
```
FROM=$(python3 ~/.claude/scripts/bus.py whoami) && python3 ~/.claude/scripts/bus.py send --from "$FROM" --all --subject standup --body "STANDUP-REQUEST"
```
If `whoami` errors, tell the user to join first and stop.

### Otherwise require `--name ROLE` (optional `--fresh`, `--silence MIN`). If no `--name`, print usage and stop:
```
/watch-standup --name ROLE [--fresh] [--silence MIN]   join as ROLE (default silence window 30 min)
/watch-standup --round                                 make everyone re-post now (one round)
```

Let `SIL = (--silence value or 30) * 60 + <len of ROLE>`  (the per-role offset staggers heartbeats so agents don't all fire at once). Then run the loop:

1. **Join:** `python3 ~/.claude/scripts/bus.py join --name ROLE [--fresh]` — capture the two printed lines (INBOX, BROADCAST). Non-zero exit (name held / invalid) → report and stop.
2. **Replace prior watcher** (alone — never combined with the arm step): `pkill -f "[f]swatch.py --tag $CLAUDE_CODE_SESSION_ID" || true`
3. **Join quietly.** Compute your STATUS from your task list and remember it as `last_status`. Do NOT broadcast on join. If you already know you need something from specific peers, unicast just them.
4. **Drain:** `python3 ~/.claude/scripts/bus.py pending --name ROLE --json`.
5. **If pending:**
   - `DIRECTIVE:LEAVE` in any message: you have been kicked — run `/watch-stop` and STOP looping.
   - Ingest each message; update your task list / dependency view. If a message asks you something, reply **unicast** to its sender: `send --from ROLE --to <sender> --body "<answer>"`.
   - `STANDUP-REQUEST` present: broadcast your full STATUS (that request means everyone should post).
   - Your STATUS changed and a *specific* peer is affected (you unblocked them / now need them): **unicast that peer**. Only broadcast if most/all are affected (policy A). Otherwise post nothing.
   - **Ack:** `python3 ~/.claude/scripts/bus.py ack --name ROLE --consumed <inbox file paths, comma-separated> --cursor <cursor_to>`. Update `last_status`.
   - Go back to step 4 (catch anything that arrived mid-turn).
6. **If nothing pending:**
   - If this wake was a **silence timeout** (the watcher background task exited with code 2 — no message for SIL seconds): broadcast your full STATUS once (heartbeat, policy B).
   - Arm the watcher and end your turn (`run_in_background: true`):
     `python3 ~/.claude/scripts/fswatch.py --arrivals --timeout SIL --tag "$CLAUDE_CODE_SESSION_ID" <INBOX> <BROADCAST>`
   - On wake → step 4.

Helpers: **UNICAST** = `send --from ROLE --to <peer> --body "<msg>"`. **BROADCAST** = `send --from ROLE --all --body "<msg>"` (policy A or B only).

Stop with `/watch-stop`.
