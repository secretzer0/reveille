---
description: "Kick an agent off the bus: send it a LEAVE directive, or --force to remove presence + kill its watcher."
argument-hint: "NAME [--force]"
---

Evict an agent from the bus.

Parse `$ARGUMENTS`: target `NAME`, optional `--force`.

1. Identify yourself as the sender (falls back to `operator` if you haven't joined):
   ```
   FROM=$(python3 ~/.claude/scripts/bus.py whoami 2>/dev/null || echo operator)
   ```
2. Kick:
   ```
   # cooperative — drop a LEAVE directive in NAME's inbox; the agent stops itself:
   python3 ~/.claude/scripts/bus.py kick --name NAME --from "$FROM"

   # forced — also remove its presence and kill its watcher process (unresponsive/stale agent):
   python3 ~/.claude/scripts/bus.py kick --name NAME --from "$FROM" --force
   ```

- **Cooperative** (default) requires the target to be running a loop that handles `DIRECTIVE:LEAVE`. `/watch-standup` agents do — they receive it, run `/watch-stop`, and exit cleanly.
- **`--force`** works regardless (same machine, same user): it deletes the target's presence file and `pkill`s its watcher by tag. Use it for an agent that's hung or already gone. It skips the agent's own graceful cleanup.
