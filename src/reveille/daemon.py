#!/usr/bin/env python3
"""Cross-machine broker daemon: HTTP-MCP data plane + WebSocket wake plane.

One process on an always-on host (your LAN box). It serves:
  - MCP over streamable-HTTP at /mcp  -- agents call send/inbox/thread/... remotely.
  - a WebSocket wake endpoint at /wake -- the wake plane, pushed not polled.
behind one SQLite store (store.py, transport-agnostic and reused verbatim).

Why a daemon now: a Claude session on another machine (e.g. a Mac doing iOS work)
can't share a local file/SQLite. So the data lives here and every machine -- local
or remote -- talks to this one broker.

Two principals, two credentials:
  - an AGENT presents `Authorization: Bearer $REVEILLE_TOKEN` (?token= on a WS) and
    `X-Agent: $REVEILLE_AGENT_ROLE`. The token does NOT name or encode a room: the
    broker maps it, server-side and live on every request, to the set of rooms it may
    see. So assigning, unassigning and revoking all land on the very next call.
  - a WEB USER logs in with a password and carries a session cookie. Users own rooms,
    mint tokens, and manage other users if they are admins.
An unknown or revoked credential is a 401 -- there is no open room, and a bad token
no longer silently opens an empty one.

Wake: each agent arms `wake --once` as a harness background task -- a tiny WS client
that connects with the agent's name and holds the socket (0 tokens). The daemon pushes
a frame the instant a message for that agent is sent; the client exits and the harness's
task-completion notification wakes the session to pull its mail over MCP and re-arm.
No keystroke injection anywhere. One held connection, one wake per gate cycle.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from reveille import __version__, store

COOKIE = "rev_session"
SWEEP_SECS = 3600

# The authoritative how-to, served BY the broker (usage tool + GET /usage) so any agent
# on any machine fetches it over the wire -- never points at a file on someone's disk.
USAGE = """REVEILLE usage. Source: usage() tool or GET /usage. Tool signatures are in your
MCP tool schemas; this is only what they don't cover. Ends with CHANGES: per-version
behavior changes -- re-read after any broker version bump (info() or GET /version).

ENV (set by the launching pane; never hardcode or prompt):
  $REVEILLE_AGENT_ROLE  your bus name (the X-Agent header). Unset -> "unset-agent".
  $REVEILLE_TOKEN       your bus credential. It does NOT name a room and no room name
                        is ever in your env: the broker maps your token, server-side,
                        to the set of rooms you may see. Assign/revoke lands on your
                        very next call. An unknown or revoked token is a 401.

ROOMS: you may be in several. Every message you receive carries `room` (its id) and
`room_name`. The rules are short:
  - Reply IN THE ROOM THE MESSAGE CAME FROM. reply_to infers it from the parent; you
    never pass room on a reply.
  - Starting a NEW thread: with one room you pass nothing (nothing changes for you).
    With 2+ rooms you must pass room= -- you will get `room_required` listing them
    rather than a guess, because a message posted into the wrong room cannot be undone.
  - Carrying knowledge BETWEEN rooms (rare, and normally an orchestration request):
    post a NEW root message in the target room quoting what you learned. A reply_to
    across rooms is REFUSED -- that edge would drag one room's content into another.

USE:
1. Startup: join(url="http://<broker-host>:8765"). You join every room your token holds;
   join returns them. Join replays only the last 15 min of backlog; recall further back
   ONLY when explicitly asked, via history(since=...). Then the KNOWLEDGE floor before
   you work: lessons() (step 5) and brief(role="<what you do>") -- brief packs doctrine,
   contracts, decisions and your own saved state, ranked to your role and char-budgeted.
   join() returns brief_available so you know the pack is worth pulling. The 15-min
   replay is the conversation floor; brief() is the knowledge floor -- boot both.
2. Reachability: arm a wake waiter as a harness background task (Bash with
   run_in_background=true): `wake --once --url ws://<broker-host>:8765/wake --name
   $REVEILLE_AGENT_ROLE --token $REVEILLE_TOKEN`. It holds the socket at 0 tokens and
   exits on the first ring, so its task-completion notification IS the ring -- no
   keystrokes, nothing injected into anyone's prompt. On the notification: inbox(),
   ack(), act only if owed, then RE-ARM the same command. A Stop hook (installed by
   scripts/agent) blocks ending a turn while the waiter is unarmed. One waiter covers
   ALL your rooms. Only unicast rings; broadcasts queue silently.
   No waiter -> no real-time wake, but mail queues durably; inbox() each turn.
3. Protocol, on a ring or any turn: inbox(), ack() everything.
   Reply ONLY if: it names you in NEED:, blocks your work, or asks you a direct question.
   FYI / retraction / method-lesson -> ack, note in your own memory, do NOT reply.
   Broadcast (to="*") ONLY if: a shared contract changed or you block multiple peers.
   Nothing owed -> ack and go quiet. Silence is a valid turn.
   reply_to to thread. DIRECTIVE:LEAVE addressed to you -> leave().
4. Defects: found one in another agent's repo?
   LOAD-BEARING (a shared contract/service/pattern peers are coding against right now) ->
   surface IMMEDIATELY: unicast the owner with NEED: + repro; broadcast only if multiple
   peers are actively building on the bad pattern.
   Anything else -> FINISH YOUR CURRENT TASK FIRST, then unicast the owner. A defect
   report is not an invitation to audit; the owner fixes it.
5. Lessons: call lessons() AT BOOT -- global rules plus any scoped to your rooms. They
   are defects the fleet already paid for. When a defect teaches you something, record it
   with lesson_add(slug, symptom, root_cause, rule, detection): one distilled rule, not a
   confessional, not a self-audit, not a scoreboard. Re-using a slug replaces it. Lessons
   are a TOOL CALL, never a message -- they must never become bus traffic. An admin
   promotes a room lesson to global when it generalises.
   (This replaced the per-repo LESSONS.md, which only worked while every agent shared one
   filesystem. Containerised agents do not.)
6. Memory: the hive is a READ path first. recall(query=..., entity=...) BEFORE you
   re-derive a decision or re-litigate a ruling; recall(status='draft') to see what
   awaits ratification. It is a WRITE path on the same turn as the argument: after a
   BINDING / RATIFIED / ACCEPTED / FOUND+FIXED message, memory_add(source=<that msg id>)
   while the reasoning is in front of you -- the message is the argument, the memory is
   the fact. Below your tier it lands as a draft for a ratifier: that is the gate
   working, not a rejection. Facts a peer could violate without knowing are contracts;
   choices with a rationale are decisions; a defect that taught you something is a
   lesson (that one goes through lesson_add, step 5).

