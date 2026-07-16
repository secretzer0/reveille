# Lessons

Defect post-mortems distilled to rules. One entry per defect, template from `usage()`
(hard cap 10 lines): Symptom / Root cause / Rule / Detection. No confessionals, no
self-audits, no scoreboards -- if it does not fit the template, it is not a lesson yet.
Agents read this file at boot. Recurring rules get promoted by the architect into
CLAUDE.md hard rules, then into automated checks (lint/CI grep), and leave this file.

## 2026-07-16 log-label-says-woke-means-delivered
Symptom: agentbus.log reads `send(web) -> * -> woke [7 agents]` for a broadcast that woke
nobody; it convinced an agent mid-storm-hunt that the fleet had just been stormed.
Root cause: the log prints res["wake"] under the label "woke", but that field is the
DELIVERY list from _wake_targets (all live agents); _notify is skipped for broadcasts,
so for `to='*'` the label names agents that were never poked.
Rule: log what happened, not what was computed. A field named for one thing and printed
under another is a wrong-diagnosis generator -- the mechanism was correct, its description
was false, and the description is what humans debug from.
Detection: broadcast on an idle bus -> log says "woke [...]" but no "wake ring" lines
follow. "wake ring" (true wake path only) is the honest signal; "woke" is not.

## 2026-07-16 naive-iso-local-time-false-zero
Symptom: history() window query for a UTC incident window returned 0 despite matching
messages; the zero happened to agree with reality by luck, five hours off target.
Root cause: naive ISO datetimes were interpreted server-local (fromisoformat +
.timestamp()), silently shifting UTC-intended windows by the host's offset.
Rule: naive ISO = UTC everywhere on the bus; pass an explicit offset to mean
anything else. Never let host timezone leak into shared-epoch queries.
Detection: since=until at a known message's UTC minute returns 0 -> timezone drift.

## 2026-07-15 broadcast-wake-storm
Symptom: 74-message overnight storm across 9 agents; ~3 hours of churn produced two real
fixes and a pile of meta-traffic; agents never returned to their tasks.
Root cause: every broadcast woke every agent and the ring text said "act", so reply
became the default -- N^2 by construction.
Rule: delivery != wakeup. Broadcasts queue silently; reply only if named, blocked, or
asked directly; silence is a valid turn.
Detection: agentbus.log shows "wake ring" lines for most of the fleet within one minute.
