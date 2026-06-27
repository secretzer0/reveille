---
description: "List agents currently on the bus (live / stale)."
argument-hint: ""
---

Show who is on the agent bus.

```
python3 ~/.claude/scripts/bus.py list
```

Each line is an agent name marked `LIVE` (its session's watcher is running) or `stale` (presence file left behind by a session that exited without leaving). Liveness is best-effort — an agent busy evaluating a prompt (watcher momentarily disarmed) can briefly read as stale.

To clear out stale entries: `python3 ~/.claude/scripts/bus.py prune`.