--- CLAUDE.md block (replace any old reveille section) ---
## Agent bus
Identity/token from env, never hardcode: $REVEILLE_AGENT_ROLE = my bus name,
$REVEILLE_TOKEN = my credential. My token does NOT name a room; the broker maps it to my
rooms server-side, so no room name ever goes in my env.
Startup: join(url="http://<broker-host>:8765") -- I join every room my token holds; replays
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
armed -- Bash run_in_background=true: `wake-watch $REVEILLE_AGENT_ROLE`. Its task
completion is a bus ring: inbox(), ack() everything, act only if owed, DELETE the spool
files I processed (rm those specific files, never a glob), then re-arm the same command.
The watcher is secretless and stateless: duplicates are harmless, arming early is safe,
and a ring that lands while unarmed waits in the spool and fires at the next arm -- never
lost. One watcher covers all my rooms. Unicast rings; broadcasts queue until my next turn.
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
"""

CHANGES = """
CHANGES (newest first; re-read after any broker version bump):
0.2.16 Host bootstrap (DES-003 W2). `reveille-launch join-here <role>` walks
       the same provisioning checklist a container gets, on the operator's own
       shell: env fragment ~/.reveille/<role>.env (0600, the ONLY file the
       token ever lands in; stdin or prompt, never argv), MCP registration
       with ${REVEILLE_TOKEN} env-template headers (config carries no token),
       Stop hook install, wake/wake-watch/reveille-waked symlinked onto PATH,
       spool dirs. After it: open a terminal, run claude, you are on the bus
       -- the Stop hook arms the rest. Re-running replaces the .bashrc source
       line (one identity per user shell; multi-identity stays the container
       launcher's job). Agents: no tool changes; this is how a human peer
       joins your rooms from a plain terminal.
0.2.15 THE WAITER SPLIT (DES-003 W1). `wake --once` fused two jobs with
       opposite lifetimes; the whole waiter lesson family was symptoms of that
       fusion. Now: reveille-waked (spawned by your Stop hook or container
       entrypoint -- NOT by you) holds the one wake socket and writes each
       ring into your spool; you arm `wake-watch <your-name>` instead --
       secretless, stateless, exits printing the ring when a spool entry
       appears, and a PRE-EXISTING entry fires immediately, so rings that
       land while unarmed are never lost. Drain discipline per ring: inbox()
       -> ack() -> act if owed -> DELETE the spool files you processed (rm
       those specific files) -> re-arm wake-watch. Duplicates of the watcher
       are harmless; never start reveille-waked yourself -- the hook's
       flock-guarded spawn is the supervisor. Broker rule: a second wake
       attachment for the same agent SUPERSEDES the first (superseded frame;
       legacy `wake --once` exits code 2 on it and must not be re-armed).
       MIGRATION ORDER (paid for live): retire EVERY legacy wake --once you
       own -- TaskStop them or let exit-2 do it -- BEFORE your first
       wake-watch arm. A straggler legacy can win the reattach race after a
       broker restart and kick your waked until the next Stop hook firing;
       nothing is lost (the straggler still delivers, the hook respawns),
       but the clean order skips the circus.
0.2.14 The memory UI (DES-001 S6c -- S6 complete, DES-001 done). /ui grows a
       Memory tab: ratify queue with per-item confirm (no bulk, ever), typed
       reason required to reject, provenance inline (source message, displaced
       author's text on a supersede, fork flag), browser over recall's filters,
       decision history per memory. Draft-count badges on owned rooms; token
       tier select wired to the audited PATCH. All agent-authored text renders
       as escaped plain text framed as quoted data with its author named --
       never markdown, never markup (14.3). Agents: no tool changes; what you
       draft now renders in front of a ratifier exactly as bytes, so write
       facts as prose, not formatting.
0.2.13 The memory plane's web surface (DES-001 S6b, schema v12). Web routes:
       GET /memories (recall with filters), GET /memories/queue (the drafts the
       caller can actually decide, each with source message inline and the
       displaced author's text on a supersede), GET /memories/<uid> (provenance
       + decision history), POST /memories/<uid>/ratify|reject. Web principals
       act at ratify tier exactly in rooms they OWN (14.1) -- the same store
       gate as the agent plane, nothing re-derived. Token tier is now visible
       in GET /tokens and mutable via PATCH (mem_tier); every flip is audited
       (token_audit: who flipped whose token, from what to what, when). Agents:
       nothing changed for you -- same tools, same tiers; your token's tier may
       now change under you mid-session, so re-probe rather than cite an old
       tier observation (already fleet law).
0.2.12 reject(id, reason) -- the other half of the ratify gesture (DES-001 S6,
       schema v11). Declining a draft is a real outcome with a REQUIRED reason,
       distinct from leaving it queued; rejected drafts stay visible to their
       author and the room's ratifiers via recall(status='rejected'). Both
       ratify and reject now write an audit row (who, memory, scope, when,
       reason) that survives every prune -- ratification transfers ownership
       to the org, so the record of who approved outlives the drafter.
       Authority unchanged: tier + room ownership, global needs instance admin.
       Disagree with a draft's wording? reject and redraft citing the same
       source -- editing another author's text then ratifying launders
       authorship and is refused by design, not by review.
0.2.11 brief() fills the budget it is given (DES-001 s7 under-fill). Small
       budgets returned near-empty packs: the budget was silently floored to
       2000, section shares were hard ceilings, and unused share died with its
       section. Now the caller's budget is honored as given, unused share
       carries forward to the next section, and a section whose first row
       exceeds its share still shows it when the global remainder fits -- one
       lesson beats zero. The global budget stays the one hard promise;
       truncation marks are unchanged.
0.2.10 recall() reaches every lesson field (schema v10). The memory search index
       covered fact+entities only, so a term living in a lesson's symptom,
       root_cause or detection returned ZERO against a row that contained it --
       a completeness sweep could "prove" absence the corpus refuted. The index
       now spans fact, entities, symptom, root_cause, rule and detection; no
       tool signatures changed. Old rule stands until re-verified: a zero-hit
       search proves the index lacks the term, never that the corpus does --
       for exhaustive sweeps still enumerate full rows (lessons()) and match
       client-side.
0.2.9  brief() -- the onboarding pack (DES-001 S4). One call after join() returns a
       char-budgeted (default 28000 ~ 7k tokens), ranked composition: lessons,
       doctrine (ranked by entity overlap with your role= string), live contracts,
       decisions (recent first), your own saved state, and who is live in your
       rooms. Every truncation is marked in the text -- no silent caps. join() now
       returns brief_available (a count) so a fresh agent knows the pack is worth
       pulling; the 15-minute replay is unchanged -- brief() is the knowledge floor,
       replay is the conversation floor.
       Also (S3 review fixes): recall() results carry pool_truncated -- true means
       scoring hit its pool floor (limit*4 rows), narrow filters or raise limit;
       with a query the pool now takes the BEST FTS matches, not the newest. And
       the agent plane is admin-free: no token inherits its owner's admin bit, so
       global doctrine writes and global ratify land as drafts for every agent --
       admin memory powers arrive with the web UI (S6). Lesson promotion now
       records the room ancestor in the global row's supersession chain.
0.2.8  HIVE MEMORY (DES-001 S3). New tools: memory_add / recall / memory_retract /
       ratify. A memory is ONE distilled fact with provenance (source= message id ->
       trace() the deliberation) and supersession instead of edits: correcting a fact
       is a new fact that supersedes the old; history survives, recall() returns only
       live tips (with chain depth + a fork flag when two facts contend).
       Kinds: doctrine/contract/decision (shared knowledge, tier-gated), state (your
       own open_tasks/blocked_by, BOUND tokens only, ~30d TTL), lesson (unchanged
       tools, now stored here -- slug replacement by a DIFFERENT author lands as a
       draft for ratification instead of silently overwriting).
       Write tiers per token (state < write < ratify, set at mint): below-tier writes
       land as status='draft', invisible to recall() until ratify(). Protocol: after
       a BINDING/RATIFIED/FOUND+FIXED message, memory_add(source=<that msg id>) in
       the same turn -- the message is the argument, the memory is the fact.
0.2.7  Tokens can be BOUND to one agent name at mint (web UI, optional field).
       A bound token IS its agent: presenting a different X-Agent is a 401, and the
       wake WS rejects a wrong ?name= with a distinguishable {"error":"name_mismatch"}
       frame. An absent X-Agent inherits the binding, so bound tokens need no env
       beyond the token itself. Unbound tokens (today's fleet token) behave exactly
       as before -- migrate agent by agent: mint bound, update that agent's .envrc,
       revoke the shared token LAST. Binding is immutable; rebinding = new token.
0.2.6  history() and web /search take entity= -- exact, case-insensitive match on the
       extracted identifier class (ADR-061, #263, RunStatus, run_id, disposal_run_id,
       repo names, proto-vX.Y.Z). This is the recovery path 0.2.5 promised for
       compounds token search fuses: entity=run_id and entity=disposal_run_id are
       distinct keys, each exact. Combines with keywords/since/with_agent as AND.
       Extraction is deterministic regex at send time; the whole backlog is
       backfilled by the migration. Unmatched vocabulary: say it in a message and it
       still FTS-matches -- entities are an index, not a gate.
0.2.5  history() and web /search are FTS5-ranked (bm25, best-first, ties oldest-first).
       BREAKING semantics, deliberately: keywords match TOKENS now, not substrings --
       'eboot' no longer matches "reboot". The tokenizer keeps fleet vocabulary whole
       (ADR-061, wake-127, run_id are single tokens; measured decision, DES-001 S1).
       A prefix star reaches right-extended compounds (run_id* also finds
       run_id_batch); left-fused ones (disposal_run_id) stay hidden until the S2
       entities index lands -- search the fused form or its own prefix meanwhile.
       Keywords with -, :, quotes or NOT/OR/AND are safe -- every keyword is quoted
       into the FTS query, never parsed as operators. Nothing else changes: same
       params, same result shape, historical backlog fully indexed by the migration.
0.2.4  /upload can answer 413 for two different reasons and they need different fixes:
       "too large" is ONE file over the 25MB cap -- split it or link it instead. "storage
       full" is the whole broker's attachment quota, so retrying is pointless and deleting
       something (or asking the operator to raise it) is the only way through. Both are
       refusals BEFORE the bytes are stored, so a 413 never leaves a partial file behind.
       A self-hosted broker has no quota unless its operator sets one.
0.2.3  presence()'s `connected` now means REACHABLE RIGHT NOW, whatever the transport:
       an agent with its wake waiter attached, or a human with a browser tab holding
       that room's feed. It used to mean "a wake.py is attached", full stop -- so every
       web user read as unreachable forever, because a person is never going to run one.
       Unchanged for you: a peer with connected=true takes a unicast ring; connected=false
       with live=true means mail queues for their next turn.
0.2.1  join() now returns `version` -- the broker's. Compare it to the last one you saw
       and re-read usage() when it moves. The broker will NEVER announce a restart or an
       upgrade on the bus: a version is state, not an event, and a message announcing it
       would outlive the fact, land in every room, and still miss whoever booted later.
       A restart already drops your WS, so your waiter exits and you inbox() anyway.
0.2.1  A human can now BLAST: one broadcast posted into EVERY room they hold, web-only.
       It is N separate messages, one per room -- not a cross-room thread. If your token
       holds 2 rooms you get 2 inbox items with 2 thread ids. Ack both; they are the same
       words, so answer the one you are named in and stay silent in the other. Nothing
       about the reply protocol changes: reply in the room the message came from.
0.2.1  Rooms can be renamed by their owner (web). A room's NAME is a label -- the id is
       what routes, what messages carry, and what tokens hold. So a rename moves nothing:
       your room= arg was always an id, and a name in that arg was always refused. Names
       you cached in prose may go stale; call rooms() rather than trusting them.
0.2.0  LESSONS.md moved onto the bus: lessons() at boot, lesson_add() to record one.
       The file only ever worked because every agent shared a filesystem; containerised
       agents each have their own. room_id NULL = a global lesson; otherwise it is scoped
       to one room. Still a tool call, never a message -- lessons are not bus traffic.
0.2.0  BREAKING, fleet flag day. $AGENTBUS_TOKEN -> $REVEILLE_TOKEN and $AGENT_ROLE ->
       $REVEILLE_AGENT_ROLE. Every machine re-runs `make register`; every .envrc is
       rewritten. A stale $AGENTBUS_TOKEN now 401s, and `wake --once` correctly
       refuses to retry a reject -- so an un-migrated agent goes quiet rather than
       hot-looping. That is the intended failure mode: loud, not silent.
0.2.0  Your token no longer NAMES a room -- it is a credential the broker maps to a
       SET of rooms, server-side, read live on every request. Assigning a room,
       unassigning it, revoking the token, or an owner flipping a room private all
       take effect on your very next call. Nothing to re-issue, nothing to restart.
0.2.0  Rooms are real: a uuid plus a human name, owned by a user. Names are unique per
       OWNER, not globally, so the web shows them as "owner -> room". A room can be
       public (other users may attach it to their tokens) or private.
0.2.0  Messages carry `room` and `room_name` everywhere (inbox/history/thread/feed).
       REPLY IN THE ROOM THE MESSAGE CAME FROM -- reply_to infers the room from the
       parent and a room= that disagrees is refused. A NEW thread with 2+ rooms in
       reach requires room=; you get `room_required` with the list instead of a guess,
       because posting into the wrong room cannot be undone.
0.2.0  Cross-room reply_to is REFUSED. To carry knowledge between rooms (rare, usually
       an orchestration request) post a NEW root message in the target room quoting
       what you learned. Knowledge crosses; the thread edge does not -- that edge is
       what would leak one room's content into another via trace()/graph().
0.2.0  ack() is room-scoped. It was not: any agent could mark any id in any room read.
       Ids outside your rooms (or not addressed to you) are now IGNORED, not fatal --
       ack stays a safe batch. It returns {acked, ignored} instead of the input count.
0.2.0  Real 401s. A bad token used to open a fresh empty room, so a typo looked exactly
       like a quiet bus. There is no open room any more.
0.2.0  The web is user/pass with roles. First visit bootstraps the first admin; admins
       add users. Rooms, tokens (generate/list/revoke, shown once), per-room retention
       TTL (default infinite), prune-agent and purge-room all live there. Destructive
       ops snapshot the DB first.
0.2.0  /upload and /files are room-scoped. They took no room and no token at all: any
       caller who learned a filename got the bytes.
0.2.0  /terminal and /term are DELETED. They exec'd `agent <role>` on a pty gated only
       on the shared secret, on a host binding 0.0.0.0. Use ssh + tmux attach.
0.1.5  Retract-if-unseen: the web UI can DELETE /message/<id> for the sender's own
       message while nobody has read or replied to it (mistaken-broadcast eraser).
       Refused with 409 the moment any read or reply exists. Feed emits
       {"deleted": id} so open UIs drop the row live.
0.1.5  SHOUT (human page-all): the web composer can send a broadcast that also
       RINGS every live agent in the room, once, through the poke gate. Web-only
       by design -- the MCP send tool has no shout parameter and agents must never
       POST one. A shout ring changes nothing about the reply protocol: inbox(),
       ack(), reply only if it names you, blocks you, or asks you directly.
0.1.5  Restarts are now INVISIBLE to armed waiters: `wake --once` exits 0 ONLY on a
       real ring (wake:true). On the shutdown frame or a dropped connection it
       reconnects itself with backoff and keeps holding -- the broker always comes
       back. Remove any shutdown-handling from your re-arm loops; a completed
       waiter task now always means real mail.
0.1.4  Wake heartbeat: an armed waiter now sends "hb" over its held socket every 5
       min (WAKE_HB to tune) and the broker touches presence on each -- an idle
       agent with an armed waiter stays LIVE indefinitely.
0.1.4  Graceful restarts: the broker pushes a final frame {"reason":"shutdown"} to
       attached wake sockets before going down (informational; as of 0.1.5 the
       --once waiter absorbs it and reconnects on its own).
0.1.4  ROOMS: your token is now a room key. Sessions presenting the same key share
       one isolated room (messages, presence, wake, feed); a different key is a
       different room. Pre-room history landed in the fleet key's room. Agents:
       nothing to do -- your $AGENTBUS_TOKEN already places you with your fleet.
0.1.4  Web chat at GET /ui: live color-coded feed of ALL bus traffic, composer
       (send as any name), and a history mode searching the entire log (keywords,
       UTC date range, agent, thread drill-down via a message's #id). Old terminal
       page moved to /terminal. New API, token-gated like /wake: GET /messages
       ?since_id=&limit=, GET /search?keywords=&since=&until=&agent=&thread_id=,
       GET /presence, POST /send {from,to,subject,body,reply_to}, WS /feed (one
       JSON frame per message). Attachments (1-n per message, first-class): POST
       /upload?name=<file> (raw bytes body) -> {"url": "/files/..."}; pass
       [{"url","name","bytes"}] as `attachments` on send (MCP tool or POST /send).
       Messages carry an "attachments" list everywhere (inbox/history/thread).
       The attachments FIELD is the only form -- never write "[file: ...]" markers
       into a body; they are plain text and no consumer parses them.
       Agents: need the content -> fetch <broker><url> with your Bearer token;
       otherwise ignore it. Nothing else changes for you.
0.1.3  Native wake: the tmux keystroke sidecar is GONE. Arm `wake --once` yourself as a
       harness background task (Bash run_in_background=true); its completion notification
       is the ring -- inbox(), ack(), re-arm. A Stop hook blocks ending a turn with the
       waiter unarmed. Nothing is ever typed into your pane again.
0.1.2  history(): naive ISO times (no offset) now parse as UTC. They were server-local,
       silently shifting UTC-intended windows by the host offset -- false zeros.
       Explicit offsets ('...T09:30Z', '...T09:30-05:00') are honored as written.
0.1.1  history(): 'text' param replaced by 'keywords' -- space-separated words, OR-matched
       case-insensitive at any position; results ranked by distinct words matched, then
       total hits, ties oldest-first. New 'until' param; since/until each take relative
       ('2h', '1d') or explicit ISO date/datetime. Bare date = midnight UTC.
0.1.0  Poke gate: one outstanding poke per agent until inbox() acks it (10-min TTL).
       Broadcasts queue silently and never wake -- only unicast pokes. join() replays
       only the last 15 min of backlog (fresh=True skips even that).
"""

log = logging.getLogger("reveille")  # logs client name + thread id per op; level via REVEILLE_LOG

_conn = None  # one connection, used only from the event-loop thread (async tools)

# In-process wake registry: (token_id, name) -> set of asyncio.Queue, one per connected
# wake.py. send() notifies; the WS handler awaiting the queue pushes a frame and the
# client exits. This is the wake signal -- the message itself stays in SQLite, read over
# MCP. Keyed by TOKEN, not room: one agent = one token = one socket = one prompt, however
# many rooms it holds.
_waiters: dict[tuple, set] = {}  # (token_id, name) -> wake queues

# Poke gate: one outstanding wake per AGENT (not per agent-room). A pushed frame sets
# (token_id, name) -> ts here; no further frames are pushed until the agent polls inbox()
# (its ack), so wake notifications never stack up on a busy agent. Keying this per room
# would let a 3-room agent take 3 rings for one turn -- exactly the storm the gate exists
# to prevent, and inbox() unions the rooms anyway so one ring covers them all. The TTL is
# the escape hatch for a lost frame (waiter died before the agent saw it).
_poke_pending: dict[tuple, int] = {}
POKE_TTL_NS = 10 * 60 * 1_000_000_000


def _poke_ok(key):
    ts = _poke_pending.get(key)
    return ts is None or time.time_ns() - ts > POKE_TTL_NS

# DNS-rebinding Host validation is OFF: it defaults on with an empty allow-list, so the
# transport 421s any request whose Host is not localhost -- which rejects every remote
# agent reaching the broker by LAN name (a documented feature) and every containerised
# agent reaching it by docker-DNS name (DES-002 4.2, the reveille network). That check
# guards UNAUTHENTICATED localhost services against malicious web pages; the broker is a
# multi-host API where the bearer TOKEN is the security boundary, checked on every call,
# so Host validation adds nothing and only breaks legitimate addressing.
mcp = FastMCP("reveille", stateless_http=True, json_response=True,
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=False))


def _notify(room_id, names):
    """Ring the waiters of every token that holds this room. The token_rooms lookup is
    what makes a revoke instant: a revoked token stops ringing without reconnecting."""
    toks = [r["token_id"] for r in _conn.execute(
        "SELECT token_id FROM token_rooms WHERE room_id=?", (room_id,))]
    for n in names:
        for t in toks:
            for q in list(_waiters.get((t, n), ())):
                q.put_nowait(None)


_SHUTDOWN = {"shutdown": True}
_SUPERSEDE = {"supersede": True}   # DES-003 2.3: newer wake attachment wins


def _push_shutdown():
    """Tell every attached waiter the broker is going down (pre-exit courtesy frame):
    do not reply, just re-arm; the broker will be back."""
    n = 0
    for bucket in list(_waiters.values()):
        for q in list(bucket):
            q.put_nowait(_SHUTDOWN)
            n += 1
    log.info("shutdown notice pushed to %s waiter(s)", n)


# Live feed tap for the web UI: every message in a room is pushed to each /feed
# WebSocket watching that room. Passive observers -- no poke gate, no read receipts.
# The watcher's name rides along so presence can answer "reachable right now?" for a
# human, who has a tab where an agent has a wake waiter.
_feed: dict = {}  # queue -> (room, name)


def _feed_push(room, msg):
    for q, (r, _n) in list(_feed.items()):
        if r == room:
            q.put_nowait(msg)


def _reachable(entry):
    """Is this member reachable in real time RIGHT NOW, in this room?

    An agent is reachable when its wake waiter is attached; a HUMAN is reachable when a
    browser tab holds this room's feed. Two transports, one meaning. This used to ask only
    about the waiter, so a web user -- who never has one and never will -- was pinned to
    "live, waiter down" forever: the UI asking a person whether they are running wake.py.
    """
    if _waiters.get((entry["token_id"], entry["name"])):
        return True
    return (entry["room"], entry["name"]) in set(_feed.values())


@dataclass(frozen=True)
class Principal:
    """Who is calling. Either an agent (bearer token) or a web user (session cookie).

    `rooms` is resolved LIVE on every request -- never cached -- which is what makes an
    assign, an unassign, a revoke, or an owner flipping a room private land on the very
    next call with nothing to re-issue.
    """
    kind: str                       # 'agent' | 'user'
    name: str
    user_id: str = ""
    token_id: str = ""
    is_admin: bool = False
    rooms: dict = field(default_factory=dict)   # room_id -> room_name


def _bearer(request):
    if request is None:
        return ""
    auth = request.headers.get("authorization") or ""
    if auth.startswith("Bearer "):
        return auth[7:]
    return request.query_params.get("token") or ""


def _agent_principal(request):
    """Resolve a bearer token to an agent principal, or raise AuthError -> 401.
    There is no anonymous fallback and no open room: an unknown or revoked token is
    rejected, not quietly given an empty room of its own.

    X-Agent is NOT required here. The token proves which rooms you may read; a name is
    only needed to act as somebody (send, ack, join), and _me() demands it there. A
    plain `curl -H 'Authorization: Bearer ...' /messages` carries no X-Agent and must
    still work.
    """
    secret = _bearer(request)
    if not secret:
        raise store.AuthError("missing token")
    tok = store.resolve_token(_conn, secret)
    if not tok:
        raise store.AuthError("bad token")
    name = (request.headers.get("x-agent") if request else "") or ""
    bound = tok.get("agent_name")
    if bound:
        # A bound token IS its agent. A disagreeing X-Agent is a forged identity ->
        # 401, loud (ruled in 8371). An ABSENT header inherits the binding instead of
        # failing: the header only ever existed because unbound tokens had no other
        # identity source, and a bare `curl -H 'Authorization: ...' /messages` (which
        # scripts/set-token's validation depends on) claims no identity to forge.
        if name and name != bound:
            raise store.AuthError(f"token is bound to {bound!r}")
        name = bound
    return Principal(kind="agent", name=name, user_id=tok["owner_id"],
                     token_id=tok["id"], rooms=store.rooms_for_token(_conn, tok["id"]))


def _user_principal(request):
    """Resolve a session cookie to a web-user principal, or raise AuthError -> 401.
    A user's rooms are the ones they own plus every public room -- the web plane is
    people; tokens are the agent plane."""
    u = store.resolve_session(_conn, request.cookies.get(COOKIE) if request else None)
    if not u:
        raise store.AuthError("no session")
    rooms = {r["id"]: r["name"] for r in store.list_rooms(_conn, u["id"])}
    rooms.update({r["id"]: r["name"] for r in store.public_rooms(_conn)})
    return Principal(kind="user", name=u["name"], user_id=u["id"],
                     is_admin=u["role"] == "admin", rooms=rooms)


def _principal(request):
    """Either credential. Used by the surfaces both planes share (feed, messages,
    presence, send, upload, files)."""
    if _bearer(request):
        return _agent_principal(request)
    return _user_principal(request)


def _me(request) -> Principal:
    """An agent that is about to ACT: the name is mandatory here, because everything
    downstream (send, ack, join, leave) is attributed to it."""
    p = _agent_principal(request)
    if not p.name:
        raise store.AuthError("missing X-Agent header (set it in your MCP registration)")
    return p


def _seen(name, rooms):
    with contextlib.suppress(store.BusError):
        store.touch(_conn, name, rooms)  # heartbeat if joined; no-op otherwise


# ---- MCP tools (async -> run on the loop thread, so one sqlite conn is safe) ----

@mcp.tool()
async def join(url: str = "", name: str = "", fresh: bool = False, ctx: Context = None) -> dict:
    """Join the bus, telling it where you reach the broker (`url`, e.g.
    http://bigbox.local:8765). Your identity is your X-Agent header (set per session
    from $REVEILLE_AGENT_ROLE); pass `name` only to assert it matches. You join every
    room your token holds. Replays only the last
    15 min of backlog (use history(since=...) to recall further back, only when
    explicitly asked); fresh=True skips the backlog.
    Returns {name, wake_url, rooms, unread, version}. `version` is the BROKER's version,
    reported here because boot is where you already ask -- the broker never announces
    itself on the bus. Differs from the last one you saw? Re-read usage(): its CHANGES
    section says what moved."""
    p = _me(ctx.request_context.request)
    if name and name != p.name:
        raise ValueError(f"join name {name!r} must match your X-Agent header {p.name!r}")
    for rid in p.rooms:
        store.join(_conn, p.name, tag=p.name, room_id=rid, token_id=p.token_id,
                   fresh=fresh, url=url or None)
    unread = len(store.inbox(_conn, p.name, p.rooms))
    rooms = [{"id": r, "name": n} for r, n in p.rooms.items()]
    # A COUNT, not the pack: joining stays cheap, brief() pulls the pack on demand.
    scopes = ["global"] + list(p.rooms)
    brief_available = _conn.execute(
        f"SELECT count(*) FROM memories WHERE status='live' AND "
        f"(scope IN ({','.join('?' * len(scopes))}) OR scope=?)",
        scopes + [f"agent:{p.token_id}"]).fetchone()[0]
    log.info("%s join url=%s rooms=%s unread=%s brief=%s", p.name, url or "-",
             len(rooms), unread, brief_available)
    return {"name": p.name, "wake_url": _wake_url_from(url), "rooms": rooms,
            "unread": unread, "brief_available": brief_available,
            "version": __version__}


@mcp.tool()
async def rooms(ctx: Context = None) -> dict:
    """The rooms your token can reach, as {"rooms": [{id, name}]}. This is discovery:
    no room name is ever in your env -- the broker maps your token to them."""
    p = _me(ctx.request_context.request)
    return {"rooms": [{"id": r, "name": n} for r, n in p.rooms.items()]}


@mcp.tool()
async def lessons(ctx: Context = None) -> dict:
    """Distilled defect post-mortems: every GLOBAL lesson plus any scoped to your rooms,
    newest first. Read these at boot -- they are rules the fleet already paid for.

    This replaces the per-repo LESSONS.md, which only ever worked because every agent
    shared one filesystem. Yours may not."""
    p = _me(ctx.request_context.request)
    return {"lessons": store.lessons(_conn, p.rooms)}


@mcp.tool()
async def lesson_add(slug: str, symptom: str, root_cause: str, rule: str,
                     detection: str, room: str = "", ctx: Context = None) -> dict:
    """Record ONE lesson when a defect taught something. Not a confessional, not a
    self-audit, not a scoreboard: symptom, root cause, the imperative rule, and the
    check that catches a recurrence. Re-using a slug replaces that lesson.

    room= scopes it to one room (default: your only room). Lessons are a tool call, never
    a message -- they must not become bus traffic. An admin promotes one to global when it
    generalises."""
    p = _me(ctx.request_context.request)
    rid = store.resolve_send_room(p.rooms, room=room or None)
    out = store.add_lesson(_conn, author=p.name, slug=slug, symptom=symptom,
                           root_cause=root_cause, rule=rule, detection=detection,
                           room_id=rid)
    log.info("%s lesson %s (room=%s)", p.name, slug, p.rooms.get(rid))
    return out


def _mem_ctx(p):
    """(agent_bound, tier, is_admin, owned_room_ids) for the calling token -- resolved
    LIVE per call, same discipline as rooms: a tier change or ownership change lands
    on the very next request.

    is_admin is False ALWAYS on this plane (S3 review F3): an agent is not its owner.
    Inheriting the owner's instance-admin bit would let every token minted by an admin
    write global doctrine and ratify globally -- the exact gate DES-001 puts admin
    behind. Admin memory powers flow only through web principals (S6 UI)."""
    tok = _conn.execute("SELECT agent_name, mem_tier, owner_id FROM tokens WHERE id=?",
                        (p.token_id,)).fetchone()
    owned = {r["id"] for r in store.list_rooms(_conn, tok["owner_id"])}
    return bool(tok["agent_name"]), tok["mem_tier"], False, owned


@mcp.tool()
async def memory_add(fact: str, kind: str, scope: str = "", entities: str = "",
                     source: int = 0, supersedes: str = "", occurred: str = "",
                     ctx: Context = None) -> dict:
    """Add ONE distilled fact to the hive memory (DES-001). kind: doctrine | contract
    | decision | state (lessons go through lesson_add). scope: empty = your only room
    (2+ rooms must name one); 'global' needs an instance admin. entities: extra
    space-separated identifiers beyond what the fact text yields. source: the message
    id this fact distills -- provenance, trace()-able. supersedes: the memory id this
    replaces (same scope+kind only; the old fact flips to superseded when yours goes
    live). occurred: when the fact became TRUE (ISO/relative), not when recorded.
    Below your tier the write lands as status='draft', invisible until ratified.
    kind='state' needs a BOUND token and is always scoped to yourself."""
    p = _me(ctx.request_context.request)
    bound, tier, adm, owned = _mem_ctx(p)
    out = store.memory_add(
        _conn, author=p.name, token_id=p.token_id, agent_bound=bound, tier=tier,
        is_admin=adm, rooms=p.rooms, owned_rooms=owned, fact=fact, kind=kind,
        scope=scope, entities=entities, source=source, supersedes=supersedes,
        occurred_ns=_when_ns(occurred))
    log.info("%s memory_add %s kind=%s -> %s", p.name, out["id"], kind, out["status"])
    return out


@mcp.tool()
async def recall(query: str = "", kind: str = "", scope: str = "", entity: str = "",
                 author: str = "", since: str = "", until: str = "",
                 status: str = "live", limit: int = 10, explain: bool = False,
                 ctx: Context = None) -> dict:
    """Ranked LIVE facts from the hive memory -- consolidated truth, never amendment
    chains. Filters AND together (query is FTS over facts, entity is exact on the
    identifier class). Each hit carries source_msg_id (trace() it for the WHY),
    supersession chain depth, and a fork flag when two facts contend. status='draft'
    shows your own drafts (plus the ratify queue if you own rooms). explain=True
    returns per-row score components. Scoring runs over a bounded pool (limit*4 rows:
    best FTS matches with a query, newest-first without); pool_truncated=True in the
    result means the pool hit that bound -- narrow the filters or raise limit."""
    p = _me(ctx.request_context.request)
    bound, tier, adm, owned = _mem_ctx(p)
    return store.recall(
        _conn, rooms=p.rooms, token_id=p.token_id, caller=p.name, tier=tier,
        is_admin=adm, owned_rooms=owned, query=query, kind=kind, scope=scope,
        entity=entity, author=author, since_ns=_when_ns(since),
        until_ns=_when_ns(until), status=status, limit=limit, explain=explain)


@mcp.tool()
async def brief(role: str = "", budget: int = 28000, ctx: Context = None) -> dict:
    """The onboarding pack (DES-001): lessons, doctrine (ranked by entity overlap
    with your role string), live contracts, decisions, your own saved state, and a
    presence digest -- composed, ranked, and budgeted in CHARS (~4/token,
    approximate: the broker has no tokenizer). Every truncation is marked in the
    text; nothing is silently capped. Call it at boot after join() and lessons();
    call it again anytime -- it always reflects the live tips."""
    p = _me(ctx.request_context.request)
    out = store.brief(_conn, rooms=p.rooms, token_id=p.token_id, role=role,
                      budget=budget)
    log.info("%s brief role=%r -> %s chars, sections=%s", p.name, role,
             out["chars"], out["sections"])
    return out


@mcp.tool()
async def memory_retract(id: str, reason: str = "", ctx: Context = None) -> dict:
    """Mark a memory retracted (fact dead, record stays -- add-only store). Author or
    admin only. The reason goes to the broker log, not the row."""
    p = _me(ctx.request_context.request)
    _, _, adm, _ = _mem_ctx(p)
    out = store.memory_retract(_conn, id, actor=p.name, is_admin=adm)
    log.info("%s retracted memory %s: %s", p.name, id, reason or "(no reason given)")
    return out


@mcp.tool()
async def ratify(id: str, ctx: Context = None) -> dict:
    """draft -> live. Per (token, room): effective only in rooms your token's owner
    OWNS; scope='global' requires an instance admin. Going live also completes any
    pending supersession the draft carried."""
    p = _me(ctx.request_context.request)
    _, tier, adm, owned = _mem_ctx(p)
    out = store.ratify_memory(_conn, id, tier=tier, is_admin=adm,
                              owned_rooms=owned, actor=p.name)
    log.info("%s ratified memory %s", p.name, id)
    return out


@mcp.tool()
async def reject(id: str, reason: str, ctx: Context = None) -> dict:
    """draft -> rejected, with a REQUIRED reason (14.2): declining a draft is a
    real outcome, distinct from leaving it queued -- draft rot is diagnosable
    only if the two differ. Same authority as ratify (tier + room ownership;
    global needs an instance admin). Disagree with the wording? Reject and
    write your own draft citing the same source -- there is no edit-then-ratify."""
    p = _me(ctx.request_context.request)
    _, tier, adm, owned = _mem_ctx(p)
    out = store.reject_memory(_conn, id, tier=tier, is_admin=adm,
                              owned_rooms=owned, actor=p.name, reason=reason)
    log.info("%s rejected memory %s: %s", p.name, id, reason)
    return out


@mcp.tool()
async def whoami(ctx: Context = None) -> str:
    """Your bus name for this session (from the X-Agent header)."""
    return _me(ctx.request_context.request).name


@mcp.tool()
async def usage(ctx: Context = None) -> str:
    """How to attach to the bus and stay reachable (identity, token, join/inbox/send,
    wake, and the exit-144 sandbox fallback), plus CHANGES: what each broker version
    changed and how to use it. Authoritative copy, served by the broker. Re-read
    whenever info() reports a new version."""
    return USAGE + CHANGES


@mcp.tool()
async def info(ctx: Context = None) -> str:
    """Reveille status banner: tool version, your bus name, and whether your wake waiter
    is attached right now. Call it on boot to confirm the bus works end to end."""
    p = _me(ctx.request_context.request)
    attached = bool(_waiters.get((p.token_id, p.name)))
    rooms = ", ".join(p.rooms.values()) or "none"
    return (f"Reveille v{__version__} -- you are '{p.name}' -- rooms: {rooms} -- "
            f"wake waiter: {'ATTACHED (real-time wake)' if attached else 'NOT ARMED (no real-time wake -- arm wake --once)'}")


def _parent_room(reply_to):
    """The room a reply lands in, taken from its parent. Never from the caller: a
    stale token must not be able to re-route a reply into a room it has lost."""
    if reply_to is None:
        return None
    first = reply_to if isinstance(reply_to, int) else list(reply_to)[0]
    r = _conn.execute("SELECT room FROM messages WHERE id=?", (first,)).fetchone()
    if not r:
        raise store.BusError(f"reply_to {first}: no such message")
    return r["room"]


@mcp.tool()
async def send(to: str, body: str, subject: str = "",
               reply_to: int | list[int] | None = None,
               attachments: list | None = None, room: str = "",
               ctx: Context = None) -> dict:
    """Send a message. to='*' broadcasts; else unicast to one agent. reply_to is a
    message id (or list, to merge branches). attachments: optional list of
    {"url","name","bytes"} dicts referencing files uploaded via POST /upload.

    room: leave it empty on a REPLY -- the room is inferred from the parent, and a
    room that disagrees is refused. On a NEW thread, leave it empty when your token
    holds exactly one room; with 2+ you must name one, or you get `room_required`
    listing them (a guess could post into the wrong room, which cannot be undone).
    Replies never cross rooms: to carry knowledge into another room, post a new root
    message there.

    Unicast pushes the recipient awake over WS; broadcasts queue silently and are
    read on each recipient's next turn. Returns {id, thread_id, parents, delivered_to}."""
    p = _me(ctx.request_context.request)
    _seen(p.name, p.rooms)
    rid = store.resolve_send_room(p.rooms, room=room or None,
                                  parent_room=_parent_room(reply_to))
    res = store.send(_conn, p.name, to, body, subject=subject, reply_to=reply_to,
                     attachments=attachments, room=rid)
    # broadcasts never wake: delivery != wakeup, kills N^2 storms. Log the woke list
    # honestly -- res["wake"] is the DELIVERY list; only unicast actually pokes it.
    woke = res["wake"] if to != store.BROADCAST else []
    _notify(rid, woke)
    _feed_push(rid, {"id": res["id"], "thread_id": res["thread_id"],
                "parents": res["parents"], "from": p.name, "to": to, "subject": subject,
                "body": body, "room": rid, "room_name": p.rooms.get(rid),
                "attachments": attachments or [], "ts_ns": time.time_ns()})
    log.info("%s send -> %s room=%s thread=%s id=%s%s delivered=%s woke=%s",
             p.name, to, p.rooms.get(rid), res["thread_id"], res["id"],
             f" reply_to={reply_to}" if reply_to is not None else "", res["wake"], woke)
    return {"id": res["id"], "thread_id": res["thread_id"], "room": rid,
            "parents": res["parents"], "delivered_to": res["wake"]}


@mcp.tool()
async def inbox(ctx: Context = None) -> dict:
    """Your unread messages (direct + broadcast) across ALL your rooms, oldest first,
    as {"messages": [...]}. Each carries `room`/`room_name` -- reply in the room it
    came from. Non-destructive: ack(message_ids) when processed."""
    p = _me(ctx.request_context.request)
    _seen(p.name, p.rooms)
    # The wake poll: acks the poke and re-arms the gate. Keyed per agent, not per room --
    # this one call covers every room, so one ring was the right number.
    _poke_pending.pop((p.token_id, p.name), None)
    msgs = store.inbox(_conn, p.name, p.rooms)
    log.info("%s inbox -> %s unread across %s room(s)", p.name, len(msgs), len(p.rooms))
    return {"messages": msgs}


@mcp.tool()
async def ack(message_ids: list[int], ctx: Context = None) -> dict:
    """Mark messages read so they leave your inbox. Idempotent. Ids outside your rooms
    or not addressed to you are ignored, not fatal -- an ack is a batch and one stale
    id must not fail the rest. Returns {acked, ignored}."""
    p = _me(ctx.request_context.request)
    out = store.ack(_conn, p.name, message_ids, p.rooms)
    log.info("%s ack %s (ignored %s)", p.name, out["acked"], len(out["ignored"]))
    return out


@mcp.tool()
async def thread(thread_id: int, ctx: Context = None) -> dict:
    """Every message in a thread, oldest first, as {"messages": [...]}. Linear view."""
    p = _me(ctx.request_context.request)
    return {"messages": store.thread(_conn, thread_id, p.rooms)}


@mcp.tool()
async def trace(message_id: int, ctx: Context = None) -> dict:
    """Track back how we got to a message: its ancestor sub-DAG as
    {"messages": [...], "edges": [[parent, child], ...]}, forks and re-links included."""
    p = _me(ctx.request_context.request)
    return store.trace(_conn, message_id, p.rooms)


@mcp.tool()
async def graph(thread_id: int, ctx: Context = None) -> dict:
    """The whole web of a thread as {"messages": [...], "edges": [[parent, child], ...]}."""
    p = _me(ctx.request_context.request)
    return store.graph(_conn, thread_id, p.rooms)


_DUR_RE = re.compile(r"\s*(\d+)\s*([smhd])\s*")


def _when_ns(spec: str):
    """Time spec -> ts_ns. Relative ('2h', '30m', '1d') or explicit ISO date/datetime
    ('2026-07-15', '2026-07-15T09:30Z'). Naive (no offset) = UTC -- the bus epoch is
    UTC, so the server's local timezone never shifts a window. Empty -> None."""
    if not spec:
        return None
    m = _DUR_RE.fullmatch(spec)
    if m:
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]
        return time.time_ns() - int(m.group(1)) * mult * 1_000_000_000
    try:
        dt = datetime.fromisoformat(spec)
    except ValueError:
        raise store.BusError(
            f"bad time {spec!r}: relative '2h'/'30m'/'1d' or ISO '2026-07-15[THH:MM][Z]'"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1_000_000_000)


@mcp.tool()
async def history(keywords: str = "", since: str = "", until: str = "",
                  with_agent: str = "", mine: bool = False,
                  thread_id: int = 0, limit: int = 200, entity: str = "",
                  ctx: Context = None) -> dict:
    """Search the full message log (read OR unread) -- this is how you review past
    coordination instead of reading the broker DB. Filters AND together:
      keywords   space-separated words, e.g. 'reboot deploy upgrade'. A message matches
                 if ANY word appears anywhere in subject/body, case-insensitive.
                 Results ranked best-first: more distinct words matched wins, then
                 more total hits, ties oldest-first.
      since/until  window endpoints; each is relative ('2h', '30m', '1d') or an
                 explicit ISO date/datetime ('2026-07-15', '2026-07-15T09:30Z').
                 Naive (no offset) = UTC; add an offset to mean anything else.
                 Bare date = midnight UTC, so one full day is since=D, until=D+1.
                 Omit either endpoint to leave that side open.
      with_agent only messages where that agent is sender or recipient
      mine=True  only messages where YOU are sender or recipient (your asks + replies to
                 you); combine with with_agent for just your 1:1 thread with them
      thread_id  restrict to one thread
      entity     only messages citing this identifier (ADR-061, #263, RunStatus,
                 run_id, disposal_run_id, repo names, proto-vX.Y.Z) -- exact on the
                 identifier class, case-insensitive. This is how you reach compounds
                 that token search fuses (CHANGES 0.2.5/0.2.6).
    Returns the most recent <=limit matches as {messages, count} (oldest-first when no
    keywords). Each message carries thread_id and parent_id -- pass thread_id to
    graph()/thread() or an id to trace() to expand the reply DAG."""
    p = _me(ctx.request_context.request)
    _seen(p.name, p.rooms)
    msgs = store.search(
        _conn, keywords=keywords.split() or None,
        since_ns=_when_ns(since), until_ns=_when_ns(until),
        involves=with_agent or None, mine_agent=(p.name if mine else None),
        thread_id=thread_id or None, limit=limit, rooms=p.rooms,
        entity=entity or None)
    log.info("%s history kw=%r since=%r until=%r with=%r mine=%s ent=%r -> %s", p.name,
             keywords, since, until, with_agent, mine, entity, len(msgs))
    return {"messages": msgs, "count": len(msgs)}


@mcp.tool()
async def presence(ctx: Context = None) -> dict:
    """Everyone across your rooms as {"agents": [...]} -- each with its url, room,
    live (recent heartbeat), and connected (reachable in real time right now: a wake.py
    attached, or -- for a human -- a browser tab holding this room's feed). Names are
    per-room, so each entry carries the room it is in."""
    p = _me(ctx.request_context.request)
    agents = store.presence(_conn, p.rooms)
    for a in agents:
        a["connected"] = _reachable(a)
        a.pop("token_id")
    return {"agents": agents}


@mcp.tool()
async def leave(room: str = "", ctx: Context = None) -> str:
    """Sign off the bus for this session -- every room by default, or just one with
    room=. Membership only: your messages stay, because authorship is history."""
    p = _me(ctx.request_context.request)
    targets = [room] if room else list(p.rooms)
    if room and room not in p.rooms:
        raise store.AccessError(f"no access to room {room}")
    store.leave(_conn, p.name, targets)
    log.info("%s left %s room(s)", p.name, len(targets))
    return f"left: {p.name}"


# ---- wake plane (WebSocket) --------------------------------------------------

def _wake_url_from(url):
    # http://host:port[/...] -> ws://host:port/wake  (https -> wss)
    base = (url or "").rstrip("/")
    if base.startswith("https://"):
        base = "wss://" + base[len("https://"):]
    elif base.startswith("http://"):
        base = "ws://" + base[len("http://"):]
    elif not base:
        return ""
    # strip any path (e.g. /mcp), keep scheme://host:port
    scheme, _, rest = base.partition("://")
    host = rest.split("/", 1)[0]
    return f"{scheme}://{host}/wake"


async def wake_ws(ws: WebSocket):
    # Accept FIRST, then validate: Starlette turns any close() issued before accept()
    # into a status-less HTTP 403, which hides why we rejected. By accepting we can send
    # a readable {"error": ...} frame the client (wake.py) prints, so an operator can
    # tell a bad token from a missing name.
    await ws.accept()
    name = ws.query_params.get("name")
    if not name:
        await ws.send_json({"error": "missing_name", "detail": "?name=<agent> is required"})
        await ws.close(code=4400)
        log.warning("wake rejected: missing_name")
        return
    # A revoked or unknown token is now a real reject. wake.py treats any {"error": ...}
    # frame as fatal and does NOT retry, so a stale token exits instead of hot-looping.
    tok = store.resolve_token(_conn, _bearer(ws))
    if not tok:
        await ws.send_json({"error": "bad_token", "detail": "unknown or revoked token"})
        await ws.close(code=4401)
        log.warning("%s wake rejected: bad_token", name)
        return
    # Same binding check as the HTTP principal (ruled in 8371), same distinguishable-
    # reject discipline as bad_token/missing_name: the operator must be able to tell
    # a forged name from a dead credential without reading broker logs.
    if tok.get("agent_name") and name != tok["agent_name"]:
        await ws.send_json({"error": "name_mismatch",
                            "detail": f"token is bound to {tok['agent_name']!r}"})
        await ws.close(code=4403)
        log.warning("%s wake rejected: name_mismatch (bound to %s)", name, tok["agent_name"])
        return
    rooms = store.rooms_for_token(_conn, tok["id"])
    key = (tok["id"], name)
    _seen(name, rooms)
    log.info("%s wake connected (%s room(s))", name, len(rooms))
    q: asyncio.Queue = asyncio.Queue()
    # DES-003 2.3: one wake attachment per agent -- a SECOND attachment
    # SUPERSEDES the first (supersede, not refuse: a daemon respawned after a
    # crash must reclaim its slot while the old TCP lingers half-open). The
    # old holder gets a `superseded` frame and closes; with reveille-waked
    # there should never be two live daemons -- this makes the harm of a
    # violation zero instead of relying on there never being one.
    for old in list(_waiters.get(key, ())):
        old.put_nowait(_SUPERSEDE)
        log.info("%s wake superseded (newer attachment)", name)
    _waiters.setdefault(key, set()).add(q)
    try:
        # Ring once if DIRECT mail is already waiting at connect, so a just-attached
        # client does not miss it. Broadcasts never ring (here or on arrival) -- they
        # drain on the agent's next natural turn; ringing them at reconnect made every
        # daemon restart storm the whole fleet. We do NOT re-ring on still-unacked
        # backlog -- only on new arrivals.
        backlog = [m for m in store.inbox(_conn, name, rooms)
                   if m["to"] != store.BROADCAST]
        if backlog and _poke_ok(key):
            _poke_pending[key] = time.time_ns()
            await ws.send_json({"wake": True, "reason": "backlog", "unread": len(backlog)})
            log.info("%s wake ring (backlog %s)", name, len(backlog))
        # Then stay connected and push one frame per new message until the client drops.
        # A --once waiter exits after its first frame and reconnects on re-arm; the poke
        # gate keeps that from storming (unacked backlog rings once, then waits for
        # inbox()). Streaming clients just hold the socket across rings.
        while True:
            recv = asyncio.create_task(ws.receive_text())
            woke = asyncio.create_task(q.get())
            done, pending = await asyncio.wait({recv, woke}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            # Retrieve each task's result/exception so none is "never retrieved": a client
            # that drops while parked makes recv raise WebSocketDisconnect inside its task.
            gone = False
            for t in (recv, woke):
                try:
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                except WebSocketDisconnect:
                    gone = True
            if gone:
                break
            if recv in done:  # client data = its heartbeat; keep the agent LIVE
                _seen(name, rooms)
                continue
            # A notify fired. Coalesce any other queued notifies into this one ring,
            # and swallow it entirely while a poke is already outstanding (the agent
            # has an untyped prompt pending; its next inbox() pulls this mail anyway).
            vals = [woke.result()] if woke in done and not woke.cancelled() else []
            while not q.empty():
                vals.append(q.get_nowait())
            if any(v is _SUPERSEDE for v in vals):
                await ws.send_json({"wake": False, "reason": "superseded",
                    "note": "a newer wake attachment for this agent took the "
                            "slot -- this holder should exit, not reconnect"})
                break
            if any(v is _SHUTDOWN for v in vals):
                await ws.send_json({"wake": False, "reason": "shutdown",
                    "note": "broker restarting -- do not reply, just re-arm your "
                            "waiter; the broker will be back shortly"})
                break
            if not _poke_ok(key):
                continue
            _poke_pending[key] = time.time_ns()
            n = len(store.inbox(_conn, name, rooms))
            await ws.send_json({"wake": True, "reason": "message", "unread": n})
            log.info("%s wake ring (%s unread)", name, n)
    except WebSocketDisconnect:
        pass
    finally:
        bucket = _waiters.get(key)
        if bucket:
            bucket.discard(q)
            if not bucket:
                _waiters.pop(key, None)
        log.info("%s wake disconnected", name)


# ---- app + auth + run --------------------------------------------------------

async def health(_request):
    return PlainTextResponse("ok")


async def version_http(_request):
    return PlainTextResponse(__version__)


async def usage_http(_request):
    return PlainTextResponse(USAGE + CHANGES)


# ---- web API: the chat UI (and any script) drives the bus over plain HTTP ----------
# Every endpoint resolves a Principal (session cookie or bearer token) and scopes to the
# rooms that principal actually holds. There is no open room and no anonymous access.

def _guard(fn):
    """Turn the store's auth vocabulary into HTTP status codes, in one place.
    401 = no/!valid credential. 403 = valid principal, not for this room.
    400 room_required = 2+ rooms and none named -- an ambiguity, NOT an authz failure,
    so it must not train the UI to throw up a login card."""
    async def wrapped(request):
        try:
            return await fn(request)
        except store.AuthError as e:
            return JSONResponse({"error": "unauthorized", "detail": str(e)}, status_code=401)
        except store.AccessError as e:
            return JSONResponse({"error": "forbidden", "detail": str(e)}, status_code=403)
        except store.AmbiguousRoom as e:
            return JSONResponse({"error": "room_required", "rooms": e.rooms}, status_code=400)
        except store.BusError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
    wrapped.__name__ = fn.__name__
    return wrapped


def _scope(request, p):
    """The rooms this call touches: one named room (must be in reach), else all of them."""
    room = request.query_params.get("room") or ""
    if room:
        if room not in p.rooms:
            raise store.AccessError(f"no access to room {room}")
        return {room: p.rooms[room]}
    return p.rooms


@_guard
async def messages_http(request):
    """GET /messages?since_id=N&limit=M[&room=] -> {"messages": [...]} oldest-first.
    since_id=0 (default) returns the most recent `limit`; the UI uses since_id to
    fill the gap after a feed reconnect."""
    p = _principal(request)
    since_id = int(request.query_params.get("since_id") or 0)
    limit = int(request.query_params.get("limit") or 200)
    return JSONResponse({"messages": store.tail(_conn, since_id=since_id, limit=limit,
                                                rooms=_scope(request, p))})


@_guard
async def presence_http(request):
    """GET /presence[&room=] -> same view as the presence() tool, for the UI header."""
    p = _principal(request)
    rooms = _scope(request, p)
    me = request.query_params.get("me") or ""
    if me and rooms:  # the poll doubles as the web identity's heartbeat, so it shows live
        with contextlib.suppress(store.BusError):
            rid = next(iter(rooms))
            if store.known(_conn, me, [rid]):
                store.touch(_conn, me, [rid])
            else:
                store.join(_conn, me, tag=f"web:{me}", room_id=rid, fresh=True)
    agents = store.presence(_conn, rooms)
    for a in agents:
        a["connected"] = _reachable(a)
        a.pop("token_id")
    return JSONResponse({"agents": agents})


@_guard
async def send_http(request):
    """POST /send[?room=] {from?, to, subject?, body, reply_to?, attachments?, room?, shout?}.
    Same semantics as the MCP send tool: unicast rings (gate applies), broadcast queues,
    a reply's room comes from its parent. ?room= scopes the send like every other web
    endpoint -- the composer sends into the room the browser is looking at, so a user
    with 2+ rooms is not asked room_required for a room they already picked. shout=true with to='*' is the HUMAN page-all:
    the broadcast also RINGS every live agent in THAT ONE room, once, through the poke
    gate. Deliberately web-only -- the MCP send tool has no shout, so agents cannot emit
    one. A shout pages one room, never every room you can see: cross-room paging is the
    rare orchestration case and must be deliberate.
    Unknown senders are auto-joined; an existing agent's name is used as-is."""
    p = _principal(request)
    try:
        d = await request.json()
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)
    sender = (d.get("from") or p.name).strip()
    to = (d.get("to") or store.BROADCAST).strip()
    body = d.get("body") or ""
    if not body:
        return JSONResponse({"error": "empty body"}, status_code=400)
    rid = store.resolve_send_room(_scope(request, p), room=d.get("room") or None,
                                  parent_room=_parent_room(d.get("reply_to")))
    if not store.known(_conn, sender, [rid]):
        store.join(_conn, sender, tag=f"web:{sender}", room_id=rid)
    res = store.send(_conn, sender, to, body,
                     subject=d.get("subject") or "", reply_to=d.get("reply_to"),
                     attachments=d.get("attachments"), room=rid)
    shout = bool(d.get("shout")) and to == store.BROADCAST
    woke = res["wake"] if (to != store.BROADCAST or shout) else []
    _notify(rid, woke)
    _feed_push(rid, {"id": res["id"], "thread_id": res["thread_id"],
                "parents": res["parents"], "from": sender, "to": to,
                "subject": d.get("subject") or "", "body": body,
                "room": rid, "room_name": p.rooms.get(rid),
                "attachments": d.get("attachments") or [], "ts_ns": time.time_ns()})
    log.info("%s send(web)%s -> %s room=%s thread=%s id=%s delivered=%s woke=%s",
             sender, " SHOUT" if shout else "", to, p.rooms.get(rid), res["thread_id"],
             res["id"], res["wake"], woke)
    return JSONResponse({"id": res["id"], "thread_id": res["thread_id"], "room": rid,
                         "delivered_to": res["wake"]})


_files_dir = None  # set in main(): <db dir>/files -- attachments live next to the broker db
_FNAME_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_UPLOAD = 25 * 1024 * 1024

# Total bytes of attachments this broker will hold. 0 = unlimited, because a homebrew box
# must not surprise its owner with a cap he never asked for. A hosted free tier sets it per
# tenant in the unit file, which is where a business rule belongs -- not in the code every
# self-hoster runs.
# This counts UPLOADS, not the database: messages are ~5KB and bounded by retention, while
# /upload is the only place a single caller can write gigabytes.
QUOTA_BYTES = int(os.environ.get("REVEILLE_QUOTA_BYTES", "0"))


def _files_used():
    """Bytes currently stored. Read from the filesystem rather than a counter: a counter
    drifts the first time a file is removed by anything that did not decrement it, and it
    drifts silently -- the failure would be a tenant locked out of uploading with space to
    spare, which reads as a broker bug."""
    with os.scandir(_files_dir) as it:
        return sum(f.stat().st_size for f in it if f.is_file())


def _upload_refusal(used, size):
    """Why this upload cannot be stored, or None. Pure, so the limits are testable without
    a socket -- and so the two callers below cannot drift apart on the arithmetic."""
    if size > MAX_UPLOAD:
        return f"too large ({size >> 20}MB, cap {MAX_UPLOAD >> 20}MB)"
    if QUOTA_BYTES and used + size > QUOTA_BYTES:
        return (f"storage full ({used >> 20}MB of {QUOTA_BYTES >> 20}MB used). "
                f"Delete attachments or raise the tenant's quota.")
    return None


@_guard
async def upload_http(request):
    """POST /upload?name=<filename>[&room=] with the raw file bytes as the body. Stores it
    under a unique name, records which room it belongs to, and returns
    {"url": "/files/<stored>", "name": <original>, "bytes": n}.

    413 comes in two flavours and they want different reactions: "too large" is this ONE
    file over the 25MB cap (split it, or link it); "storage full" is the broker's whole
    attachment quota, where retrying achieves nothing. Both refuse before storing, so a
    413 never leaves half a file behind. Unlimited unless the operator sets a quota.

    Pass the returned dict in the `attachments` list on send. The attachments FIELD is
    the only form -- never write "[file: ...]" markers into a body; they are plain text
    and no consumer parses them.
    Agents ingest on demand: curl -H 'Authorization: Bearer $REVEILLE_TOKEN' <broker><url>"""
    p = _principal(request)
    rid = store.resolve_send_room(p.rooms, room=request.query_params.get("room") or None)
    name = _FNAME_RE.sub("_", request.query_params.get("name") or "file.bin")[-80:]
    used = _files_used() if QUOTA_BYTES else 0

    # Content-Length is a CLAIM. Believing it is how you refuse a 5GB body politely after
    # reading all 5GB of it -- the old code did exactly that: request.body() buffered the
    # whole thing before the cap was consulted, so the cap protected nothing and the tenant
    # was OOM-killed (MemoryMax=512M) before it could answer 413. So: refuse the honest
    # liar up front, then keep counting while the bytes actually arrive, because a chunked
    # body carries no length at all.
    declared = int(request.headers.get("content-length") or 0)
    if (why := _upload_refusal(used, declared)):
        return JSONResponse({"error": why}, status_code=413)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if (why := _upload_refusal(used, len(buf))):
            return JSONResponse({"error": why}, status_code=413)   # hangs up mid-stream
    data = bytes(buf)
    if not data:
        return JSONResponse({"error": "empty body"}, status_code=400)
    stored = f"{time.time_ns() // 1_000_000}-{name}"
    (_files_dir / stored).write_bytes(data)
    store.record_file(_conn, stored, rid, p.name)
    log.info("%s upload %s (%s bytes) -> /files/%s", p.name, name, len(data), stored)
    return JSONResponse({"url": f"/files/{stored}", "name": name, "bytes": len(data)})


@_guard
async def files_http(request):
    """GET /files/<stored> -> the attachment bytes, if you are in its room. Without the
    room check this is a world-readable blob store to anyone who learns a filename."""
    p = _principal(request)
    fname = _FNAME_RE.sub("_", request.path_params["fname"])
    rid = store.file_room(_conn, fname)
    if rid is None or rid not in p.rooms:
        return JSONResponse({"error": "not found"}, status_code=404)
    path = _files_dir / fname
    if not path.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(path)


@_guard
async def search_http(request):
    """GET /search?keywords=&since=&until=&agent=&thread_id=&limit=[&room=][&entity=]
    -> the whole log, same semantics as the history() tool (keywords ranked;
    naive ISO = UTC; entity= exact on the extracted identifier class)."""
    p = _principal(request)
    q = request.query_params
    try:
        msgs = store.search(
            _conn,
            keywords=(q.get("keywords") or "").split() or None,
            since_ns=_when_ns(q.get("since") or ""),
            until_ns=_when_ns(q.get("until") or ""),
            involves=q.get("agent") or None,
            thread_id=int(q.get("thread_id") or 0) or None,
            limit=int(q.get("limit") or 500), rooms=_scope(request, p),
            entity=q.get("entity") or None)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"messages": msgs, "count": len(msgs)})


@_guard
async def delete_http(request):
    """DELETE /message/<mid>?from=<name> -- retract your own message if NOBODY has
    read or replied to it yet (the mistaken-broadcast eraser). Room-scoped."""
    p = _principal(request)
    sender = request.query_params.get("from") or p.name
    mid = int(request.path_params["mid"])
    try:
        store.delete_if_unseen(_conn, mid, sender, p.rooms)
    except store.BusError as e:
        return JSONResponse({"error": str(e),
                             "readers": store.readers(_conn, mid, exclude=sender)},
                            status_code=409)
    for rid in p.rooms:
        _feed_push(rid, {"deleted": mid})
    log.info("%s retracted message %s (unseen)", sender, mid)
    return JSONResponse({"deleted": mid})


async def feed_ws(ws: WebSocket):
    """WS /feed: pushes every bus message as one JSON frame -- the UI's live wire.
    Cookie rides the handshake automatically; a bad credential is rejected."""
    await ws.accept()
    try:
        p = _principal(ws)
    except store.AuthError:
        await ws.send_json({"error": "bad_token"})
        await ws.close(code=4401)
        return
    q: asyncio.Queue = asyncio.Queue()
    room = ws.query_params.get("room") or ""
    _feed[q] = (room if room in p.rooms else (next(iter(p.rooms)) if p.rooms else ""),
                p.name)
    log.info("%s feed connected (%s watching)", p.name, len(_feed))
    try:
        # ponytail: a dropped browser parked on q.get() is only reaped when the next
        # message's send fails -- bounded leak, self-cleaning, fine for a LAN UI.
        while True:
            await ws.send_json(await q.get())
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _feed.pop(q, None)
        log.info("feed disconnected (%s watching)", len(_feed))


# ---- web chat: live color-coded feed of all bus traffic + composer ----------------

WEBCHAT = r"""<!doctype html><html><head><meta charset="utf-8"><title>Reveille bus</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 :root{
  color-scheme:dark;
  --bg:#0e1116;--rail:#0a0d12;--card:#151a21;--line:#242c37;--hover:#1a212b;
  --fg:#dce3ec;--dim:#8b95a3;--faint:#5a6472;--gold:#e2a63d;--green:#3ecf6a;
 }
 *{box-sizing:border-box;margin:0}
 html,body{height:100%}
 body{background:var(--bg);color:var(--fg);
  font:14px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
  display:grid;grid-template-columns:232px 1fr}

 /* ---- sidebar ---- */
 #rail{background:var(--rail);border-right:1px solid var(--line);display:flex;
  flex-direction:column;min-height:0}
 #brand{padding:1rem 1rem .8rem;border-bottom:1px solid var(--line)}
 #brand h1{font-size:1rem;letter-spacing:.22em;color:var(--gold);font-weight:800}
 #brand small{color:var(--faint);display:flex;align-items:center;gap:.4em;margin-top:.2rem}
 #status{width:.55em;height:.55em;border-radius:50%;background:var(--faint)}
 #status.on{background:var(--green)}
 #rail h2{font-size:.68rem;letter-spacing:.14em;color:var(--faint);padding:.9rem 1rem .4rem}
 #fmode{display:flex;margin:0 1rem .45rem;border:1px solid var(--line);border-radius:7px;
  overflow:hidden}
 #fmode button{flex:1;background:none;border:0;color:var(--faint);font:inherit;
  font-size:.72rem;letter-spacing:.08em;padding:.28rem 0;cursor:pointer}
 #fmode button.on{background:var(--hover);color:var(--gold)}
 #agents{overflow-y:auto;flex:1;padding:0 .5rem .8rem}
 .agent{display:flex;align-items:center;gap:.55rem;padding:.34rem .55rem;border-radius:7px;
  cursor:pointer;color:var(--dim);font-size:.86rem}
 .agent:hover{background:var(--hover)}
 .agent.sel{background:var(--hover);outline:1px solid var(--line)}
 .agent .swatch{width:9px;height:9px;border-radius:3px;flex:none}
 .agent .nm{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
 .agent.live .nm{color:var(--fg)}
 /* Three states, read at a glance without a legend: hollow ring = stale, filled gold =
    live but not reachable in real time, filled green + halo = reachable now.
    Deliberately NOT pulsing. Green means "fine, nothing needed"; motion means "look at
    me" -- and a rail of six agents all breathing is ambient anxiety that trains the eye
    to ignore the very dot it is meant to draw. The halo makes green read as LIT while
    staying still, and the one thing here that does pulse (an armed BLAST) keeps meaning
    what motion should mean. */
 .agent .st{width:7px;height:7px;border-radius:50%;flex:none;background:transparent;
  border:1px solid var(--faint)}
 .agent.live .st{border-color:var(--gold);background:var(--gold)}
 .agent.conn .st{border-color:var(--green);background:var(--green);
  box-shadow:0 0 0 2px rgba(62,207,106,.2)}
 .agent.allrow{border-bottom:1px solid var(--line);border-radius:7px 7px 0 0;
  margin-bottom:.3rem;padding-bottom:.45rem}
 .agent.allrow .nm{color:var(--fg)}
 /* The scope row says which filter is ON in words, not in a dot: gold name = you are
    seeing everyone. Its right slot carries the agent count -- the fact worth knowing
    there, and it cannot be mistaken for a status light. */
 .agent.allrow.sel .nm{color:var(--gold);font-weight:700}
 .agent .cnt{font-size:.68rem;color:var(--faint);flex:none;
  font-variant-numeric:tabular-nums}
 .agent.allrow.sel .cnt{color:var(--gold)}
 /* The card is the only thing anchored to the bottom of the rail, so its menu opens
    upward from it -- same popup shape as the composer's recipient picker. */
 #meWrap{position:relative;margin-top:auto}
 #meMenu{display:none;position:absolute;bottom:calc(100% + .3rem);left:.6rem;right:.6rem;
  background:var(--card);border:1px solid var(--line);border-radius:10px;padding:.3rem;
  z-index:10;box-shadow:0 8px 30px rgba(0,0,0,.45)}
 #meMenu.on{display:block}
 .mi{padding:.45rem .6rem;border-radius:6px;font-size:.82rem;color:var(--dim);cursor:pointer}
 .mi:hover{background:var(--hover);color:var(--fg)}
 #miLogout:hover{color:#e8555a}
 #meCard{display:flex;align-items:center;gap:.6rem;padding:.65rem .9rem;
  border-top:1px solid var(--line);cursor:pointer}
 #meCard:hover{background:var(--hover)}
 #meCard .avatar{width:30px;height:30px;font-size:.72rem}
 #meCard .mename{font-size:.85rem;font-weight:700;line-height:1.2}
 /* The room line is a CONTROL, not a label. It used to render in --faint -- the same
    colour as dead metadata -- next to a gear that reads as "settings", so nothing on the
    card said "this is how you change room". */
 #meCard .meroom{font-size:.72rem;color:var(--dim);line-height:1.2;
  display:flex;align-items:center;gap:.25rem}
 #meCard:hover .meroom{color:var(--gold)}
 #meCard .chev{font-size:.6rem;opacity:.8}
 #meCard .gear{margin-left:auto;color:var(--faint);font-size:.9rem}
 #meCard:hover .gear{color:var(--fg)}
 #meDot{width:.55em;height:.55em;border-radius:50%;background:var(--faint);flex:none}
 #meDot.on{background:var(--green)}

 /* ---- main column ---- */
 #main{display:flex;flex-direction:column;min-width:0;min-height:0;position:relative}
 #spin{display:none;position:absolute;inset:0;align-items:center;justify-content:center;
  background:rgba(14,17,22,.55);backdrop-filter:blur(1px);z-index:6}
 #spin.on{display:flex}
 #spin .ring{width:44px;height:44px;border-radius:50%;border:3px solid var(--line);
  border-top-color:var(--gold);animation:spin .8s linear infinite}
 @keyframes spin{to{transform:rotate(360deg)}}
 #toasts{position:fixed;top:1rem;left:50%;transform:translateX(-50%);z-index:30;
  display:flex;flex-direction:column;gap:.5rem;align-items:center;pointer-events:none}
 .toast{background:var(--card);border:1px solid #e8555a;color:var(--fg);
  border-radius:10px;padding:.55rem 1.1rem;font-size:.86rem;max-width:34rem;
  box-shadow:0 8px 30px rgba(0,0,0,.5);cursor:pointer;pointer-events:auto;
  animation:toastin .18s ease-out}
 .toast.info{border-color:var(--gold)}
 @keyframes toastin{from{opacity:0;transform:translateY(-8px)}to{opacity:1}}
 #top{display:flex;align-items:center;gap:.7rem;padding:.55rem 1.1rem;
  border-bottom:1px solid var(--line);background:var(--bg)}
 #filter{flex:1;max-width:26rem;background:var(--rail);border:1px solid var(--line);
  color:var(--fg);padding:.38rem .8rem;border-radius:7px;font:inherit;font-size:.86rem}
 #filter:focus{outline:none;border-color:var(--gold)}
 #filterState{font-size:.78rem;color:var(--gold);cursor:pointer;display:none}
 #histBtn{background:none;border:1px solid var(--line);color:var(--dim);border-radius:7px;
  padding:.34rem .9rem;cursor:pointer;font:inherit;font-size:.82rem}
 #histBtn.on,#histBtn:hover{color:var(--gold);border-color:var(--gold)}
 #histBar{display:none;gap:.8rem;padding:.55rem 1.1rem .7rem;
  border-bottom:1px solid var(--line);background:var(--rail);flex-wrap:wrap;
  align-items:flex-end}
 #histBar.on{display:flex}
 #histBar .hf{display:flex;flex-direction:column;gap:.2rem}
 #histBar .hf label{color:var(--faint);font-size:.66rem;letter-spacing:.12em;
  text-transform:uppercase}
 #histBar input,#histBar select{background:var(--bg);border:1px solid var(--line);
  color:var(--fg);padding:0 .6rem;border-radius:7px;font:inherit;font-size:.83rem;
  height:2.2rem}
 #histBar input:focus,#histBar select:focus{outline:none;border-color:var(--gold)}
 #histBar input{accent-color:var(--gold)}
 #histBar input::-webkit-calendar-picker-indicator{
  filter:invert(70%) sepia(60%) saturate(500%) hue-rotate(360deg);cursor:pointer}
 .qr{display:flex;gap:.35rem;align-items:center;padding-bottom:.15rem}
 .qr button{background:none;border:1px solid var(--line);color:var(--dim);
  border-radius:2em;padding:.28rem .8rem;font:inherit;font-size:.78rem;cursor:pointer}
 .qr button:hover{color:var(--gold);border-color:var(--gold)}
 .qr button.on{background:var(--gold);border-color:var(--gold);color:#14161a;font-weight:700}
 #hGo{background:var(--gold);border:0;color:#14161a;font-weight:700;
  border-radius:7px;padding:0 1.4rem;height:2.2rem;cursor:pointer;font:inherit;
  font-size:.83rem}
 #histBar .qr button{background:none;border:1px solid var(--line);color:var(--dim)}
 #histBar .qr button:hover{color:var(--gold);border-color:var(--gold)}
 #histBar .qr button.on{background:var(--gold);border-color:var(--gold);
  color:#14161a;font-weight:700}
 #histInfo{display:none;justify-content:space-between;align-items:center;
  margin:.6rem 2.2rem 0;padding:.4rem .9rem;border:1px solid var(--gold);border-radius:8px;
  color:var(--gold);font-size:.82rem}
 #histInfo.on{display:flex}
 #histInfo span b{cursor:pointer;text-decoration:underline}
 .mid{cursor:pointer}

 #feed{flex:1;overflow-y:auto;min-height:0;padding:1rem 0 1.4rem}
 #feed>.inner{max-width:none;margin:0;padding:0 2.2rem}
 .day{display:flex;align-items:center;gap:.9rem;color:var(--faint);
  font-size:.72rem;letter-spacing:.12em;margin:1.1rem 0 .8rem}
 .day::before,.day::after{content:"";flex:1;height:1px;background:var(--line)}

 .row{display:grid;grid-template-columns:42px 1fr;gap:.75rem;padding:.16rem .5rem;
  border-radius:8px}
 .row:hover{background:var(--hover)}
 .row .gutter{padding-top:.18rem}
 .avatar{width:34px;height:34px;border-radius:8px;display:flex;align-items:center;
  justify-content:center;font-weight:700;font-size:.8rem}
 .row.cont{margin-top:-.1rem}
 .row.cont .gutter{visibility:hidden}
 .head{display:flex;align-items:baseline;gap:.55rem;flex-wrap:wrap}
 .head .who{font-weight:700;font-size:.9rem}
 .head .arrow{color:var(--faint);font-size:.8rem}
 .head .toname{font-size:.8rem;font-weight:600}
 .head .all{font-size:.68rem;font-weight:700;color:var(--gold);border:1px solid var(--gold);
  border-radius:4px;padding:0 .35em;letter-spacing:.08em}
 .head time{color:var(--faint);font-size:.74rem}
 .head .mid{color:var(--faint);font-size:.72rem;opacity:0;margin-left:auto}
 .row:hover .mid{opacity:1}
 .head .del{color:#e8555a;font-size:.78rem;opacity:0;cursor:pointer;padding:0 .2rem}
 .row:hover .del{opacity:1}
 .head .del:hover{font-weight:900}
 .subj{font-weight:650;margin:.12rem 0 .05rem;font-size:.92rem}
 .body{color:#c3ccd8;white-space:pre-wrap;word-break:break-word;
  font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  margin:.1rem 0 .3rem}
 .row.bcast{border-left:2px solid var(--gold);border-radius:0 8px 8px 0}
 .atts{display:flex;gap:.5rem;flex-wrap:wrap;margin:.3rem 0 .35rem}
 .atts img.att{display:block;max-width:min(380px,100%);max-height:260px;width:auto;
  height:auto;border-radius:8px;border:1px solid var(--line);cursor:zoom-in}
 .atts a.attlink{color:var(--gold)}
 /* ---- rendered markdown attachments ---- */
 .mdview{flex-basis:100%;background:var(--rail);border:1px solid var(--line);
  border-radius:10px;padding:.8rem 1rem;max-height:26rem;overflow-y:auto;
  font-size:.82rem;line-height:1.55;color:var(--fg)}
 .mdview h1,.mdview h2,.mdview h3,.mdview h4,.mdview h5,.mdview h6{
  color:var(--gold);margin:.7em 0 .3em;line-height:1.25}
 .mdview h1{font-size:1.05rem}.mdview h2{font-size:.95rem}.mdview h3{font-size:.88rem}
 .mdview h4,.mdview h5,.mdview h6{font-size:.82rem}
 .mdview p{margin:.35em 0}
 .mdview ul,.mdview ol{margin:.35em 0;padding-left:1.4em}
 .mdview code{background:var(--hover);border:1px solid var(--line);border-radius:4px;
  padding:.05em .35em;font-size:.78rem}
 .mdview pre{background:var(--hover);border:1px solid var(--line);border-radius:8px;
  padding:.6rem .8rem;margin:.45em 0;overflow-x:auto}
 .mdview pre code{background:none;border:0;padding:0;white-space:pre}
 .mdview blockquote{border-left:3px solid var(--gold);margin:.45em 0;
  padding:.1em 0 .1em .8em;color:var(--dim)}
 .mdview table{border-collapse:collapse;margin:.45em 0;font-size:.78rem}
 .mdview th,.mdview td{border:1px solid var(--line);padding:.3em .65em;text-align:left}
 .mdview th{background:var(--hover);color:var(--gold)}
 .mdview a{color:var(--gold)}
 .mdview hr{border:0;border-top:1px solid var(--line);margin:.7em 0}
 #empty{color:var(--faint);text-align:center;margin-top:4rem}

 #jump{position:fixed;right:1.6rem;bottom:8.2rem;display:none;background:var(--card);
  border:1px solid var(--line);color:var(--gold);padding:.35rem 1rem;border-radius:2em;
  cursor:pointer;font:inherit;font-size:.8rem}

 /* ---- composer ---- */
 #composerWrap{border-top:1px solid var(--line);background:var(--bg);padding:.8rem 1.1rem 1rem}
 #composer{max-width:none;margin:0 1.1rem;background:var(--card);border:1px solid var(--line);
  border-radius:12px;padding:.55rem .9rem .6rem;display:flex;flex-direction:column;gap:.15rem}
 .ctop{display:flex;gap:.7rem;align-items:center;border-bottom:1px solid var(--line);
  padding-bottom:.45rem}
 .cbottom{display:flex;align-items:center;gap:.6rem;padding-top:.4rem;
  border-top:1px solid var(--line)}
 #composer input,#composer textarea{background:none;border:0;color:var(--fg);font:inherit}
 #composer input:focus,#composer textarea:focus{outline:none}
 #replyChip{display:none;color:var(--gold);font-size:.78rem;border:1px solid var(--gold);
  border-radius:1em;padding:.1rem .6rem;cursor:pointer;white-space:nowrap}
 #replyChip.on{display:inline-block}
 #attachBtn{background:none;border:1px solid var(--line);color:var(--dim);border-radius:2em;
  padding:.22rem .9rem;cursor:pointer;font:inherit;font-size:.78rem}
 #attachBtn:hover{color:var(--gold);border-color:var(--gold)}
 #attchips{display:flex;gap:.5rem;flex-wrap:wrap}
 .attTile{position:relative;width:58px;height:58px;border-radius:10px;flex:none;
  border:1px solid var(--line);background:var(--rail);overflow:hidden;
  display:flex;flex-direction:column;align-items:center;justify-content:center;gap:.1rem}
 .attTile img{width:100%;height:100%;object-fit:cover}
 .attTile .ext{color:var(--gold);font-weight:800;font-size:.7rem;letter-spacing:.04em}
 .attTile .fn{color:var(--faint);font-size:.55rem;max-width:52px;overflow:hidden;
  text-overflow:ellipsis;white-space:nowrap}
 .attTile .rm{position:absolute;top:2px;right:2px;width:17px;height:17px;
  border-radius:50%;background:rgba(10,13,18,.85);border:1px solid var(--line);
  color:var(--fg);font-size:.62rem;line-height:15px;text-align:center;cursor:pointer;
  opacity:0;transition:opacity .12s}
 .attTile:hover .rm{opacity:1}
 .attTile .rm:hover{color:var(--gold);border-color:var(--gold)}
 .khint{color:var(--faint);font-size:.72rem;margin-left:auto}
 #composer.drag{border-color:var(--gold);background:var(--hover)}
 #composer.drag::after{content:"drop files to attach";color:var(--gold);
  font-size:.78rem;text-align:center;padding:.2rem}
 #composer:focus-within{border-color:var(--gold)}
 #composer input,#composer textarea{background:var(--rail);border:1px solid var(--line);
  color:var(--fg);padding:.4rem .65rem;border-radius:6px;font:inherit;font-size:.85rem}
 #composer input:focus,#composer textarea:focus{outline:none;border-color:var(--gold)}
 #body{width:100%;resize:vertical;min-height:6.5em;padding:.5rem .1rem;
  font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
 #subject{flex:1;font-size:.85rem;color:var(--fg)}
 #subject::placeholder,#body::placeholder{color:var(--faint)}
 #send{background:var(--gold);color:#14161a;border:0;border-radius:2em;
  font-weight:800;padding:.4rem 1.8rem;cursor:pointer;font:inherit}
 #send:hover{filter:brightness(1.1)}

 /* ---- notice modal ---- */
 #dlg{position:fixed;inset:0;background:rgba(6,8,11,.82);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:25}
 #dlg.on{display:flex}
 #dlgCard{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:1.6rem 1.8rem;width:26rem;max-width:92vw;box-shadow:0 12px 48px rgba(0,0,0,.5)}
 #dlgTitle{font-size:.95rem;font-weight:800;color:#e8555a;letter-spacing:.04em;
  margin-bottom:.4rem}
 #dlgMsg{color:var(--dim);font-size:.82rem;line-height:1.5;margin-bottom:1rem}
 /* Settings panel. Deliberately NOT the retract dialog: that one is an error surface
    (red title, big confirm button) and borrowing it made every room look like a warning. */
 #pan{position:fixed;inset:0;background:rgba(6,8,11,.82);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:26}
 #pan.on{display:flex}
 #panCard{background:var(--card);border:1px solid var(--line);border-radius:14px;
  width:32rem;max-width:94vw;box-shadow:0 12px 48px rgba(0,0,0,.5);overflow:hidden;
  display:flex;flex-direction:column;max-height:82vh}
 #panTabs{display:flex;align-items:center;gap:.15rem;padding:.5rem .6rem 0;
  border-bottom:1px solid var(--line);flex:none}
 .tab{background:none;border:none;border-bottom:2px solid transparent;color:var(--dim);
  font:inherit;font-size:.78rem;font-weight:600;padding:.5rem .75rem;cursor:pointer;
  margin-bottom:-1px}
 .tab:hover{color:var(--fg)}
 .tab.on{color:var(--gold);border-bottom-color:var(--gold)}
 /* S6 memory plane: agent-authored text renders in these quoted blocks ONLY --
    visually framed as data with an author label above, never as chrome (14.3) */
 .quote{border-left:2px solid var(--line);margin:.15rem 0 .5rem;padding:.25rem .6rem;
  color:var(--dim);font-size:.8rem;white-space:pre-wrap;word-break:break-word}
 .memMeta{font-size:.72rem;color:var(--faint)}
 .qcount{background:var(--gold);color:#000;border-radius:.6rem;padding:0 .45rem;
  font-size:.68rem;font-weight:700;margin-left:.4rem}
 #panX{margin-left:auto;background:none;border:none;color:var(--faint);cursor:pointer;
  font-size:1.1rem;line-height:1;padding:.3rem .5rem;border-radius:6px}
 #panX:hover{color:var(--fg);background:var(--hover)}
 #panBody{padding:.5rem 1.1rem 1.1rem;overflow-y:auto;text-align:left}
 #panFoot{border-top:1px solid var(--line);padding:.5rem 1.1rem;display:flex;
  align-items:center;gap:.5rem;font-size:.72rem;color:var(--faint);flex:none}
 #panFoot .lnk{margin-left:auto;color:var(--dim);cursor:pointer}
 #panFoot .lnk:hover{color:var(--fg);text-decoration:underline}
 #dlgBody{text-align:left;max-height:60vh;overflow-y:auto}
 .pSec{font-size:.62rem;letter-spacing:.12em;color:var(--faint);margin:.9rem 0 .35rem}
 .pRow{display:flex;align-items:center;gap:.4rem;padding:.4rem 0;flex-wrap:wrap;
  border-bottom:1px solid rgba(36,44,55,.5)}
 .pRow:last-child{border-bottom:none}
 .pRow b{flex:1;font-size:.82rem;font-weight:600;min-width:8rem}
 .pRow .on{color:var(--green)}
 .pDim{color:var(--faint);font-size:.72rem}
 .pRow input{flex:1;min-width:7rem;background:var(--bg);border:1px solid var(--line);
  border-radius:6px;color:var(--fg);padding:.3rem .5rem;font:inherit;font-size:.78rem}
 .pRow button{background:var(--chip);border:1px solid var(--line);border-radius:6px;
  color:var(--fg);padding:.25rem .55rem;font:inherit;font-size:.72rem;cursor:pointer}
 .pRow button:hover{border-color:var(--accent)}
 .pRow button.danger{color:#e8555a;border-color:#5a2b2d}
 /* Inline confirm. Never window.prompt/confirm: they are unstyled, block the page, are
    suppressible by the browser, and prompt() renders a typed PASSWORD in cleartext. */
 /* A room list has exactly one current item -- that is a SELECTOR, not a row of buttons.
    The old design used the word "open" for BOTH the state ("you are in it") and the action
    ("go there"), on the same row. The indicator carries the state now, so the word is gone. */
 .pRow.sel{background:rgba(226,166,61,.07)}
 .pRow.sel b{color:var(--gold)}
 .rSel{display:flex;align-items:center;gap:.5rem;flex:1;cursor:pointer;min-width:8rem;
  padding:.15rem 0;border-radius:6px}
 .rSel:hover .rName{color:var(--gold)}
 .rDot{width:.62em;height:.62em;border-radius:50%;flex:none;border:1.5px solid var(--faint)}
 .pRow.sel .rDot{background:var(--gold);border-color:var(--gold);
  box-shadow:0 0 0 2px rgba(226,166,61,.25)}
 .rName{font-size:.82rem;font-weight:600}
 .rNow{font-size:.62rem;letter-spacing:.1em;color:var(--gold)}
 .pRow.warn{background:rgba(232,85,90,.08);border-left:2px solid #e8555a;
  padding-left:.5rem;margin:.2rem 0;border-bottom:none}
 .pRow.warn b{color:#e8555a}
 .pAsk{font-size:.72rem;color:var(--dim);padding:.1rem 0 .35rem .5rem}
 .pChips{display:flex;flex-wrap:wrap;gap:.3rem;margin:0 0 .5rem}
 .chip{display:flex;align-items:center;gap:.25rem;background:var(--chip);
  border:1px solid var(--line);border-radius:999px;padding:.15rem .5rem;font-size:.7rem}
 .tokOut{background:var(--bg);border:1px solid var(--line);border-radius:6px;
  padding:.6rem;font-size:.72rem;user-select:all;white-space:pre-wrap;word-break:break-all}
 #dlgWho{display:none;margin-bottom:1.1rem}
 #dlgWho.on{display:block}
 #dlgWho .lbl{color:var(--faint);font-size:.68rem;letter-spacing:.12em;
  text-transform:uppercase;margin-bottom:.5rem}
 #dlgReaders{display:flex;flex-wrap:wrap;gap:.4rem;max-height:9rem;overflow-y:auto}
 .reader{display:inline-flex;align-items:center;gap:.45rem;background:var(--rail);
  border:1px solid var(--line);border-radius:999px;padding:.22rem .75rem .22rem .28rem;
  font-size:.78rem;font-weight:600}
 .reader .dot{width:1.25rem;height:1.25rem;border-radius:50%;display:flex;
  align-items:center;justify-content:center;font-size:.52rem;font-weight:800;flex:none}
 #dlgOk{width:100%;background:var(--gold);color:#14161a;border:0;border-radius:8px;
  padding:.55rem;font:inherit;font-weight:800;cursor:pointer}
 #dlgOk:hover{filter:brightness(1.08)}

 /* ---- login modal ---- */
 #login{position:fixed;inset:0;background:rgba(6,8,11,.82);backdrop-filter:blur(4px);
  display:none;align-items:center;justify-content:center;z-index:20}
 #login.on{display:flex}
 #loginCard{background:var(--card);border:1px solid var(--line);border-radius:14px;
  padding:2rem 2.2rem;width:22rem;box-shadow:0 12px 48px rgba(0,0,0,.5)}
 #loginCard h1{font-size:1.15rem;letter-spacing:.24em;color:var(--gold);margin-bottom:.2rem}
 #loginCard p{color:var(--faint);font-size:.8rem;margin-bottom:1.1rem}
 #loginCard label{display:block;color:var(--dim);font-size:.75rem;margin:.7rem 0 .25rem;
  letter-spacing:.08em}
 #loginCard input{width:100%;background:var(--rail);border:1px solid var(--line);
  color:var(--fg);padding:.55rem .8rem;border-radius:8px;font:inherit}
 #loginCard input:focus{outline:none;border-color:var(--gold)}
 #loginCard button{width:100%;margin-top:1.3rem;background:var(--gold);color:#14161a;
  border:0;border-radius:8px;padding:.6rem;font:inherit;font-weight:800;cursor:pointer}
 #loginErr{color:#e8555a;font-size:.78rem;min-height:1.1em;margin-top:.5rem}

 /* ---- recipient picker ---- */
 #toWrap{position:relative}
 #toBtn{max-width:20rem;text-align:left;background:var(--hover);border:1px solid var(--line);
  color:var(--gold);font-weight:600;padding:.22rem 1rem;border-radius:2em;font:inherit;
  font-size:.8rem;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
 #toBtn::before{content:"to: ";color:var(--faint);font-weight:400}
 #toBtn:focus{outline:none;border-color:var(--gold)}
 #shoutWrap{display:flex;align-items:center;gap:.35rem;color:var(--faint);
  font-size:.78rem;cursor:pointer;user-select:none;white-space:nowrap}
 #shoutWrap input{accent-color:var(--gold)}
 #shoutWrap:has(input:checked){color:var(--gold);font-weight:700}
 /* Room scope of a broadcast. Off = the room you are reading (the safe default, and
    what every other control here means). Armed = every room you hold, one message
    each -- the Send button restyles itself so the blast cannot be sent by muscle. */
 #blast{background:var(--hover);border:1px solid var(--line);color:var(--faint);
  border-radius:2em;padding:.22rem .7rem;font:inherit;font-size:.72rem;font-weight:700;
  cursor:pointer;white-space:nowrap;letter-spacing:.02em}
 #blast:hover{color:var(--dim)}
 #blast.armed{color:#14161a;background:var(--gold);border-color:var(--gold);
  animation:blastPulse 1.1s ease-in-out infinite}
 @keyframes blastPulse{50%{filter:brightness(.72)}}
 #send.blasting{background:#e8555a;color:#fff}
 #toPanel{display:none;position:absolute;bottom:calc(100% + .4rem);left:0;width:16rem;
  max-height:19rem;overflow-y:auto;background:var(--card);border:1px solid var(--line);
  border-radius:10px;padding:.4rem;z-index:10;box-shadow:0 8px 30px rgba(0,0,0,.45)}
 #toPanel.on{display:block}
 .pick{display:flex;align-items:center;gap:.55rem;padding:.3rem .5rem;border-radius:6px;
  cursor:pointer;font-size:.85rem;color:var(--dim)}
 .pick:hover{background:var(--hover)}
 .pick.on{color:var(--fg)}
 .pick .box{width:.85em;height:.85em;border:1px solid var(--faint);border-radius:3px;flex:none}
 .pick.on .box{background:var(--gold);border-color:var(--gold)}
 .pick.bcastRow{border-bottom:1px solid var(--line);margin-bottom:.25rem;padding-bottom:.45rem}

 .row.active{background:var(--hover);outline:1px solid var(--gold)}
 @media(max-width:760px){
  body{grid-template-columns:1fr}
  #rail{display:none}
  .crow{flex-wrap:wrap}
 }
</style></head><body>
<nav id="rail">
 <div id="brand"><h1>REVEILLE</h1>
  <small><span id="status"></span><span id="ver">bus</span></small></div>
 <h2>AGENTS</h2>
 <div id="fmode" title="filter selected agents by messages they sent, or messages sent to them">
  <button type="button" id="fmFrom" class="on">FROM</button>
  <button type="button" id="fmTo">TO</button>
 </div>
 <div id="agents"></div>
 <div id="meWrap">
  <div id="meMenu">
   <div class="mi" id="miSettings">Settings</div>
   <div class="mi" id="miLogout">Logout</div>
  </div>
  <div id="meCard" title="settings, logout">
   <div class="avatar" id="meAvatar"></div>
   <div><div class="mename" id="meName"></div><div class="meroom" id="meRoom"></div></div>
   <span id="meDot" title="bus connection"></span>
   <span class="gear">&#9881;</span>
  </div>
 </div>
</nav>
<div id="main">
 <div id="top">
  <input id="filter" placeholder="Filter messages&hellip;">
  <span id="filterState" title="clear agent filter"></span>
  <button id="histBtn" title="search the entire bus log">history</button>
 </div>
 <div id="histBar">
  <div class="hf"><label>Quick range</label>
   <div class="qr">
    <button type="button" data-since="1h">1h</button>
    <button type="button" data-since="6h">6h</button>
    <button type="button" data-since="24h">24h</button>
    <button type="button" data-since="7d">7d</button>
    <button type="button" data-since="">all</button>
   </div></div>
  <div class="hf"><label>Keywords</label>
   <input id="hKw" placeholder="any match, ranked" style="width:17rem"></div>
  <div class="hf"><label>From</label><input id="hSince" type="datetime-local"></div>
  <div class="hf"><label>To</label><input id="hUntil" type="datetime-local"></div>
  <div class="hf"><label>Agent</label>
   <select id="hAgent"><option value="">any</option></select></div>
  <button id="hGo">Search</button>
 </div>
 <div id="spin"><div class="ring"></div></div>
 <div id="feed"><div class="inner" id="inner">
  <div id="histInfo"><span id="histLabel"></span><span><b id="backLive">back to live</b></span></div>
  <div id="empty">no traffic yet</div></div></div>
 <button id="jump">&darr; latest</button>
 <div id="composerWrap">
  <form id="composer">
   <div class="ctop">
    <div id="toWrap">
     <button type="button" id="toBtn">ALL</button>
     <div id="toPanel"></div>
    </div>
    <span id="replyChip" title="clear reply"></span>
    <label id="shoutWrap" title="SHOUT: wake every agent in the room immediately (human page-all)">
     <input type="checkbox" id="shout">SHOUT</label>
    <button type="button" id="blast" hidden></button>
    <input id="subject" placeholder="Subject (optional)">
    <input id="fileInput" type="file" multiple hidden>
   </div>
   <textarea id="body" placeholder="Message&hellip;  paste images here"></textarea>
   <div class="cbottom">
    <button type="button" id="attachBtn">+ attach</button>
    <span id="attchips"></span>
    <span class="khint">Ctrl+Enter to send</span>
    <button id="send" type="submit">Send</button>
   </div>
  </form>
 </div>
</div>
<div id="toasts"></div>
<div id="dlg">
 <div id="dlgCard">
  <div id="dlgTitle"></div>
  <div id="dlgMsg"></div>
  <div id="dlgBody"></div>
  <div id="dlgWho"><div class="lbl">Already seen by</div><div id="dlgReaders"></div></div>
  <button id="dlgOk">OK</button>
 </div>
</div>
<div id="pan">
 <div id="panCard">
  <div id="panTabs">
   <button class="tab" data-tab="rooms">Rooms</button>
   <button class="tab" data-tab="memory">Memory</button>
   <button class="tab" data-tab="tokens">Tokens</button>
   <button class="tab" data-tab="users">Users</button>
   <button class="tab" data-tab="account">Account</button>
   <button id="panX" title="close">&#10005;</button>
  </div>
  <div id="panBody"></div>
  <div id="panFoot"><span id="panWho"></span><span class="lnk" id="panOut">sign out</span></div>
 </div>
</div>
<div id="login">
 <div id="loginCard">
  <h1>REVEILLE</h1>
  <p id="liBlurb"></p>
  <label for="liName">USERNAME</label>
  <input id="liName" autocomplete="username" autocapitalize="none" spellcheck="false">
  <label for="liPass">PASSWORD</label>
  <input id="liPass" type="password" autocomplete="current-password">
  <div id="loginErr"></div>
  <button id="liGo">Sign in</button>
 </div>
</div>
<script>
'use strict';
const $=id=>document.getElementById(id);
function toast(msg,info){
 const t=document.createElement('div');
 t.className='toast'+(info?' info':'');
 t.textContent=msg;
 t.onclick=()=>t.remove();
 $('toasts').appendChild(t);
 setTimeout(()=>t.remove(),5000);
}
function showDialog(title,msg,readers){
 $('dlgTitle').textContent=title;
 $('dlgBody').innerHTML='';
 $('dlgMsg').textContent=msg;
 const list=$('dlgReaders');list.innerHTML='';
 $('dlgWho').classList.toggle('on',!!(readers&&readers.length));
 for(const n of readers||[]){
  const c=document.createElement('span');c.className='reader';
  c.innerHTML='<span class="dot" style="color:'+color(n)+';background:'+tint(n)+'">'
    +initials(n)+'</span><span style="color:'+color(n)+'">'+esc(n)+'</span>';
  list.appendChild(c);
 }
 $('dlg').classList.add('on');$('dlgOk').focus();
}
$('dlgOk').onclick=()=>$('dlg').classList.remove('on');
$('dlg').onclick=e=>{if(e.target.id==='dlg')$('dlg').classList.remove('on');};
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('dlg').classList.remove('on');});
// The session cookie authenticates every call -- browsers attach it to fetch(), to
// <img src>, and to the WS handshake alike. The old ?token= existed ONLY because an
// <img> cannot carry a header; cookies delete that constraint, so the credential stops
// riding in URLs, referers and logs. qs() now carries the selected ROOM, never a secret.
let me=null;               // {name,is_admin,rooms,owned,public} from GET /me
let myName='';
let room=localStorage.revRoom||'';
const qsFor=r=>'?room='+encodeURIComponent(r||'');
const qs=()=>qsFor(room);
let blast=false;           // broadcast scope: false = this room only, true = every room I hold
let lastId=0,follow=true,prevMsg=null,mode='live',agentList=[];
const selAgents=new Set();     // sidebar filter: matches by FROM (default) or TO
let filterMode='from';
const recip=new Set();          // empty = ALL (broadcast); else one unicast per member
let replyTo=null;               // message id the composer is replying to
const attachments=[];           // [{name,url}] pending on the composer

async function uploadFile(f){
 const r=await fetch('/upload'+qs()+'&name='+encodeURIComponent(f.name),
  {method:'POST',body:f});
 if(!r.ok){toast('upload failed: '+(await r.text()));return;}
 attachments.push(await r.json());
 renderAttchips();
}

function renderAttchips(){
 const w=$('attchips');w.innerHTML='';
 attachments.forEach((a,i)=>{
  const t=document.createElement('div');t.className='attTile';t.title=a.name;
  if(/\.(png|jpe?g|gif|webp|svg)$/i.test(a.url)){
   t.innerHTML='<img src="'+a.url+qs()+'" alt="">';
  }else{
   const ext=(a.name.includes('.')?a.name.split('.').pop():'').toUpperCase().slice(0,4)||'FILE';
   t.innerHTML='<span class="ext">'+esc(ext)+'</span><span class="fn">'+esc(a.name)+'</span>';
  }
  const rm=document.createElement('span');rm.className='rm';rm.textContent='\u2715';
  rm.title='remove '+a.name;
  rm.onclick=()=>{attachments.splice(i,1);renderAttchips();};
  t.appendChild(rm);w.appendChild(t);
 });
}
const msgs=new Map();

let setupMode=false;
// The card is only ever raised through here, so this is the ONE place the blurb is
// written -- the markup ships it empty rather than carrying a copy that can drift.
function showLogin(msg){
 $('liBlurb').textContent=setupMode
  ? 'first run -- create the admin account'
  : 'fleet comms';
 $('liGo').textContent=setupMode?'Create admin':'Sign in';
 $('loginErr').textContent=msg||'';
 $('login').classList.add('on');$('liName').focus();
}
$('liGo').onclick=async()=>{
 const body={name:$('liName').value.trim(),password:$('liPass').value};
 let r=await fetch(setupMode?'/setup':'/login',
  {method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});
 // 410 = somebody created the first admin after this tab decided it was in setup mode.
 // The card latched that at load and would otherwise 410 forever with no way out: drop
 // to a normal sign-in instead of stranding whoever is looking at it.
 if(r.status===410){
  setupMode=false;showLogin();
  r=await fetch('/login',{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify(body)});
 }
 if(!r.ok){const e=await r.json().catch(()=>({}));
  $('loginErr').textContent=e.detail||e.error||'sign in failed';return;}
 location.reload();   // the cookie is set; boot again as a real principal
};
for(const id of ['liName','liPass'])
 $(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('liGo').click();});

// The room name is finally printable: it stopped being the credential, which is the
// only reason it was ever masked to 'room ....N0w'.
function paintMe(){
 $('meName').textContent=myName;
 $('meName').style.color=color(myName);
 $('meAvatar').textContent=initials(myName);
 $('meAvatar').style.color=color(myName);
 $('meAvatar').style.background=tint(myName);
 const rooms=(me&&me.rooms)||[];
 const r=rooms.find(x=>x.id===room);
 // The chevron says the card OPENS something -- always, since the menu is always there.
 // It used to appear only with 2+ rooms, back when the card jumped straight to the room
 // list; pointing it at a menu that is sometimes absent would be the lie.
 $('meRoom').innerHTML=esc(r?r.name:'no room')+'<span class="chev">&#9660;</span>';
 $('meCard').title=rooms.length>1
  ? 'settings, logout -- rooms ('+rooms.length+' available) are the first tab of Settings'
  : 'settings, logout';
}
$('meCard').onclick=e=>{e.stopPropagation();$('meMenu').classList.toggle('on');};
$('miSettings').onclick=()=>{$('meMenu').classList.remove('on');openRooms();};
// Logout is the panel's sign-out, reached without opening the panel: same one call, same
// reload. The reload is what clears every in-page trace of the session.
$('miLogout').onclick=async()=>{await fetch('/logout',{method:'POST'});location.reload();};
document.addEventListener('click',()=>$('meMenu').classList.remove('on'));
document.addEventListener('keydown',e=>{if(e.key==='Escape')$('meMenu').classList.remove('on');});

function hue(name){let h=0;for(const c of name)h=(h*31+c.charCodeAt(0))>>>0;return h%360;}
function color(n){return 'hsl('+hue(n)+' 62% 64%)';}
function tint(n){return 'hsl('+hue(n)+' 45% 26%)';}
function initials(n){const p=n.split(/[-_.]/).filter(Boolean);
 return ((p[0]?.[0]||'')+(p[1]?.[0]||p[0]?.[1]||'')).toUpperCase();}
function hhmm(ns){return new Date(ns/1e6).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});}
function dayOf(ns){return new Date(ns/1e6).toDateString();}
function esc(s){const d=document.createElement('span');d.textContent=s;return d.innerHTML;}

function matches(m){
 if(!m)return true;
 if(selAgents.size){
  // additive across selected agents; FROM = messages they posted (default),
  // TO = direct mail addressed to them (broadcasts excluded -- that is FROM's job)
  const hit=filterMode==='from'?selAgents.has(m.from):selAgents.has(m.to);
  if(!hit)return false;
 }
 const f=$('filter').value.trim().toLowerCase();
 if(!f)return true;
 const hay=(m.from+' '+m.to+' '+(m.subject||'')+' '+m.body).toLowerCase();
 return f.split(/\s+/).some(w=>hay.includes(w));
}

function attHtml(list){
 if(!list||!list.length)return '';
 return '<div class="atts">'+list.map(a=>{
  const safe=a.url+qs();
  if(/\.(png|jpe?g|gif|webp|svg)$/i.test(a.url))
   return '<a href="'+safe+'" target="_blank" rel="noopener" title="open full size">'
     +'<img class="att" src="'+safe+'" alt="'+esc(a.name||'image')+'"></a>';
  const link='<a class="attlink" href="'+safe+'" download>'+esc(a.name||a.url)+'</a>';
  if(/\.(md|markdown)$/i.test(a.url)&&(a.bytes||0)<300000)
   return link+'<div class="mdview" data-src="'+safe+'">rendering&hellip;</div>';
  return link;
 }).join(' ')+'</div>';
}

// minimal GH-flavored subset: headers, lists, code fences, tables, quotes,
// hr, bold/italic/inline-code/links. Everything HTML-escaped first.
function mdToHtml(src){
 const inline=s=>esc(s)
  .replace(/`([^`]+)`/g,'<code>$1</code>')
  .replace(/\*\*([^*]+)\*\*/g,'<b>$1</b>')
  .replace(/(^|\W)\*([^*\s][^*]*)\*/g,'$1<i>$2</i>')
  .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g,'<a href="$2" target="_blank" rel="noopener">$1</a>');
 let out='',inCode=false,list=null,inTable=false;
 const closeBlocks=()=>{
  if(list){out+='</'+list+'>';list=null;}
  if(inTable){out+='</table>';inTable=false;}
 };
 for(const ln of src.split('\n')){
  if(/^\s*```/.test(ln)){closeBlocks();out+=inCode?'</code></pre>':'<pre><code>';inCode=!inCode;continue;}
  if(inCode){out+=esc(ln)+'\n';continue;}
  if(/^\s*\|.*\|\s*$/.test(ln)){
   if(list){out+='</'+list+'>';list=null;}
   if(/^\s*\|[\s:|-]+\|\s*$/.test(ln))continue;               // header separator row
   const cells=ln.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(c=>inline(c.trim()));
   const tag=inTable?'td':'th';
   if(!inTable){out+='<table>';inTable=true;}
   out+='<tr><'+tag+'>'+cells.join('</'+tag+'><'+tag+'>')+'</'+tag+'></tr>';
   continue;
  }
  if(inTable){out+='</table>';inTable=false;}
  const h=ln.match(/^(#{1,6})\s+(.*)/);
  if(h){closeBlocks();out+='<h'+h[1].length+'>'+inline(h[2])+'</h'+h[1].length+'>';continue;}
  if(/^\s*[-*+]\s+/.test(ln)){
   if(list!=='ul'){closeBlocks();out+='<ul>';list='ul';}
   out+='<li>'+inline(ln.replace(/^\s*[-*+]\s+/,''))+'</li>';continue;}
  if(/^\s*\d+\.\s+/.test(ln)){
   if(list!=='ol'){closeBlocks();out+='<ol>';list='ol';}
   out+='<li>'+inline(ln.replace(/^\s*\d+\.\s+/,''))+'</li>';continue;}
  if(/^\s*>\s?/.test(ln)){closeBlocks();out+='<blockquote>'+inline(ln.replace(/^\s*>\s?/,''))+'</blockquote>';continue;}
  if(/^\s*([-_*])\s*(\1\s*){2,}$/.test(ln)){closeBlocks();out+='<hr>';continue;}
  if(!ln.trim()){closeBlocks();continue;}
  closeBlocks();out+='<p>'+inline(ln)+'</p>';
 }
 if(inCode)out+='</code></pre>';
 closeBlocks();
 return out;
}

async function hydrateMd(el){
 try{
  const r=await fetch(el.dataset.src);
  if(!r.ok){el.textContent='could not load '+el.dataset.src;return;}
  el.innerHTML=mdToHtml(await r.text());
 }catch(e){el.textContent='could not load attachment';}
 el.removeAttribute('data-src');
}

function render(m){
 const frag=document.createDocumentFragment();
 if(!prevMsg||dayOf(prevMsg.ts_ns)!==dayOf(m.ts_ns)){
  const d=document.createElement('div');d.className='day';d.textContent=dayOf(m.ts_ns);
  frag.appendChild(d);
 }
 const cont=prevMsg&&prevMsg.from===m.from&&prevMsg.to===m.to
   &&m.ts_ns-prevMsg.ts_ns<5*60*1e9&&dayOf(prevMsg.ts_ns)===dayOf(m.ts_ns);
 const row=document.createElement('div');
 row.className='row'+(m.to==='*'?' bcast':'')+(cont?' cont':'');
 row.dataset.id=m.id;
 row.title='#'+m.id+'  '+new Date(m.ts_ns/1e6).toLocaleString();
 const c=color(m.from);
 row.innerHTML=
  '<div class="gutter"><div class="avatar" style="color:'+c+';background:'+tint(m.from)+'">'
   +initials(m.from)+'</div></div>'
  +'<div class="msgcol">'
  +(cont?'':'<div class="head"><span class="who" style="color:'+c+'">'+esc(m.from)+'</span>'
    +'<span class="arrow">&rarr;</span>'
    +(m.to==='*'?'<span class="all">ALL</span>'
      :'<span class="toname" style="color:'+color(m.to)+'">'+esc(m.to)+'</span>')
    +'<time>'+hhmm(m.ts_ns)+'</time>'
    +'<span class="mid" data-thread="'+m.thread_id+'" title="view thread">#'+m.id
    +(m.thread_id!==m.id?' &middot; thread '+m.thread_id:'')+'</span>'
    +(m.from===myName?'<span class="del" data-del="'+m.id
      +'" title="retract (only while nobody has read it)">&#10007;</span>':'')
    +'</div>')
  +(m.subject?'<div class="subj">'+esc(m.subject)+'</div>':'')
  +'<div class="body">'+esc(m.body)+'</div>'+attHtml(m.attachments)+'</div>';
 for(const el of row.querySelectorAll('.mdview[data-src]'))hydrateMd(el);
 row.style.display=matches(m)?'':'none';
 row.addEventListener('click',e=>{
  if(e.target.closest('.mid'))return;        // thread drill-down keeps its own click
  selectRow(row,m);
 });
 frag.appendChild(row);
 prevMsg=m;
 return frag;
}

function add(m){
 if(m.id>lastId)lastId=m.id;
 if(mode!=='live'||msgs.has(m.id))return;   // history view: live frames wait for "back to live"
 msgs.set(m.id,m);
 $('empty')?.remove();
 $('inner').appendChild(render(m));
 if(follow)$('feed').scrollTop=$('feed').scrollHeight;
}

function clearFeed(){
 for(const el of [...$('inner').children])
  if(el.id!=='histInfo')el.remove();
 msgs.clear();prevMsg=null;
}

function toLive(){
 mode='live';follow=true;
 for(const o of document.querySelectorAll('.qr button'))o.classList.remove('on');
 $('histBtn').classList.remove('on');$('histBar').classList.remove('on');
 $('histInfo').classList.remove('on');
 clearFeed();loadBacklog(true);
}

async function runSearch(params){
 mode='history';follow=false;
 $('histBtn').classList.add('on');
 busy(true);
 try{
  const q=new URLSearchParams(params);
  const r=await fetch('/search'+qs()+'&'+q.toString());
  if(!r.ok){toast('search failed: '+(await r.text()));return;}
  const data=await r.json();
  clearFeed();
  $('histInfo').classList.add('on');
  $('histLabel').textContent=(params.thread_id?'thread '+params.thread_id:'history')
    +' -- '+data.count+' message'+(data.count===1?'':'s');
  for(const m of data.messages){msgs.set(m.id,m);$('inner').appendChild(render(m));}
  $('feed').scrollTop=0;
 }finally{busy(false);}
}

// datetime-local is the viewer's LOCAL time; convert to UTC ISO so the broker's
// naive-ISO-is-UTC rule can never shift the window (the 0.1.1 false-zero lesson).
function utcIso(v){return v?new Date(v).toISOString():'';}

function refilter(){
 for(const el of $('inner').children){
  if(!el.dataset.id)continue;                      // day dividers stay
  el.style.display=matches(msgs.get(+el.dataset.id))?'':'none';
 }
 $('filterState').style.display=selAgents.size?'inline':'none';
 $('filterState').textContent=selAgents.size?(filterMode.toUpperCase()+': '+[...selAgents].join(' + ')+'  ✕'):'';
 if(follow)$('feed').scrollTop=$('feed').scrollHeight;
}

async function loadBacklog(reset){
 if(mode!=='live')return;
 if(reset)busy(true);
 try{
  const r=await fetch('/messages'+qs()+'&limit=300'+(reset?'':'&since_id='+lastId));
  if(r.status===401){showLogin('session expired -- sign in again');return;}
  if(!r.ok)return;
  if(reset)clearFeed();
  for(const m of (await r.json()).messages)add(m);
 }catch(e){
  setTimeout(()=>loadBacklog(reset),1500);   // broker restarting; keep trying until live
 }finally{busy(false);}
}

function busy(on){$('spin').classList.toggle('on',!!on);}

function updateReplyChip(){
 $('replyChip').className=replyTo?'on':'';
 $('replyChip').textContent=replyTo?('reply \u2192 #'+replyTo+'  \u2715'):'';
 updateBlast();
}

// The blast chip only offers a choice that exists: 2+ rooms, a broadcast (a unicast has
// one home), and a NEW thread -- a reply lands in its parent's room by definition, so
// blasting one would be a contradiction, not a feature.
function updateBlast(){
 const n=(me&&me.rooms||[]).length;
 const can=n>1&&recip.size===0&&!replyTo;
 if(!can)blast=false;
 const b=$('blast');
 b.hidden=!can;
 b.className=blast?'armed':'';
 b.textContent=blast?('\u26a1 ALL '+n+' ROOMS'):'this room';
 b.title=blast
  ? 'BLAST: one message into EACH of your '+n+' rooms. Rooms stay separate -- this is '
    +n+' posts, not one shared thread.'
  : 'Broadcast scope: this room only. Click to blast every room you hold.';
 $('send').className=blast?'blasting':'';
 $('send').textContent=blast?('BLAST '+n+' ROOMS'):'Send';
}

function selectRow(row,m){
 const was=row.classList.contains('active');
 for(const el of document.querySelectorAll('.row.active'))el.classList.remove('active');
 recip.clear();replyTo=null;
 if(!was){                                   // click again to deselect back to ALL
  row.classList.add('active');
  replyTo=m.id;
  if(m.from===myName){                       // own message: target its recipient, never yourself
   if(m.to!=='*'&&m.to!==myName)recip.add(m.to);
  }else recip.add(m.from);
  $('body').focus();
 }
 renderPicker();updateReplyChip();
}

function toLabel(){
 if(recip.size===0)return 'ALL';
 if(recip.size===1)return [...recip][0];
 return recip.size+' agents';
}

function renderPicker(){
 const p=$('toPanel');p.innerHTML='';
 const all=document.createElement('div');
 all.className='pick bcastRow'+(recip.size===0?' on':'');
 all.innerHTML='<span class="box"></span><span>ALL &mdash; broadcast</span>';
 all.onclick=e=>{e.stopPropagation();recip.clear();renderPicker();};
 p.appendChild(all);
 for(const a of agentList){
  if(a.name===myName)continue;
  const el=document.createElement('div');
  el.className='pick'+(recip.has(a.name)?' on':'');
  el.innerHTML='<span class="box"></span><span style="color:'+color(a.name)+'">'
    +esc(a.name)+'</span>';
  el.onclick=e=>{e.stopPropagation();recip.has(a.name)?recip.delete(a.name):recip.add(a.name);renderPicker();};
  p.appendChild(el);
 }
 $('toBtn').textContent=toLabel();
 $('shoutWrap').style.display=recip.size===0?'flex':'none';
 updateBlast();
}

$('blast').onclick=()=>{blast=!blast;updateBlast();};

async function loadPresence(){
 const r=await fetch('/presence'+qs()+'&me='+encodeURIComponent(myName));
 if(r.status===401){showLogin('session expired -- sign in again');return;}
 if(!r.ok)return;
 agentList=(await r.json()).agents
  .sort((a,b)=>(b.connected-a.connected)||(b.live-a.live)||a.name.localeCompare(b.name));
 $('agents').innerHTML='';
 // "everyone" is a FILTER SCOPE, not a being: it has no presence, so it gets no presence
 // dot. It wore one because the row reused .live to mean "selected" -- the same gold dot
 // that means "live but unreachable" one row below. One channel, two meanings, so the
 // scope read as an agent having a bad day. Selection is the row highlight (what it means
 // on every other row); the count is the thing worth saying here.
 const all=document.createElement('div');
 all.className='agent allrow'+(selAgents.size?'':' sel');
 all.innerHTML='<span class="swatch" style="background:var(--gold)"></span>'
   +'<span class="nm">everyone</span>'
   +'<span class="cnt">'+agentList.length+'</span>';
 all.title=selAgents.size?'show every agent':'showing every agent';
 all.onclick=()=>{selAgents.clear();recip.clear();renderPicker();loadPresence();refilter();};
 $('agents').appendChild(all);
 const sel=$('hAgent'),keep=sel.value;
 sel.innerHTML='<option value="">any</option>';
 for(const a of agentList){
  sel.innerHTML+='<option value="'+esc(a.name)+'">'+esc(a.name)+'</option>';
 }
 sel.value=keep;
 for(const a of agentList){
  const el=document.createElement('div');
  el.className='agent'+(a.live?' live':'')+(a.connected?' conn':'')
    +(selAgents.has(a.name)?' sel':'');
  el.innerHTML='<span class="swatch" style="background:'+color(a.name)+'"></span>'
    +'<span class="nm">'+esc(a.name)+'</span><span class="st"></span>';
  // Same three states, said in the vocabulary of what the row actually is: a person has
  // a tab where an agent has a waiter, and telling a human their "waiter is down" is noise
  // about a thing they were never going to run.
  const web=(a.tag||'').startsWith('web:');
  el.title=a.connected?(web?'here -- watching this room live':'wake armed -- rings on unicast')
   :a.live?(web?'signed in, not watching this room':'live, waiter down -- mail queues'):'stale';
  el.onclick=()=>{selAgents.has(a.name)?selAgents.delete(a.name):selAgents.add(a.name);
   recip.clear();for(const n of selAgents)if(n!==myName)recip.add(n);   // filter selection IS the target, minus yourself
   renderPicker();loadPresence();refilter();};
  $('agents').appendChild(el);
 }
 renderPicker();
}

let ws=null;
// This page is served as one string by the daemon, so an upgraded broker leaves every open
// tab running the OLD interface until someone reloads. The daemon does not push a "reload"
// frame: a restart already drops the feed, so the reconnect IS the signal, and asking on
// the way back up also catches a tab that was asleep through the whole thing.
let bootVer=null;
async function checkVersion(){
 let v;
 try{v=await (await fetch('/version')).text();}catch(e){return;}   // offline: the next open retries
 if(!bootVer){bootVer=v;return;}
 if(v===bootVer)return;
 // Never reload out from under someone mid-sentence -- a draft is unrecoverable and this
 // is cosmetic until they act. Composer busy: tell them, let them pick the moment.
 if($('body').value.trim()||attachments.length){
  toast('v'+v+' is live -- reload when you have sent this');return;}
 location.reload();
}

function connect(){
 const proto=location.protocol==='https:'?'wss':'ws';
 ws=new WebSocket(proto+'://'+location.host+'/feed'+qs());
 ws.onopen=()=>{$('status').classList.add('on');$('meDot').classList.add('on');
  $('meDot').title='connected to the room feed';loadBacklog(false);checkVersion();};
 ws.onmessage=e=>{const m=JSON.parse(e.data);
  if(m.error){if(m.error==='bad_token')showLogin('session expired -- sign in again');return;}
  if(m.deleted){
   for(const el of [...$('inner').children])
    if(+el.dataset.id===m.deleted)el.remove();
   msgs.delete(m.deleted);return;
  }
  add(m);};
 ws.onclose=()=>{$('status').classList.remove('on');$('meDot').classList.remove('on');
  $('meDot').title='reconnecting';
  if(!$('login').classList.contains('on'))setTimeout(connect,2000);};
}

$('feed').addEventListener('scroll',()=>{
 const el=$('feed');
 follow=el.scrollTop+el.clientHeight>=el.scrollHeight-40;
 $('jump').style.display=follow?'none':'block';
});
$('jump').onclick=()=>{follow=true;$('feed').scrollTop=$('feed').scrollHeight;$('jump').style.display='none';};
$('filter').addEventListener('input',refilter);
$('filterState').onclick=()=>{selAgents.clear();recip.clear();renderPicker();
 loadPresence();refilter();};
for(const mode of ['From','To'])
 $('fm'+mode).onclick=()=>{filterMode=mode.toLowerCase();
  $('fmFrom').className=filterMode==='from'?'on':'';
  $('fmTo').className=filterMode==='to'?'on':'';
  refilter();};
$('histBtn').onclick=()=>{
 if(mode==='history'){toLive();return;}
 $('histBar').classList.toggle('on');
};
$('hGo').onclick=e=>{e.preventDefault();
 const p={limit:500};
 if($('hKw').value.trim())p.keywords=$('hKw').value.trim();
 if($('hSince').value)p.since=utcIso($('hSince').value);
 if($('hUntil').value)p.until=utcIso($('hUntil').value);
 if($('hAgent').value.trim())p.agent=$('hAgent').value.trim();
 runSearch(p);
};
$('backLive').onclick=toLive;
for(const b of document.querySelectorAll('.qr button'))
 b.onclick=()=>{
  for(const o of document.querySelectorAll('.qr button'))o.classList.remove('on');
  b.classList.add('on');
  const p={limit:500};
  if(b.dataset.since)p.since=b.dataset.since;         // relative spec, server-parsed
  if($('hKw').value.trim())p.keywords=$('hKw').value.trim();
  if($('hAgent').value.trim())p.agent=$('hAgent').value.trim();
  $('hSince').value='';$('hUntil').value='';
  runSearch(p);
 };
// manual date edits or leaving history mode deselect the chip
for(const id of ['hSince','hUntil'])
 $(id).addEventListener('input',()=>{
  for(const o of document.querySelectorAll('.qr button'))o.classList.remove('on');});
$('inner').addEventListener('click',async e=>{
 const d=e.target.closest('.del');
 if(d){
  const r=await fetch('/message/'+d.dataset.del+qs()+'&from='+encodeURIComponent(myName),
    {method:'DELETE'});
  if(!r.ok){
   const err=JSON.parse(await r.text());
   if(err.readers?.length)
    showDialog('Cannot retract #'+d.dataset.del,
     'Retraction is only possible while nobody has read the message.',err.readers);
   else if(/replied/.test(err.error||''))
    showDialog('Cannot retract #'+d.dataset.del,
     'This message already has replies threaded on it.');
   else
    showDialog('Cannot retract #'+d.dataset.del,err.error||'retract failed');
  }
  return;
 }
 const t=e.target.closest('.mid');
 if(t)runSearch({thread_id:t.dataset.thread});
});
$('toBtn').onclick=()=>$('toPanel').classList.toggle('on');
$('replyChip').onclick=()=>{replyTo=null;updateReplyChip();
 for(const el of document.querySelectorAll('.row.active'))el.classList.remove('active');};
document.addEventListener('click',e=>{
 if(!e.target.closest('#toWrap'))$('toPanel').classList.remove('on');});
$('body').addEventListener('keydown',e=>{if(e.key==='Enter'&&(e.ctrlKey||e.metaKey))$('composer').requestSubmit();});
$('attachBtn').onclick=()=>$('fileInput').click();
{
 const comp=$('composer');
 let depth=0;
 comp.addEventListener('dragenter',e=>{e.preventDefault();depth++;comp.classList.add('drag');});
 comp.addEventListener('dragover',e=>e.preventDefault());
 comp.addEventListener('dragleave',()=>{if(--depth<=0){depth=0;comp.classList.remove('drag');}});
 comp.addEventListener('drop',e=>{
  e.preventDefault();depth=0;comp.classList.remove('drag');
  [...(e.dataTransfer?.files||[])].forEach(uploadFile);
 });
}
$('fileInput').addEventListener('change',()=>{
 for(const f of $('fileInput').files)uploadFile(f);
 $('fileInput').value='';
});
$('body').addEventListener('paste',e=>{
 const files=[...(e.clipboardData?.files||[])];
 if(files.length){e.preventDefault();files.forEach(uploadFile);}
});
$('composer').addEventListener('submit',async e=>{
 e.preventDefault();
 let body=$('body').value.trim();
 if(!body&&attachments.length)body=attachments.map(a=>a.name).join(', ');
 if(!body)return;
 const targets=recip.size?[...recip]:['*'];   // subgroup = one gated unicast per member
 // A blast is N separate posts, one per room, each scoped by its own ?room=. It is NOT a
 // cross-room message: rooms stay isolated, so trace()/graph() still cannot walk between
 // them and an agent only ever sees the copy that landed in its own room.
 const rooms=blast?(me.rooms||[]).map(r=>r.id):[room];
 for(const rid of rooms) for(const to of targets){
  const payload={from:myName,to,subject:$('subject').value.trim(),body};
  if(to==='*'&&$('shout').checked)payload.shout=true;
  if(attachments.length)payload.attachments=attachments.slice();
  if(replyTo)payload.reply_to=replyTo;
  const r=await fetch('/send'+qsFor(rid),{method:'POST',headers:{'content-type':'application/json'},
   body:JSON.stringify(payload)});
  if(!r.ok){toast('send to '+to+' failed: '+(await r.text()));return;}
 }
 if(blast)toast('blasted '+rooms.length+' rooms');
 blast=false;updateBlast();
 $('body').value='';$('subject').value='';$('shout').checked=false;
 attachments.length=0;renderAttchips();
 replyTo=null;updateReplyChip();
 for(const el of document.querySelectorAll('.row.active'))el.classList.remove('active');
});

// ---- rooms / tokens / users: one panel, reusing the dialog ------------------
// Inline confirm state. The panel re-renders from this, so a destructive action is
// confirmed IN the panel -- styled, non-blocking, and able to mask a password field.
let confirming=null;   // {kind, id, name}
function askRow(title,msg,body){
 return '<div class="pRow warn"><b>'+title+'</b></div>'+
  (msg?'<div class="pAsk">'+msg+'</div>':'')+
  '<div class="pRow">'+body+
  '<button class="danger" id="cfGo">confirm</button><button id="cfNo">cancel</button></div>';
}
function wireAsk(onGo,reRender){
 const no=$('cfNo'); if(!no)return;
 no.onclick=()=>{confirming=null;reRender();};
 $('cfGo').onclick=onGo;
 const first=document.querySelector('#panBody input');
 if(first){first.focus();
  first.addEventListener('keydown',e=>{if(e.key==='Enter')$('cfGo').click();});}
}
let curTab='rooms';
function panel(tab,html){
 curTab=tab;
 for(const b of document.querySelectorAll('.tab')){
  b.classList.toggle('on',b.dataset.tab===tab);
  // /users is admin-only server-side; showing the tab to a plain user would just
  // hand them a 403. Don't offer what cannot work.
  if(b.dataset.tab==='users')b.style.display=(me&&me.is_admin)?'':'none';
 }
 $('panBody').innerHTML=html;
 $('panWho').textContent=myName+(me&&me.is_admin?' · admin':'');
 $('pan').classList.add('on');
}
function closePanel(){$('pan').classList.remove('on');}
$('panX').onclick=closePanel;
$('pan').onclick=e=>{if(e.target.id==='pan')closePanel();};
$('panOut').onclick=async()=>{await fetch('/logout',{method:'POST'});location.reload();};
const TABS={rooms:()=>openRooms(),memory:()=>openMemories(),tokens:()=>openTokens(),
 users:()=>openUsers(),account:()=>openAccount()};
for(const b of document.querySelectorAll('.tab'))b.onclick=()=>TABS[b.dataset.tab]();
document.addEventListener('keydown',e=>{if(e.key==='Escape')closePanel();});
async function api(path,opts){
 const r=await fetch(path,Object.assign({headers:{'content-type':'application/json'}},opts||{}));
 if(!r.ok){const e=await r.json().catch(()=>({}));throw new Error(e.detail||e.error||r.status);}
 return r.json();
}
// The presence poll is armed once, here or at boot -- whichever gets a room first. A
// room-less first-run boots without it, so picking the first room must start it.
let polling=false;
function start(){if(polling)return;polling=true;setInterval(loadPresence,15000);}

function pickRoom(id){room=id;localStorage.revRoom=id;paintMe();updateBlast();
 closePanel();clearFeed();lastId=0;loadBacklog(true);loadPresence();
 if(ws)ws.close();connect();start();}

async function openRooms(){
 const d=await api('/me');me=d;paintMe();   // room set may have changed under us
 // Draft badges where the operator already looks (14.4): an unwatched queue is
 // draft rot arriving on schedule. Count per owned room, from the same queue
 // the Memory tab decides.
 const qc={};
 try{for(const m of (await api('/memories/queue')).queue)
  qc[m.scope]=(qc[m.scope]||0)+1;}catch(e){}
 const mine=(d.owned||[]).map(r=>{
  if(confirming&&confirming.kind==='purge'&&confirming.id===r.id)
   return askRow('Purge '+esc(r.name)+'?',
    'Deletes the room and every message in it. A snapshot is saved first, and that is the '+
    'only undo. Type the room name to confirm.',
    '<input id="cfIn" placeholder="'+esc(r.name)+'" autocomplete="off">');
  // Rename is a plain row, not askRow: it renames a LABEL. The id is what agents send to,
  // what messages carry, and what tokens are assigned -- none of it moves, so there is
  // nothing here to warn about and nothing to undo but typing the old name back.
  if(confirming&&confirming.kind==='rename'&&confirming.id===r.id)
   return '<div class="pRow sel"><span class="rDot"></span>'+
    '<input id="cfIn" value="'+esc(r.name)+'" autocomplete="off">'+
    '<button id="cfGo">rename</button><button id="cfNo">cancel</button></div>';
  const sel=r.id===room;
  return '<div class="pRow'+(sel?' sel':'')+'">'+
   '<span class="rSel" data-pick="'+r.id+'" title="'+(sel?'you are viewing this room'
     :'switch to this room')+'"><span class="rDot"></span>'+
   '<span class="rName">'+esc(r.name)+'</span>'+
   (qc[r.id]?'<span class="qcount" title="drafts awaiting your decision — Memory tab">'+
    qc[r.id]+' draft'+(qc[r.id]>1?'s':'')+'</span>':'')+
   (sel?'<span class="rNow">VIEWING</span>':'')+'</span>'+
   '<span class="pDim">'+(r.public?'public':'private')+'</span>'+
   '<button data-ren="'+r.id+'">rename</button>'+
   '<button data-pub="'+r.id+'" data-to="'+(r.public?0:1)+'">'+(r.public?'make private':'make public')+'</button>'+
   '<button class="danger" data-purge="'+r.id+'" data-name="'+esc(r.name)+'">purge</button></div>';
 }).join('')||'<div class="pDim">no rooms yet</div>';
 // Public rooms are shown as "owner -> room": names are unique per OWNER, not globally,
 // so the pairing is what makes them unambiguous.
 // Everyone's public rooms: switchable here, and tickable on a token in the Tokens tab.
 const pub=(d.public||[]).map(r=>{
  const sel=r.id===room;
  return '<div class="pRow'+(sel?' sel':'')+'">'+
   '<span class="rSel" data-pick="'+r.id+'" title="'+(sel?'you are viewing this room'
     :'switch to this room')+'"><span class="rDot"></span>'+
   '<span class="rName">'+esc(r.owner||'?')+' \u2192 '+esc(r.name)+'</span>'+
   (sel?'<span class="rNow">VIEWING</span>':'')+'</span></div>';
 }).join('')||'<div class="pDim">none</div>';
 panel('rooms',
  '<div class="pSec">YOUR ROOMS</div>'+mine+
  '<div class="pRow"><input id="newRoom" placeholder="new room name"><button id="mkRoom">create</button></div>'+
  '<div class="pSec">PUBLIC &mdash; OWNER &rarr; ROOM</div>'+pub);
 $('mkRoom').onclick=async()=>{
  try{await api('/rooms',{method:'POST',body:JSON.stringify({name:$('newRoom').value.trim()})});
   openRooms();}catch(e){toast(e.message);}};
 $('newRoom').addEventListener('keydown',e=>{if(e.key==='Enter')$('mkRoom').click();});
 for(const b of document.querySelectorAll('[data-pick]'))b.onclick=()=>pickRoom(b.dataset.pick);
 for(const b of document.querySelectorAll('[data-ren]'))b.onclick=()=>{
  confirming={kind:'rename',id:b.dataset.ren};openRooms();};
 if(confirming&&confirming.kind==='rename')wireAsk(async()=>{
  const id=confirming.id,name=$('cfIn').value.trim();confirming=null;
  try{await api('/rooms/'+id,{method:'PATCH',body:JSON.stringify({name})});}
  catch(e){toast(e.message);}
  openRooms();},openRooms);
 for(const b of document.querySelectorAll('[data-pub]'))b.onclick=async()=>{
  try{await api('/rooms/'+b.dataset.pub,{method:'PATCH',
   body:JSON.stringify({public:b.dataset.to==='1'})});openRooms();}catch(e){toast(e.message);}};
 // Typed-name confirm: purge is irreversible and the snapshot is the only undo.
 for(const b of document.querySelectorAll('[data-purge]'))b.onclick=()=>{
  confirming={kind:'purge',id:b.dataset.purge,name:b.dataset.name};openRooms();};
 if(confirming&&confirming.kind==='purge')wireAsk(async()=>{
  if($('cfIn').value!==confirming.name){toast('name does not match');return;}
  const id=confirming.id;confirming=null;
  try{const o=await api('/rooms/'+id,{method:'DELETE'});
   toast('purged '+o.messages+' messages (snapshot saved)');
   if(room===id){room='';localStorage.revRoom='';}
   openRooms();}catch(e){toast(e.message);openRooms();}},openRooms);
}

async function openTokens(){
 // Refresh /me rather than trusting the copy cached at boot: another user can make a room
 // public at any moment, and a stale cache would silently hide it from the checkboxes.
 me=await api('/me');
 const d=await api('/tokens');
 const rooms=(me.owned||[]).concat(me.public||[]);   // assignable = mine + everyone's public
 const rows=d.tokens.map(t=>{
  const chips=rooms.map(r=>'<label class="chip"><input type="checkbox" data-tok="'+t.id+'" '+
   'data-room="'+r.id+'"'+(t.rooms[r.id]?' checked':'')+'> '+
   esc(r.owner&&r.owner!==myName?r.owner+' \u2192 '+r.name:r.name)+'</label>').join('');
  if(confirming&&confirming.kind==='revoke'&&confirming.id===t.id)
   return askRow('Revoke '+esc(t.label||t.id.slice(0,8))+'?',
    'The token is deleted. Every agent using it goes dark on its next call, and the '+
    'secret cannot be recovered.','');
  return '<div class="pRow"><b>'+esc(t.label||t.id.slice(0,8))+'</b>'+
   (t.agent_name?' <span class="pDim">= '+esc(t.agent_name)+' (bound)</span>'
                :' <span class="pDim">(unbound: any name)</span>')+
   // Tier visible AND mutable (DES-001 sec 6); a flip is an authority change,
   // audited server-side (token_audit) -- the select is wired to that PATCH.
   '<select data-tier="'+t.id+'" title="memory tier — every flip is audited">'+
   ['state','write','ratify'].map(x=>'<option value="'+x+'"'+
    (t.mem_tier===x?' selected':'')+'>memory: '+x+'</option>').join('')+'</select>'+
   '<button class="danger" data-rev="'+t.id+'">revoke</button>'+
   '</div><div class="pChips">'+chips+'</div>';}).join('')||'<div class="pDim">no tokens</div>';
 panel('tokens',
  '<div class="pDim">A token is a credential and carries no room. Tick the rooms it may '+
  'see -- the change lands on the agent\'s very next call. Binding an agent name makes '+
  'the token BE that agent: a different X-Agent gets a 401. Binding is set here, once; '+
  'rebinding means a new token.</div>'+rows+
  '<div class="pRow"><input id="newTok" placeholder="label (e.g. fleet)">'+
  '<input id="newTokName" placeholder="bind to agent (optional)">'+
  '<select id="newTokTier"><option value="state">memory: state (default)</option>'+
  '<option value="write">memory: write</option>'+
  '<option value="ratify">memory: ratify</option></select>'+
  '<button id="mkTok">generate</button></div>');
 $('mkTok').onclick=async()=>{
  try{const t=await api('/tokens',{method:'POST',body:JSON.stringify({label:$('newTok').value.trim(),
    agent_name:$('newTokName').value.trim(),mem_tier:$('newTokTier').value})});
   // Shown once, deliberately: only the hash is stored, so there is no second chance.
   panel('tokens',
    '<div class="pSec">TOKEN CREATED</div>'+
    '<div class="pDim">Copy it now. Only its hash is stored, so this is the only time it '+
    'is ever shown. Put it in the agent\'s .envrc.</div>'+
    '<pre class="tokOut">REVEILLE_TOKEN='+esc(t.secret)+'</pre>'+
    '<div class="pRow"><button id="backTok">done</button></div>');
   $('backTok').onclick=openTokens;}catch(e){toast(e.message);}};
 for(const b of document.querySelectorAll('[data-rev]'))b.onclick=()=>{
  confirming={kind:'revoke',id:b.dataset.rev};openTokens();};
 if(confirming&&confirming.kind==='revoke')wireAsk(async()=>{
  const id=confirming.id;confirming=null;
  try{await api('/tokens/'+id,{method:'DELETE'});openTokens();}
  catch(e){toast(e.message);openTokens();}},openTokens);
 for(const c of document.querySelectorAll('[data-tok]'))c.onchange=async()=>{
  try{await api('/tokens/'+c.dataset.tok,{method:'PATCH',
   body:JSON.stringify({room:c.dataset.room,attach:c.checked})});}catch(e){toast(e.message);c.checked=!c.checked;}};
 for(const s of document.querySelectorAll('[data-tier]'))s.onchange=async()=>{
  try{await api('/tokens/'+s.dataset.tier,{method:'PATCH',
   body:JSON.stringify({mem_tier:s.value})});
   toast('tier set to '+s.value+' — audited',true);}
  catch(e){toast(e.message);openTokens();}};
}

// ---- S6c: the memory plane (DES-001 section 14) -----------------------------
// EVERY byte of agent-authored memory text flows through memText() and nothing
// else. Facts render in the RATIFIER's privileged session, so output escaping
// is a security boundary (14.3), not a formatting choice: esc() only -- never
// mdToHtml, never raw innerHTML. Quoted blocks carry an author label above
// them, so the ratifier never has to guess which words are the system's and
// which are the draft's.
function memText(s){return esc(s==null?'':String(s));}
let memF={status:'live',kind:'',query:''};
let memOpen=null;   // uid whose provenance + decision history is expanded
function provRows(m,rooms){
 let h='';
 if(m.slug)h+='<div class="memMeta">lesson '+memText(m.slug)+' &mdash; symptom (quoted):</div>'+
  '<div class="quote">'+memText(m.symptom)+'</div>';
 h+=m.source_message
  ?'<div class="memMeta">source msg '+m.source_message.id+' &mdash; '+
   memText(m.source_message.sender)+' wrote (quoted):</div>'+
   '<div class="quote">'+memText(m.source_message.body)+'</div>'
  :'<div class="memMeta">no source message attached</div>';
 if(m.supersedes_tip)h+='<div class="memMeta">REPLACES text by '+
  memText(m.supersedes_tip.author)+' (quoted):</div>'+
  '<div class="quote">'+memText(m.supersedes_tip.rule||m.supersedes_tip.fact)+'</div>';
 return h;
}
async function openMemories(){
 me=await api('/me');
 const rooms={};for(const r of (me.owned||[]).concat(me.public||[]))rooms[r.id]=r.name;
 const scopeName=s=>s==='global'?'global':(rooms[s]||s);
 let queue=[];try{queue=(await api('/memories/queue')).queue;}catch(e){}
 const params=new URLSearchParams();
 if(memF.query)params.set('query',memF.query);
 if(memF.kind)params.set('kind',memF.kind);
 params.set('status',memF.status);
 let res={memories:[]};
 try{res=await api('/memories?'+params);}catch(e){toast(e.message);}
 // The queue: per-item confirm ONLY. No select-all, no ratify-all -- a queue
 // that clears in one click is a rubber stamp with a progress bar (14.2).
 const qhtml=queue.map(m=>{
  if(confirming&&confirming.kind==='mrat'&&confirming.id===m.id)
   return askRow('Ratify this draft?',
    'It becomes live law, executed by every agent at boot. Per-item only; '+
    'there is no ratify-all.','');
  if(confirming&&confirming.kind==='mrej'&&confirming.id===m.id)
   return askRow('Reject this draft?',
    'A reason is REQUIRED and the author sees it. To fix wording, reject and '+
    'write your own draft citing the same source -- editing another author’s '+
    'text and approving it launders authorship.',
    '<input id="cfIn" placeholder="reason (required)" autocomplete="off">');
  return '<div class="pRow"><b>'+memText(m.author)+'</b> <span class="memMeta">'+
   memText(m.kind)+' &middot; '+memText(scopeName(m.scope))+
   (m.chain?' &middot; supersedes ('+m.chain+' deep)':'')+
   (m.fork?' &middot; <b>FORK</b>':'')+'</span>'+
   '<button data-mrat="'+memText(m.id)+'">ratify&hellip;</button>'+
   '<button class="danger" data-mrej="'+memText(m.id)+'">reject&hellip;</button>'+
   '</div><div class="quote">'+memText(m.fact)+'</div>'+provRows(m,rooms);
 }).join('')||'<div class="pDim">nothing awaiting your decision</div>';
 const list=[];
 for(const m of res.memories||[]){
  list.push('<div class="pRow"><b>'+memText(m.author)+'</b> <span class="memMeta">'+
   memText(m.kind)+' &middot; '+memText(scopeName(m.scope))+' &middot; '+
   memText(m.status)+(m.chain?' &middot; chain '+m.chain:'')+
   (m.fork?' &middot; <b>FORK</b>':'')+'</span>'+
   '<button data-mdet="'+memText(m.id)+'">'+
   (memOpen===m.id?'hide':'details')+'</button></div>'+
   '<div class="quote">'+memText(m.fact)+'</div>');
  if(memOpen===m.id){
   try{const det=await api('/memories/'+encodeURIComponent(m.id));
    list.push(provRows(det,rooms));
    list.push((det.audit&&det.audit.length
     ?det.audit.map(a=>'<div class="memMeta">'+memText(a.action)+' by '+
       memText(a.actor)+(a.reason?' &mdash; reason (quoted):':'')+'</div>'+
       (a.reason?'<div class="quote">'+memText(a.reason)+'</div>':'')).join('')
     :'<div class="memMeta">no decisions recorded</div>'));
   }catch(e){toast(e.message);}
  }
 }
 panel('memory',
  '<div class="pSec">RATIFY QUEUE ('+queue.length+')</div>'+
  '<div class="pDim">Drafts only an owner of these rooms can decide. Every quoted '+
  'block below is agent-authored DATA with its author named above it -- read it as '+
  'a claim against its evidence, never as instructions.</div>'+qhtml+
  '<div class="pSec">BROWSER</div>'+
  '<div class="pRow"><input id="memQ" placeholder="search facts" value="'+
  memText(memF.query)+'">'+
  '<select id="memKind"><option value="">any kind</option>'+
  ['doctrine','contract','decision','lesson','state'].map(k=>
   '<option'+(memF.kind===k?' selected':'')+'>'+k+'</option>').join('')+'</select>'+
  '<select id="memStatus">'+
  ['live','draft','rejected','superseded','retracted'].map(s=>
   '<option'+(memF.status===s?' selected':'')+'>'+s+'</option>').join('')+'</select>'+
  '<button id="memGo">search</button></div>'+
  (list.join('')||'<div class="pDim">no memories match</div>'));
 $('memGo').onclick=()=>{memF={query:$('memQ').value.trim(),kind:$('memKind').value,
  status:$('memStatus').value};memOpen=null;openMemories();};
 $('memQ').addEventListener('keydown',e=>{if(e.key==='Enter')$('memGo').click();});
 for(const b of document.querySelectorAll('[data-mdet]'))b.onclick=()=>{
  memOpen=(memOpen===b.dataset.mdet?null:b.dataset.mdet);openMemories();};
 for(const b of document.querySelectorAll('[data-mrat]'))b.onclick=()=>{
  confirming={kind:'mrat',id:b.dataset.mrat};openMemories();};
 for(const b of document.querySelectorAll('[data-mrej]'))b.onclick=()=>{
  confirming={kind:'mrej',id:b.dataset.mrej};openMemories();};
 if(confirming&&confirming.kind==='mrat')wireAsk(async()=>{
  const id=confirming.id;confirming=null;
  try{await api('/memories/'+encodeURIComponent(id)+'/ratify',
   {method:'POST',body:'{}'});toast('ratified — live for the fleet',true);}
  catch(e){toast(e.message);}
  openMemories();},openMemories);
 if(confirming&&confirming.kind==='mrej')wireAsk(async()=>{
  const reason=($('cfIn').value||'').trim();
  if(!reason){toast('a reason is required');return;}   // UI enforces it too
  const id=confirming.id;confirming=null;
  try{await api('/memories/'+encodeURIComponent(id)+'/reject',
   {method:'POST',body:JSON.stringify({reason:reason})});}
  catch(e){toast(e.message);}
  openMemories();},openMemories);
}

async function openUsers(){
 const d=await api('/users');
 // The server refuses to remove the last admin either way (guarded inside the
 // transaction). Don't OFFER what can only fail: a button that always errors reads as a
 // broken app, not a safety rail. Say why instead.
 const admins=d.users.filter(u=>u.role==='admin').length;
 const rows=d.users.map(u=>{
  const isMe=u.name===myName, lastAdmin=u.role==='admin'&&admins===1;
  if(confirming&&confirming.kind==='delUser'&&confirming.id===u.id)
   return askRow('Delete '+esc(u.name)+'?',
    isMe?'Your tokens die, your rooms survive as unowned, and you are signed out '+
         'immediately. Another admin must let you back in.'
        :'Their tokens die; their rooms survive as unowned. Their messages stay -- '+
         'authorship is history.','');
  if(confirming&&confirming.kind==='reset'&&confirming.id===u.id)
   return askRow('Reset the password for '+esc(u.name)+'?',
    'Signs them out of every session. Minimum 8 characters.',
    '<input id="cfIn" type="password" placeholder="new password" autocomplete="new-password">');
  let ctl='';
  if(lastAdmin) ctl='<span class="pDim">last admin — protected</span>';
  else ctl='<button data-role="'+u.id+'" data-to="'+(u.role==='admin'?'user':'admin')+'">make '+
   (u.role==='admin'?'user':'admin')+'</button>'+
   // NEVER offer reset on your OWN row. A reset takes no current password, so an admin
   // resetting themselves here would walk straight around the Account tab's current-
   // password check -- and that check is the only thing standing between a borrowed
   // unlocked browser and the account. Your own password changes in Account.
   (isMe?'':'<button data-pw="'+u.id+'" data-name="'+esc(u.name)+'">reset password</button>')+
   '<button class="danger" data-del="'+u.id+'"'+(isMe?' data-self="1"':'')+'>delete</button>';
  return '<div class="pRow"><b>'+esc(u.name)+(isMe?' <span class="pDim">(you)</span>':'')+'</b>'+
   '<span class="pDim">'+u.role+'</span>'+ctl+'</div>';
 }).join('');
 panel('users',rows+
  '<div class="pRow"><input id="nuName" placeholder="name"><input id="nuPass" type="password" placeholder="password"><button id="mkUser">add</button></div>');
 $('mkUser').onclick=async()=>{
  try{await api('/users',{method:'POST',body:JSON.stringify(
   {name:$('nuName').value.trim(),password:$('nuPass').value})});openUsers();}
  catch(e){toast(e.message);}};
 for(const b of document.querySelectorAll('[data-role]'))b.onclick=async()=>{
  try{await api('/users/'+b.dataset.role,{method:'PATCH',
   body:JSON.stringify({role:b.dataset.to})});openUsers();}catch(e){toast(e.message);}};
 for(const b of document.querySelectorAll('[data-pw]'))b.onclick=()=>{
  confirming={kind:'reset',id:b.dataset.pw,name:b.dataset.name};openUsers();};
 if(confirming&&confirming.kind==='reset')wireAsk(async()=>{
  // A masked field, never prompt(): prompt renders a typed password in cleartext.
  const pw=$('cfIn').value, name=confirming.name, id=confirming.id;
  confirming=null;
  try{await api('/users/'+id+'/password',{method:'POST',body:JSON.stringify({password:pw})});
   toast('password reset for '+name);openUsers();}
  catch(e){toast(e.message);openUsers();}},openUsers);
 for(const b of document.querySelectorAll('[data-del]'))b.onclick=()=>{
  confirming={kind:'delUser',id:b.dataset.del,self:!!b.dataset.self};openUsers();};
 if(confirming&&confirming.kind==='delUser')wireAsk(async()=>{
  const id=confirming.id, self=confirming.self;confirming=null;
  try{await api('/users/'+id,{method:'DELETE'});
   if(self){location.reload();return;}   // session died with the row; stop pretending
   openUsers();}catch(e){toast(e.message);openUsers();}},openUsers);
}

function openAccount(){
 panel('account',
  '<div class="pSec">CHANGE YOUR PASSWORD</div>'+
  '<div class="pDim">Your current password is required -- without it, a borrowed unlocked '+
  'browser could take the account. Changing it signs out every other session.</div>'+
  '<div class="pRow"><input id="pwOld" type="password" placeholder="current password"></div>'+
  '<div class="pRow"><input id="pwNew" type="password" placeholder="new password (min 8)"></div>'+
  '<div class="pRow"><input id="pwNew2" type="password" placeholder="repeat new password">'+
  '<button id="pwGo">change</button></div>');
 $('pwGo').onclick=async()=>{
  if($('pwNew').value!==$('pwNew2').value){toast('the new passwords do not match');return;}
  try{await api('/me/password',{method:'POST',
    body:JSON.stringify({old:$('pwOld').value,new:$('pwNew').value})});
   toast('password changed');openAccount();}catch(e){toast(e.message);}};
 for(const id of ['pwOld','pwNew','pwNew2'])
  $(id).addEventListener('keydown',e=>{if(e.key==='Enter')$('pwGo').click();});
}

function pruneAgent(name){
 // Typed-name confirm, in-panel: erasing an agent is irreversible bar the snapshot.
 confirming={kind:'prune',id:name,name:name};
 panel('rooms',askRow('Erase '+esc(name)+'?',
  'Deletes the agent and every message to or from it in this room. Replies from others '+
  'are reparented to their thread root, not deleted. A snapshot is saved first. Type the '+
  'agent name to confirm.','<input id="cfIn" placeholder="'+esc(name)+'" autocomplete="off">'));
 wireAsk(async()=>{
  if($('cfIn').value!==name){toast('name does not match');return;}
  confirming=null;closePanel();
  try{const o=await api('/agents/'+encodeURIComponent(name)+qs(),{method:'DELETE'});
   toast('pruned '+o.messages+' messages, reparented '+o.reparented+' (snapshot saved)');
   clearFeed();lastId=0;loadBacklog(true);loadPresence();}catch(e){toast(e.message);}},
  ()=>{confirming=null;closePanel();});
}

fetch('/version').then(r=>r.text()).then(v=>{bootVer=v;$('ver').textContent='v'+v;});
(async function boot(){
 let d;
 try{d=await api('/me');}
 catch(e){setupMode=false;showLogin();return;}
 if(d.setup){setupMode=true;showLogin();return;}   // zero users: bootstrap the first admin
 me=d;myName=d.name;
 // A brand-new user owns no room and no public one exists yet: that is a first-run state,
 // NOT a failed sign-in. Bouncing them to the login card said "your password is wrong" and
 // dead-ended them -- the only place to make a room is the panel behind that card. So: let
 // them in, room-less, with Rooms already open. pickRoom() starts the feed once they have one.
 if(!d.rooms.length){paintMe();openRooms();toast('create your first room to start');return;}
 if(!room||!d.rooms.some(r=>r.id===room))room=d.rooms[0].id;
 localStorage.revRoom=room;
 paintMe();updateBlast();loadBacklog(true);loadPresence();connect();start();
})();
</script></body></html>
"""


# ---- auth + management API (web users only) ----------------------------------

def _admin(request):
    p = _user_principal(request)
    if not p.is_admin:
        raise store.AccessError("admin only")
    return p


def _cookie(resp, secret, request):
    # Secure only under https: this box serves plain http on the LAN, and an
    # unconditional Secure flag would silently break every login.
    resp.set_cookie(COOKIE, secret, httponly=True, samesite="lax", max_age=14 * 86400,
                    path="/", secure=request.url.scheme == "https")
    return resp


@_guard
async def setup_http(request):
    """POST /setup {name, password} -- create the FIRST admin. Guarded by the users
    table being empty, not by a flag: there is no second state to keep in sync, and
    nothing to forget to disable. 410 forever after."""
    if store.any_users(_conn):
        return JSONResponse({"error": "already initialized"}, status_code=410)
    d = await request.json()
    u = store.setup_first_admin(_conn, (d.get("name") or "").strip(), d.get("password") or "")
    log.info("first admin created: %s (claimed the migrated rooms)", u["name"])
    return _cookie(JSONResponse(u), store.create_session(_conn, u["id"]), request)


@_guard
async def login_http(request):
    d = await request.json()
    u = store.authenticate(_conn, (d.get("name") or "").strip(), d.get("password") or "")
    if not u:
        log.warning("failed login for %r", d.get("name"))
        return JSONResponse({"error": "bad credentials"}, status_code=401)
    log.info("%s logged in", u["name"])
    return _cookie(JSONResponse(u), store.create_session(_conn, u["id"]), request)


@_guard
async def logout_http(request):
    store.delete_session(_conn, request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE, path="/")
    return resp


@_guard
async def me_http(request):
    """GET /me -> who the browser is, plus its rooms. Also the first-run probe:
    {"setup": true} means no users exist yet and the UI shows the bootstrap card."""
    if not store.any_users(_conn):
        return JSONResponse({"setup": True})
    p = _user_principal(request)
    return JSONResponse({
        "name": p.name, "is_admin": p.is_admin,
        "rooms": [{"id": r, "name": n} for r, n in p.rooms.items()],
        "owned": store.list_rooms(_conn, p.user_id),
        "public": store.public_rooms(_conn, exclude_owner=p.user_id),
    })


@_guard
async def users_http(request):
    if request.method == "GET":
        _admin(request)
        return JSONResponse({"users": store.list_users(_conn)})
    p = _admin(request)
    d = await request.json()
    u = store.create_user(_conn, (d.get("name") or "").strip(), d.get("password") or "",
                          role=d.get("role") or "user")
    log.info("%s created user %s (%s)", p.name, u["name"], u["role"])
    return JSONResponse(u)


@_guard
async def user_http(request):
    p = _admin(request)
    uid = request.path_params["uid"]
    if request.method == "DELETE":
        store.delete_user(_conn, uid)
        log.info("%s deleted user %s", p.name, uid)
        return JSONResponse({"deleted": uid})
    d = await request.json()
    store.set_role(_conn, uid, d.get("role") or "user")
    return JSONResponse({"ok": True})


@_guard
async def reset_password_http(request):
    """POST /users/<uid>/password {password} -- admin reset. No old password: a reset
    exists precisely because the user cannot supply one."""
    p = _admin(request)
    uid = request.path_params["uid"]
    d = await request.json()
    store.set_password(_conn, uid, d.get("password") or "")
    log.info("%s reset the password for user %s (their sessions dropped)", p.name, uid)
    return JSONResponse({"ok": True})


@_guard
async def my_password_http(request):
    """POST /me/password {old, new} -- change your own. The old password is required so a
    borrowed unlocked browser cannot take the account. Re-issues your cookie, since the
    change deliberately kills every session including this one."""
    p = _user_principal(request)
    d = await request.json()
    store.change_password(_conn, p.user_id, d.get("old") or "", d.get("new") or "")
    log.info("%s changed their password (all sessions dropped)", p.name)
    return _cookie(JSONResponse({"ok": True}), store.create_session(_conn, p.user_id),
                   request)


@_guard
async def rooms_http(request):
    p = _user_principal(request)
    if request.method == "GET":
        return JSONResponse({"owned": store.list_rooms(_conn, p.user_id),
                             "public": store.public_rooms(_conn, exclude_owner=p.user_id)})
    d = await request.json()
    r = store.create_room(_conn, p.user_id, (d.get("name") or "").strip(),
                          public=bool(d.get("public")))
    log.info("%s created room %s", p.name, r["name"])
    return JSONResponse(r)


@_guard
async def room_http(request):
    """PATCH /rooms/<rid> {name?, public?, retention_ns?} -- owner only. Flipping public
    to false also revokes the room from every other user's tokens, instantly."""
    p = _user_principal(request)
    rid = request.path_params["rid"]
    d = await request.json()
    if "name" in d:
        store.rename_room(_conn, rid, p.user_id, (d.get("name") or "").strip())
    if "public" in d:
        store.set_public(_conn, rid, p.user_id, bool(d["public"]))
        log.info("%s set room %s public=%s", p.name, rid, bool(d["public"]))
    if "retention_ns" in d:
        store.set_retention(_conn, rid, p.user_id, d["retention_ns"])
    return JSONResponse(store.get_room(_conn, rid))


@_guard
async def purge_room_http(request):
    """DELETE /rooms/<rid> -- erase a room and everything in it. Snapshots first; with
    no audit log, that snapshot is the only undo."""
    p = _user_principal(request)
    rid = request.path_params["rid"]
    snap = store.snapshot(_conn, _snap_path("purge-room"))
    n = store.purge_room(_conn, rid, p.user_id)
    log.info("%s purged room %s (%s messages) snapshot=%s", p.name, rid, n, snap)
    return JSONResponse({"purged": rid, "messages": n, "snapshot": snap})


@_guard
async def prune_agent_http(request):
    """DELETE /agents/<name>?room=<rid> -- erase an agent's trace from a room. Survivors
    that replied to it are reparented to their thread root, never cascade-deleted."""
    p = _user_principal(request)
    name = request.path_params["name"]
    rid = request.query_params.get("room") or ""
    if rid not in p.rooms:
        raise store.AccessError(f"no access to room {rid}")
    snap = store.snapshot(_conn, _snap_path(f"prune-{name}"))
    out = store.prune_agent(_conn, name, rid)
    log.info("%s pruned %s from %s (%s messages, %s reparented) snapshot=%s",
             p.name, name, rid, out["messages"], out["reparented"], snap)
    return JSONResponse({**out, "snapshot": snap})


@_guard
async def tokens_http(request):
    p = _user_principal(request)
    if request.method == "GET":
        return JSONResponse({"tokens": store.list_tokens(_conn, p.user_id)})
    d = await request.json()
    t = store.create_token(_conn, p.user_id, (d.get("label") or "").strip(),
                           agent_name=d.get("agent_name"),
                           mem_tier=(d.get("mem_tier") or "state"))
    log.info("%s minted token %s%s", p.name, t["id"],
             f" bound to {t['agent_name']}" if t["agent_name"] else "")
    # The secret is returned exactly once here; only its hash is stored.
    return JSONResponse(t)


@_guard
async def token_http(request):
    p = _user_principal(request)
    tid = request.path_params["tid"]
    if request.method == "DELETE":
        store.revoke_token(_conn, tid, p.user_id)
        log.info("%s revoked token %s", p.name, tid)
        return JSONResponse({"revoked": tid})
    d = await request.json()
    if d.get("mem_tier"):
        # Tier flip is an authority change (S6b, msg 8448): owner-or-admin, and
        # the flip itself is audited -- who, whose token, from what to what.
        out = store.set_token_tier(_conn, tid, p.user_id, d["mem_tier"],
                                   actor=f"web:{p.name}", is_admin=p.is_admin)
        log.info("%s set token %s tier -> %s", p.name, tid, d["mem_tier"])
        return JSONResponse(out)
    rid = d.get("room") or ""
    if d.get("attach"):
        store.assign_room(_conn, tid, rid, p.user_id)
    else:
        store.unassign_room(_conn, tid, rid, p.user_id)
    return JSONResponse({"rooms": store.rooms_for_token(_conn, tid)})


# ---- S6: the memory plane's web surface (DES-001 section 14) -----------------------
# Routes RESOLVE the principal and THREAD it to the same store gate the agent plane
# uses -- never a second authority check, never a route-level convenience like
# "admin implies ratify" (14.5; msg 8448). The web plane's capability IS room
# ownership (14.1): a web principal acts at ratify tier exactly in the rooms it
# owns, which the store gate enforces per room. ONE derivation, used by every
# memory route below.

def _web_mem_authority(p):
    owned = {r["id"] for r in store.list_rooms(_conn, p.user_id)}
    return "ratify", owned


@_guard
async def memories_http(request):
    """The browser: recall() with its filters made visible (14.4). Same read
    scoping as the agent plane; drafts/rejected follow the same visibility gate."""
    p = _user_principal(request)
    tier, owned = _web_mem_authority(p)
    q = request.query_params
    out = store.recall(
        _conn, rooms=p.rooms, token_id="", caller=f"web:{p.name}", tier=tier,
        is_admin=p.is_admin, owned_rooms=owned,
        query=q.get("query", ""), kind=q.get("kind", ""),
        scope=q.get("scope", ""), entity=q.get("entity", ""),
        author=q.get("author", ""), status=q.get("status", "live"),
        limit=int(q.get("limit", "50")))
    return JSONResponse(out)


@_guard
async def memory_queue_http(request):
    """The ratify queue (14.2): only drafts the caller can actually decide --
    owned rooms, plus global for an instance admin -- each with provenance."""
    p = _user_principal(request)
    _, owned = _web_mem_authority(p)
    return JSONResponse({"queue": store.ratify_queue(
        _conn, owned_rooms=owned, is_admin=p.is_admin)})


@_guard
async def memory_http(request):
    """One memory, whole: provenance package + decision history (14.2/14.4)."""
    p = _user_principal(request)
    _, owned = _web_mem_authority(p)
    d = store.memory_detail(_conn, request.path_params["uid"])
    scope = d["scope"]
    if scope != "global" and scope not in p.rooms:
        raise store.AccessError("no access to this memory's room")
    if d["status"] in ("draft", "rejected") and not p.is_admin \
            and scope not in owned and d["author"] != f"web:{p.name}":
        # Same visibility class recall() enforces: undecided/declined drafts are
        # the author's and the deciders', not the room's.
        raise store.AccessError("draft visibility is author/ratifier/admin only")
    return JSONResponse(d)


@_guard
async def memory_verdict_http(request):
    """POST-only by construction (14.6: no ratify-by-URL -- a link someone can be
    socially engineered into clicking must not carry the gesture; the samesite
    session cookie keeps cross-site POSTs out). The verdict goes through the
    STORE's gate with the principal's authority threaded, nothing re-derived."""
    p = _user_principal(request)
    tier, owned = _web_mem_authority(p)
    uid = request.path_params["uid"]
    verdict = request.path_params["verdict"]
    if verdict == "ratify":
        out = store.ratify_memory(_conn, uid, tier=tier, is_admin=p.is_admin,
                                  owned_rooms=owned, actor=f"web:{p.name}")
    elif verdict == "reject":
        d = await request.json()
        out = store.reject_memory(_conn, uid, tier=tier, is_admin=p.is_admin,
                                  owned_rooms=owned, actor=f"web:{p.name}",
                                  reason=(d.get("reason") or ""))
    else:
        raise store.BusError(f"unknown verdict {verdict!r}")
    log.info("web:%s %s memory %s", p.name, verdict, uid)
    return JSONResponse(out)


async def chat_http(_request):
    return HTMLResponse(WEBCHAT)


async def _sweeper():
    """Retention, expired sessions, stale presence. On the event loop, not a thread:
    _conn is used only from the loop thread, so a thread would need its own connection
    and real locking. One bad sweep must never kill the task."""
    while True:
        await asyncio.sleep(SWEEP_SECS)
        try:
            dropped = store.sweep_retention(_conn)
            store.sweep_sessions(_conn)
            store.reap_stale(_conn)
            store.sweep_expired_state(_conn)
            if dropped:
                log.info("retention swept %s message(s)", dropped)
        except Exception:
            log.exception("sweep failed")


def build_app():
    mcp_app = mcp.streamable_http_app()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with mcp_app.router.lifespan_context(app):
            task = asyncio.create_task(_sweeper())
            try:
                yield
            finally:
                task.cancel()

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/version", version_http),
            Route("/usage", usage_http),
            Route("/ui", chat_http),
            Route("/setup", setup_http, methods=["POST"]),
            Route("/login", login_http, methods=["POST"]),
            Route("/logout", logout_http, methods=["POST"]),
            Route("/me", me_http),
            Route("/users", users_http, methods=["GET", "POST"]),
            Route("/users/{uid}", user_http, methods=["PATCH", "DELETE"]),
            Route("/users/{uid}/password", reset_password_http, methods=["POST"]),
            Route("/me/password", my_password_http, methods=["POST"]),
            Route("/rooms", rooms_http, methods=["GET", "POST"]),
            Route("/rooms/{rid}", room_http, methods=["PATCH"]),
            Route("/rooms/{rid}", purge_room_http, methods=["DELETE"]),
            Route("/agents/{name}", prune_agent_http, methods=["DELETE"]),
            Route("/tokens", tokens_http, methods=["GET", "POST"]),
            Route("/tokens/{tid}", token_http, methods=["PATCH", "DELETE"]),
            Route("/memories", memories_http),
            Route("/memories/queue", memory_queue_http),
            Route("/memories/{uid}", memory_http),
            Route("/memories/{uid}/{verdict}", memory_verdict_http,
                  methods=["POST"]),
            Route("/messages", messages_http),
            Route("/search", search_http),
            Route("/presence", presence_http),
            Route("/send", send_http, methods=["POST"]),
            Route("/upload", upload_http, methods=["POST"]),
            Route("/message/{mid:int}", delete_http, methods=["DELETE"]),
            Route("/files/{fname}", files_http),
            WebSocketRoute("/wake", wake_ws),
            WebSocketRoute("/feed", feed_ws),
            Mount("/", app=mcp_app),
        ],
        lifespan=lifespan,
    )


