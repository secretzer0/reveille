---
description: "Join the agent message bus under a unique name and react to messages in real time (inotify-driven)."
argument-hint: "--name NAME [--fresh] -- PROMPT"
---

Join the self-organizing **agent message bus** and run a loop that reacts to messages the instant they arrive. The bus is a well-known global directory (`~/.claude/agent-bus`, override `$CLAUDE_AGENT_BUS`) — no paths to configure. Other sessions find you by your name; you watch only your own inbox plus the shared broadcast log.

**If `$ARGUMENTS` has no `--name`, do NOT start anything — print this usage and stop:**

```
/watch-loop --name NAME [--fresh] -- PROMPT

Join the agent bus as NAME and run PROMPT each time a message arrives.

  --name NAME   your unique handle on the bus (required; fails if a live agent holds it)
  --fresh       skip the broadcast backlog; only react to messages from now on
  PROMPT        what to do with received messages (after `--`)

Examples:
  /watch-loop --name reviewer -- review any diff I'm sent and reply with findings
  /watch-loop --name builder --fresh -- on each message, run the build and broadcast pass/fail

Talk to peers:  /watch-send --to NAME|--all -- MESSAGE
See who's here:  /watch-list      Leave:  /watch-stop
```

Parse `$ARGUMENTS`: `--name NAME` (required), optional `--fresh`, and the PROMPT after `--`.

Then:

1. **Join the bus.** `BUS=~/.claude/scripts/bus.py`.
   ```
   python3 ~/.claude/scripts/bus.py join --name NAME [--fresh]
   ```
   It prints two lines — your INBOX dir and the BROADCAST dir — capture both. If it exits non-zero (name held by a live agent, or invalid name), report the error and stop; do not loop.

2. **Replace any prior watcher for this session** (one loop per session) — run alone, not combined with the arm step (a combined `pkill ...; python3 ...fswatch...` self-kills, exit 144):
   ```
   pkill -f "[f]swatch.py --tag $CLAUDE_CODE_SESSION_ID" || true
   ```

3. **Drain pending** (non-destructive):
   ```
   python3 ~/.claude/scripts/bus.py pending --name NAME --json
   ```
   Returns `{inbox:[{file,msg}], broadcasts:[{file,ts,msg}], cursor_to}`.

4. **If anything is pending**, run the user's PROMPT over the message bodies (inbox + broadcasts together, as one batch). When done, **ack** what you handled — this is what moves inbox files out and advances your broadcast cursor (nothing is consumed until you ack, so a failed prompt loses nothing):
   ```
   python3 ~/.claude/scripts/bus.py ack --name NAME --consumed <inbox file paths, comma-separated> --cursor <cursor_to>
   ```
   Then go back to step 3 — messages that arrived while the prompt ran are now pending (post-eval rescan; nothing missed, no per-message storm).

5. **If nothing is pending**, arm the watcher in the background and end your turn:
   ```
   python3 ~/.claude/scripts/fswatch.py --arrivals --tag "$CLAUDE_CODE_SESSION_ID" <INBOX> <BROADCAST>
   ```
   `run_in_background: true`. `--arrivals` wakes only on new messages / move-in (not on you moving consumed files out). The watcher exits the instant a message lands; the harness re-invokes you → resume at step 3. (A broadcast you sent yourself may wake the watcher; `pending` filters your own broadcasts out, so it's a harmless extra cycle.)

**Stopping:** `/watch-stop` (leaves the bus + kills this session's watcher). Esc alone does NOT stop it — the background watcher keeps running and re-wakes you.

Notes:
- You never write to your own inbox, and consumed messages move to `processed/` (a move-out, ignored by `--arrivals`) — so your own activity can't trigger the loop. Only real arrivals do.
- To reply or message peers during the prompt, call `/watch-send` (or `bus.py send`) — see that command.
