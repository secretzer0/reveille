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
Reachability (DES-003): reveille-waked holds THE wake socket -- my Stop hook, the host
waked, or the container entrypoint runs it; I NEVER start it, poll it, or re-arm it. Each
ring becomes a file in my spool (~/.reveille/spool/$REVEILLE_AGENT_ROLE/new/) AND rings my
CLI's own inbox socket, which starts a turn in a body sitting idle. So a ring reaches me
with NO watcher running anywhere.
I DO NOT ARM A WATCHER. The arm rule is DEAD (operator, 2026-09-20). Do not run
`wake-watch`, do not re-arm after a ring, do not arm "just in case" at boot. THE STOP HOOK
DECIDES, not standing doctrine: it lets me stop when the doorbell can reach me -- a waked
holding my spool lock, and a live interactive session in my registered directory with the
reveille MCP -- and when it cannot it BLOCKS and NAMES WHICH HALF FAILED. I act on that
sentence, and only then, and only as it says.
Per ring: inbox(), ack() everything, act only if owed, DELETE the spool file I processed
-- the ring's `spool` key is its absolute path; rm that, never a glob. `reveille ack <the
spool path>` does the ack and the rm in one call and refuses to delete a ring whose ack did
not land. An entry I leave behind is replayed, so drain what I read.
THE SPOOL IS STILL THE MAILBOX; the doorbell is only the doorbell. The socket reaches a
RUNNING session and nothing else, so waked files the ring FIRST and rings afterwards: a
ring that lands while no session is up waits in the spool and is read on my next turn --
never lost.
IF THE HOOK EVER DOES SEND ME TO ARM ONE, arm ONCE and verify on the turn after: a second
arm raised while the first still holds the wake socket gets the NEWCOMER SIGTERM'd
(`Terminated`, exit 143), which leaves me UNARMED behind an exit code that reads as
ordinary noise. The old claim that "a duplicate costs one duplicate ring" is RETRACTED --
it costs the arming. Unicast rings. A HUMAN's broadcast rings the
room; an AGENT's parentless broadcast queues until my next turn, and an
agent's REPLY on a thread I authored in rings me unless I already read it
(or the room has run 40 agent messages with no human speaking). Being woken is not being asked:
inbox(), ack(), reply only if the body names me, blocks me, or asks me directly --
the ring carries id/from/subject and direct=0 means nothing is addressed to me.
A reason=mail ring is the daemon's probe finding DIRECT mail (60 s, W4); broadcast-only
unread never rings, because a parentless broadcast is read on my next turn.
A reason=idle-nudge ring is the daemon restarting my parked work (55 min idle, W3) and it
is BLIND -- it claims nothing about mail: inbox, resume anything owed, re-ping a blocking
peer once, else NOTHING -- silence stays valid.
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
