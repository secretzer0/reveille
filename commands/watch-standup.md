---
description: "Join the standup bus as a role and auto-coordinate: post status, react to peers, reply to asks. Storm-guarded."
argument-hint: "--name ROLE [--fresh] | --round"
---

Standup coordination on the agent bus. Format + changed-only guard are baked in — you only pass a role.

`BUS=~/.claude/scripts/bus.py`, `FSWATCH=~/.claude/scripts/fswatch.py`.

**STATUS line (always exactly this shape):**
`NEED:<from peers> BLOCKED-BY:<who/what blocks you> I-BLOCK:<who waits on you> OPEN:[<tasks>] CLOSED:[<tasks>]`
Source it from your live task list (TaskList tool / your working todos).

---

### If `$ARGUMENTS` is `--round`
Broadcast a standup request, then STOP (do not join, do not loop):
```
FROM=$(python3 ~/.claude/scripts/bus.py whoami) && python3 ~/.claude/scripts/bus.py send --from "$FROM" --all --subject standup --body "STANDUP-REQUEST"
```
If `whoami` errors, tell the user to join first (`/watch-standup --name <role>`) and stop.

### Otherwise require `--name ROLE` (optional `--fresh`). If no `--name`, print usage and stop:
```
/watch-standup --name ROLE [--fresh]   join as ROLE, post status, react to peers
/watch-standup --round                 make everyone re-post now (one round)
```

Then run the loop:

1. **Join:** `python3 ~/.claude/scripts/bus.py join --name ROLE [--fresh]` — capture the two printed lines (INBOX, BROADCAST). Non-zero exit (name held by a live agent / invalid) → report and stop.
2. **Replace prior watcher** (alone — never combined with the arm step): `pkill -f "[f]swatch.py --tag $CLAUDE_CODE_SESSION_ID" || true`
3. **Post initial status once:** build the STATUS line from your task list, POST it (below), remember it as `last_status`.
4. **Drain:** `python3 ~/.claude/scripts/bus.py pending --name ROLE --json`.
5. **If pending:**
   - If any message body is `DIRECTIVE:LEAVE`: you have been kicked. Run `/watch-stop` (leave the bus + kill your watcher) and STOP looping. Do nothing else.
   - Ingest each message: update your task list / dependency view. If a message is addressed to you and asks for something, reply: `python3 ~/.claude/scripts/bus.py send --from ROLE --to <sender> --body "<answer>"`.
   - If any message body is `STANDUP-REQUEST`: POST your STATUS now even if unchanged.
   - Otherwise POST your STATUS **only if it changed** vs `last_status`. Unchanged → post nothing. (This is the storm guard — do not skip it.)
   - **Ack:** `python3 ~/.claude/scripts/bus.py ack --name ROLE --consumed <inbox file paths, comma-separated> --cursor <cursor_to>`.
   - Go back to step 4 (catch anything that arrived mid-turn).
6. **If nothing pending:** arm the watcher in the background and end your turn:
   `python3 ~/.claude/scripts/fswatch.py --arrivals --tag "$CLAUDE_CODE_SESSION_ID" <INBOX> <BROADCAST>` (`run_in_background: true`). On wake → step 4.

**POST** = `python3 ~/.claude/scripts/bus.py send --from ROLE --all --subject status --body "<STATUS line>"`, then set `last_status` to that line.

Stop with `/watch-stop`.