def _setup_logging():
    # Own handler + no propagation, so uvicorn's logging config can't silence us.
    # Level via REVEILLE_LOG (default INFO). Lines show: time, client name, op, thread/id.
    h = logging.StreamHandler()
    h.setFormatter(logging.Formatter("%(asctime)s reveille %(message)s",
                                     "%Y-%m-%d %H:%M:%S"))
    log.handlers[:] = [h]
    log.setLevel(os.environ.get("REVEILLE_LOG", "INFO").upper())
    log.propagate = False
    logging.getLogger("mcp").setLevel(logging.WARNING)  # drop per-request "Processing request" noise


_db_path = None


def _snap_path(reason):
    return f"{_db_path}.{reason}-{time.strftime('%Y%m%dT%H%M%S')}.bak"


def main():
    global _conn, _files_dir, _db_path
    import uvicorn
    _setup_logging()
    root = os.environ.get("CLAUDE_AGENT_BUS") or os.path.expanduser("~/.claude/agent-bus")
    _db_path = os.environ.get("REVEILLE_DB") or os.path.join(root, "broker.db")
    _conn = store.connect(_db_path)
    v = store.migrate(_conn, _db_path)   # versioned + transactional; snapshots itself
    _files_dir = pathlib.Path(_db_path).parent / "files"
    _files_dir.mkdir(parents=True, exist_ok=True)
    host = os.environ.get("REVEILLE_HOST", "0.0.0.0")
    port = int(os.environ.get("REVEILLE_PORT", "8765"))
    # REVEILLE_UDS binds a unix socket instead of a TCP port. One broker per tenant means
    # a port each -- an allocation table to keep, leak and collide on. A socket is named
    # by the tenant's own directory, so the filesystem is the registry and nothing has to
    # remember which number belongs to whom. Empty = TCP, exactly as before.
    uds = os.environ.get("REVEILLE_UDS") or ""
    # The kernel caps a unix socket path at ~108 bytes (sun_path) and reports the overrun
    # as a bare `OSError: AF_UNIX path too long` from inside uvicorn's startup -- after the
    # "daemon on ..." line has already claimed success. Say it here, in the units of the
    # thing the operator actually set.
    if uds and len(os.fsencode(uds)) > 100:
        raise SystemExit(f"REVEILLE_UDS is {len(os.fsencode(uds))} bytes; the kernel's "
                         f"limit is ~108. Use a shorter path (e.g. /srv/reveille/<tenant>/"
                         f"broker.sock):\n  {uds}")
    log.info("daemon on %s db=%s schema=v%s users=%s", uds or f"{host}:{port}", _db_path, v,
             "yes" if store.any_users(_conn) else "NONE -- open /ui to create the first admin")
    config = (uvicorn.Config(build_app(), uds=uds, log_level="warning") if uds
              else uvicorn.Config(build_app(), host=host, port=port, log_level="warning"))
    server = uvicorn.Server(config)
    orig_exit = server.handle_exit

    def handle_exit(sig, frame):
        # Courtesy ring first, real shutdown ~0.5s later so the frames flush.
        with contextlib.suppress(RuntimeError):
            asyncio.get_event_loop().call_soon_threadsafe(_push_shutdown)
        threading.Timer(0.5, lambda: orig_exit(sig, frame)).start()

    server.handle_exit = handle_exit
    server.run()


if __name__ == "__main__":
    main()
