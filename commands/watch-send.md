---
description: "Send a message to one agent (--to NAME) or broadcast to all (--all) on the agent bus."
argument-hint: "--to NAME | --all [--subject S] -- MESSAGE"
---

Send a message on the agent bus.

Parse `$ARGUMENTS`: exactly one of `--to NAME` or `--all`, optional `--subject S`, and the MESSAGE body after `--`.

1. Resolve your own bus name from this session:
   ```
   FROM=$(python3 ~/.claude/scripts/bus.py whoami)
   ```
   If that errors ("not joined"), you have not joined the bus — tell the user to run `/watch-standup --name ...` first (or, if they just want a one-off send, ask which name to send as and pass it as `--from`).

2. Send:
   ```
   # unicast:
   python3 ~/.claude/scripts/bus.py send --from "$FROM" --to NAME --subject "S" --body "MESSAGE"
   # broadcast to every current + future agent (persisted in the shared log):
   python3 ~/.claude/scripts/bus.py send --from "$FROM" --all --subject "S" --body "MESSAGE"
   ```
   For multi-line or shell-unsafe bodies, pipe the body on stdin and pass `--body -`.

- **Unicast** drops the message into NAME's inbox (atomic move-in) — wakes their loop immediately.
- **Broadcast** appends to the shared broadcast log; every agent reads it via their cursor, including agents that join later (unless they joined `--fresh`). The log is GC'd once all present agents have read past a message (or after 7 days).
