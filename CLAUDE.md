# claude-mcp

## Agent bus
Identity/token from env, never hardcode: $REVEILLE_AGENT_ROLE = my bus name,
$REVEILLE_TOKEN = my credential. My token does NOT name a room; the broker maps it to my
rooms server-side, so no room name ever goes in my env.
Startup: join(url="http://127.0.0.1:8765") -- I join every room my token holds; replays
last 15 min only; older mail via history(since=...) ONLY when explicitly asked. Then
lessons() -- rules the fleet already paid for.
Reachability: I keep a wake waiter armed -- Bash run_in_background=true: `wake --once --url
ws://127.0.0.1:8765/wake --name $REVEILLE_AGENT_ROLE --token $REVEILLE_TOKEN`. Its
task-completion notification is a bus ring: inbox(), ack(), act only if owed, RE-ARM. One
waiter covers all my rooms. Unicast rings; broadcasts queue until my next turn. Waiter down
-> mail still queues; inbox() each turn.
Rooms: every message carries room/room_name. I reply in the room it came from (reply_to
infers it). New thread with 2+ rooms -> I pass room=; I never guess. Cross-room reply is
refused -- to carry knowledge across, I post a new root message in the target room.
Protocol: inbox(), ack() everything. Reply ONLY if named in NEED:, blocked, or asked
directly. FYI/retraction/method-lesson -> ack + own notes, no reply. Broadcast ONLY if a
shared contract changed or I block multiple peers. Nothing owed -> silence is a valid turn.
reply_to to thread. DIRECTIVE:LEAVE to me -> leave().
Defects: load-bearing (peers coding against it now) -> surface immediately (unicast owner
NEED: + repro; broadcast only if several peers build on it). Anything else -> finish my
current task first, then unicast the owner. Lessons -> lesson_add(), never bus traffic.
Full reference: usage() or GET <broker>/usage. Broker version bumped -> re-read usage(),
its CHANGES section says what changed and how to use it.
