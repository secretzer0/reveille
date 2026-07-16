# claude-mcp

## Agent bus
Identity/token from env, never hardcode: $AGENT_ROLE = my bus name, $AGENTBUS_TOKEN = secret.
Startup: join(url="http://127.0.0.1:8765") -- replays last 15 min only; older mail via
history(since=...) ONLY when explicitly asked. Read LESSONS.md at repo root, if present.
Reachability: I keep a wake waiter armed -- Bash run_in_background=true: `wake --once --url
ws://127.0.0.1:8765/wake --name $AGENT_ROLE --token $AGENTBUS_TOKEN`. Its task-completion
notification is a bus ring: inbox(), ack(), act only if owed, RE-ARM. Unicast rings;
broadcasts queue until my next turn. Waiter down -> mail still queues; inbox() each turn.
Protocol: inbox(), ack() everything. Reply ONLY if named in NEED:, blocked, or asked
directly. FYI/retraction/method-lesson -> ack + own notes, no reply. Broadcast ONLY if a
shared contract changed or I block multiple peers. Nothing owed -> silence is a valid turn.
reply_to to thread. DIRECTIVE:LEAVE to me -> leave().
Defects: load-bearing (peers coding against it now) -> surface immediately (unicast owner
NEED: + repro; broadcast only if several peers build on it). Anything else -> finish my
current task first, then unicast the owner. Lessons -> one LESSONS.md entry (template via
usage()), never bus traffic.
Full reference: usage() or GET http://127.0.0.1:8765/usage. Broker version bumped -> re-read
usage(), its CHANGES section says what changed and how to use it.
