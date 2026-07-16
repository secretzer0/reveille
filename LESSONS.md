# Lessons

Defect post-mortems distilled to rules. One entry per defect, template from `usage()`
(hard cap 10 lines): Symptom / Root cause / Rule / Detection. No confessionals, no
self-audits, no scoreboards -- if it does not fit the template, it is not a lesson yet.
Agents read this file at boot. Recurring rules get promoted by the architect into
CLAUDE.md hard rules, then into automated checks (lint/CI grep), and leave this file.

## 2026-07-15 broadcast-wake-storm
Symptom: 74-message overnight storm across 9 agents; ~3 hours of churn produced two real
fixes and a pile of meta-traffic; agents never returned to their tasks.
Root cause: every broadcast woke every agent and the ring text said "act", so reply
became the default -- N^2 by construction.
Rule: delivery != wakeup. Broadcasts queue silently; reply only if named, blocked, or
asked directly; silence is a valid turn.
Detection: agentbus.log shows "wake ring" lines for most of the fleet within one minute.
