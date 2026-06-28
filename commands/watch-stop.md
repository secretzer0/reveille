---
description: "Leave the agent bus and stop THIS session's standup/bus loop."
argument-hint: ""
---

Leave the bus and stop the loop for **this session only**.

1. Leave the bus (remove this session's presence so peers stop seeing you):
   ```
   NAME=$(python3 ~/.claude/scripts/bus.py whoami 2>/dev/null) && python3 ~/.claude/scripts/bus.py leave --name "$NAME" || echo "not joined"
   ```
2. Kill only this session's watcher (other windows' watchers carry a different tag and keep running). The `[f]` keeps pkill from matching its own command line:
   ```
   pkill -f "[f]swatch.py.*--tag $CLAUDE_CODE_SESSION_ID" && echo "watcher stopped" || echo "no watcher running"
   ```
3. Do NOT re-arm. The loop is over. Acknowledge and wait for the user.

Note: Esc alone does not stop the loop — it interrupts the turn but the background watcher keeps running and re-wakes you. This command is the real stop. Your inbox/processed dirs are left on disk (history); `bus.py prune` and broadcast GC clean up over time.
