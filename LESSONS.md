# Lessons

Defect post-mortems distilled to rules. One entry per defect, template from `usage()`
(hard cap 10 lines): Symptom / Root cause / Rule / Detection. No confessionals, no
self-audits, no scoreboards -- if it does not fit the template, it is not a lesson yet.
Agents read this file at boot. Recurring rules get promoted by the architect into
CLAUDE.md hard rules, then into automated checks (lint/CI grep), and leave this file.

## 2026-07-16 usage-prescribes-uninstalled-wake-127
Symptom: two agents' boots armed the waiter verbatim from usage() and got `wake: command
not found` (exit 127). Unarmed waiter reads as a quiet bus, not a broken one -- mail queues
durably but no unicast ever rings. Silent reachability loss.
Root cause: `wake` is a console-script of the agentbus PIP package, stranded in
claude-mcp/.venv/bin; nothing puts that venv on PATH. The MCP server is http transport --
it installs nothing locally and can never deliver the CLI usage() tells agents to run.
Rule: a doc that prescribes a command MUST be true as written -- fix the PATH, not the doc.
Ship the CLI onto PATH at install time (symlink into ~/.local/bin) so usage() stays honest.
Detection: `command -v wake` empty while .venv/bin/wake exists -> every agent 127s on arm.
Never `pgrep -af`/`ps args=` a wake proc to check: argv carries --token in cleartext.

## 2026-07-16 compound-waiter-arm-sandbox-144
Symptom: wake waiter tasks died with exit 144 and no ring ever fired; agent believed
itself armed (a pgrep even matched other processes' cmdlines containing the pattern).
Root cause: arming with a compound command (pkill/grep + wake in one background Bash)
trips the sandbox, which reaps the whole task; bare single-command waiters survive.
Rule: arm with EXACTLY the one-line wake command the Stop hook prints -- nothing
prepended or appended. Verify armedness by broker state (presence connected), not pgrep.
Detection: background task exits 144; presence shows connected=false despite a "running"
arm task.

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
