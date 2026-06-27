---
description: "Stop THIS session's /watch-loop watcher (leaves other windows' watchers running)."
argument-hint: ""
---

Stop the filesystem watch-loop for **this session only**.

1. Kill only the watcher tagged with this window's session id:
   ```
   pkill -f "[f]swatch.py --tag $CLAUDE_CODE_SESSION_ID" && echo "this session's watcher stopped" || echo "no watcher running for this session"
   ```
   Each `/watch-loop` arms its watcher with `--tag "$CLAUDE_CODE_SESSION_ID"`, so this matches only watchers started by THIS window. Other sessions' watchers carry a different id and keep running. The `[f]` in the pattern stops pkill from matching its own command line.
2. Do NOT re-arm the watcher. The loop is over. Acknowledge and wait for the user's next instruction.

(Also `TaskStop` the watcher's background task if you still have its id in context — belt and suspenders. To nuke every session's watchers at once: `pkill -f '[f]swatch.py'`.)
