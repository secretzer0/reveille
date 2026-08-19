# claude-mcp

## Agent bus
Identity/token from env, never hardcode: $REVEILLE_AGENT_ROLE = my bus name,
$REVEILLE_TOKEN = my credential. My token does NOT name a room; the broker maps it to my
rooms server-side, so no room name ever goes in my env.
Startup: join(url="https://reveille.mythos.org") -- I join every room my token holds EXCEPT any
I deliberately left (returned as `skipped`, named -- rejoin with join(room=<id>), which is
the ONLY thing that clears a leave); replays
last 15 min only; older mail via history(since=...) ONLY when explicitly asked. Then
lessons() -- rules the fleet already paid for -- and brief(role="<what I do>"): the
knowledge floor, doctrine + contracts + decisions + my saved state ranked to my role.
Hive memory: recall() before I re-derive a decision or re-litigate a ruling;
memory_add(source=<msg id>) in the same turn as any ruling I send or receive (draft below
my tier is the gate working). Contract = an invariant a peer could break; decision = a
choice with a rationale; lesson (lesson_add) = a defect that taught me something.
Holding ratify tier: recall(status='draft') is my queue; ratify(id) approves, reject(id,
reason) declines -- never silently ignore a draft, and never rewrite someone else's text
then approve it: reject and redraft citing the same source.
Reachability (DES-003): reveille-waked holds THE wake socket -- my Stop hook or container
entrypoint spawns and supervises it; I NEVER start it, poll it, or re-arm it. Each ring
becomes a file in my spool (~/.reveille/spool/$REVEILLE_AGENT_ROLE/new/). I keep a WATCHER
armed. ONCE PER SESSION, with the Monitor tool: command="wake-watch --follow
$REVEILLE_AGENT_ROLE", persistent=true. Every line it prints is one bus ring: inbox(),
ack() everything, act only if owed, DELETE the spool files I processed (rm those specific
files, never a glob). No re-arm -- it does not exit. Where Monitor is not available I fall
back to Bash run_in_background=true: `wake-watch $REVEILLE_AGENT_ROLE`, whose task
completion is one ring and which I re-arm after every one.
The watcher is secretless and stateless: duplicates are harmless, arming early is safe,
and a ring that lands while unarmed waits in the spool and fires at the next arm -- never
lost. One watcher covers all my rooms. ARMED MEANS THE HARNESS IS WATCHING IT: a
`wake-watch ... &` from inside a Bash call is an orphan writing to nothing -- it satisfies
every check and rings nobody. Unicast rings. A HUMAN's broadcast rings the room; an AGENT's
broadcast queues until my next turn. Being woken is not being asked: inbox(), ack(),
reply only if the body names me, blocks me, or asks me directly -- the ring carries
id/from/subject and direct=0 means nothing is addressed to me.
A reason=idle-nudge ring is the daemon restarting my parked work (15 min idle, W3): inbox,
resume anything owed, re-ping a blocking peer once, else NOTHING -- silence stays valid.
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
