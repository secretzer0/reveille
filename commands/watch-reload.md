---
description: "Tell every standup agent to reload its instructions from disk (after you update the command)."
argument-hint: ""
---

Broadcast a reload directive so every `/watch-standup` agent re-invokes itself and picks up the latest command file.

```
FROM=$(python3 ~/.claude/scripts/bus.py whoami 2>/dev/null || echo operator)
python3 ~/.claude/scripts/bus.py send --from "$FROM" --all --subject reload --body "DIRECTIVE:RELOAD"
```

Report it sent, then stop (do not loop).

An agent running a current `/watch-standup` handles `DIRECTIVE:RELOAD` by re-running `/watch-standup --name <its role>`, which re-reads the updated file. Agents that were started **before** the RELOAD handler existed won't recognize the directive — restart those sessions or re-run the command in their terminal.
