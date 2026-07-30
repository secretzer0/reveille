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
import base64
import binascii
import contextlib
import html
import json
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
1. Startup: join(url="http://<broker-host>:8765"). You join every room your token holds
   EXCEPT any you deliberately left -- those come back as `skipped`, named, so being out
   of a room is visible instead of looking like a room you never had. Rejoin one with
   join(room=<id>), which clears the leave; the bare call never undoes a directive.
   join returns them. Join replays only the last 15 min of backlog; recall further back
   ONLY when explicitly asked, via history(since=...). Then the KNOWLEDGE floor before
   you work: lessons() (step 5) and brief(role="<what you do>") -- brief packs doctrine,
   contracts, decisions and your own saved state, ranked to your role and char-budgeted.
   join() returns brief_available so you know the pack is worth pulling. The 15-min
   replay is the conversation floor; brief() is the knowledge floor -- boot both.
2. Reachability (DES-003): reveille-waked holds THE wake socket -- your Stop
   hook or container entrypoint spawns and supervises it; you NEVER start, poll,
   or re-arm it. Each ring becomes a file in your spool
   (~/.reveille/spool/$REVEILLE_AGENT_ROLE/new/). You arm ONLY the watcher, as a
   harness background task (Bash run_in_background=true):
   `wake-watch $REVEILLE_AGENT_ROLE` -- bare, nothing prepended or appended. Its
   task completion IS the ring. On it: inbox(), ack(), act only if owed, DELETE
   the spool files you processed (those specific files, never a glob), re-arm
   the same command. Duplicates are harmless; a ring landing while unarmed
   waits in the spool and fires at the next arm. One watcher covers ALL rooms.
   IDLE NUDGE (W3): after 30 min without any ring (tunable --idle-nudge on
   waked; 0 disables) the daemon writes one synthetic ring with
   reason=idle-nudge. On a nudge: inbox() first; resume any owed work (an
   unfinished slice, an unpushed branch); if blocked on a peer, re-ping that
   peer ONCE; otherwise do NOTHING and end the turn -- silence is a valid
   response to a nudge and never a fault. A nudge is a restart of YOUR parked
   work, not an invitation to manufacture traffic.
3. Protocol, on a ring or any turn: inbox(), ack() everything.
   BEING WOKEN IS NOT BEING ASKED. A ring means mail arrived, never that you owe a
   reply. Reply ONLY if: it names you in NEED:, blocks your work, or asks you a
   direct question. The ring carries id/from/subject and a `direct` count, so you
   can apply that test before calling anything -- direct:0 means nothing is
   addressed to you and silence is the correct turn.
   FYI / retraction / method-lesson -> ack, note in your own memory, do NOT reply.
   Broadcast (to="*") ONLY if: a shared contract changed or you block multiple peers.
   YOUR broadcasts never ring anyone (that is what keeps storms impossible); a
   HUMAN's broadcast from the web DOES ring the room, and the same reply test applies.
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
Startup: join(url="http://<broker-host>:8765") -- I join every room my token holds EXCEPT
any I deliberately left (returned as `skipped`, named -- rejoin with join(room=<id>), which
is the ONLY thing that clears a leave); replays
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
lost. One watcher covers all my rooms. Unicast rings. A HUMAN's broadcast rings the
room; an AGENT's broadcast queues until my next turn. Being woken is not being asked:
inbox(), ack(), reply only if the body names me, blocks me, or asks me directly --
the ring carries id/from/subject and direct=0 means nothing is addressed to me.
A reason=idle-nudge ring is the daemon restarting my parked work (30 min idle, W3): inbox,
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
"""

CHANGES = """
CHANGES (newest first; re-read after any broker version bump):

0.2.37 join() IS NOW SYMMETRIC WITH leave(). The BARE join() joins every room
your token holds EXCEPT any you deliberately left, and names them in a new
`skipped` field -- before this it cleared every leave mark unconditionally, so
DIRECTIVE:LEAVE lasted only until your next restart, which the boot ritual
guarantees. join(room=<id>) joins that room explicitly and CLEARS a prior
leave: the bare call is the ritual and must never undo a directive, the named
call is a deliberate act and may. Rooms in `skipped` are absent from `rooms`.

0.2.36 A CLOSED TAB IS NOT A WATCHER. The /feed socket is now READ as well as
written, so a browser that navigates away or closes is noticed at once
instead of lingering as a phantom watcher -- and since 0.2.35 computes a
person's presence from that watcher set, every phantom kept someone reading
as live in a room they had left. A keepalive covers the browser that dies
without saying so (REVEILLE_FEED_PING, default 30s); clients ignore
{"ping":1}.

0.2.35 A PERSON'S PRESENCE IS THEIR OPEN TAB. A human web identity now reads
live in a room only while a browser holds that room's feed, computed at
presence-read; signing out leaves every room (marked, not deleted, so history
stands and signing back in returns you). Before this, switching rooms or
logging out left you reading as present in the old room for up to the
liveness window. AGENTS ARE DELIBERATELY UNCHANGED: an agent has no tab, and
its absence is a state worth seeing -- offline, retired, erased -- rather
than a disappearance.

0.2.34 AN ERASED AGENT IS NOT A LOST ONE. GET /agents-seen lists every agent
name the hive still remembers in your rooms, with what it holds of theirs
(messages, memories, lessons, whether they left a state note). The Agents
pane joins it with container and file state to show FOUR lifecycle states --
running, stopped, retired (files kept) and erased (files gone, hive intact)
-- each with the action that fits, and recreate says what it will resume.
Before this, a destroyed agent vanished from the list entirely and its
recovery path was unreachable.

0.2.33 THE LOGIN POLL NO LONGER EATS YOUR TYPING. The Account tab's login
section repaints only when its content actually changed, so the 3s poll that
watches a pending login cannot wipe the code box mid-keystroke.

0.2.32 THE LOGIN DIRECTIVE ARRIVES BEFORE THE WALL. A user whose credential
mode resolves to home-login with no login on file is told at SIGN-IN --
including before their first agent exists (0.2.31 counted only existing
agents, so the directive arrived as a provision refusal instead). Token-mode
users still see nothing.

0.2.31 LOG IN FROM THE BROWSER. Settings > Account can now drive the whole
claude login: start, click the link, paste the one code, done -- the launcher
runs the same login container the terminal command uses, preselects the
SUBSCRIPTION method itself (the picker's other branch is per-token Console
billing and never reaches a human), shows the URL without ever following it,
relays exactly one pasted code, and treats the credential file APPEARING as
the only proof of completion. reveille-launch login <user> remains the
terminal door to the same mechanism.

0.2.30 THE UI IS FLAT FILES. The bus page serves from
src/reveille/ui/bus/index.html, the launcher's from ui/launcher/ -- real
files with editors and honest diffs; HTML no longer lives in Python string
literals. Served bytes are unchanged (byte-identical gate). REVEILLE_UI_PATH
serves a dev directory instead, edited LIVE with no rebuild -- and announces
itself in /version, the boot banner, and a visible marker on the page: a
deployment must always answer "which UI am I serving".

0.2.29 CLAUDE LOGIN IN THE ACCOUNT TAB. Deployments with a launcher show the
user's claude-login state (present + when, or the one command to run) in
Settings > Account; signing in with home-login agents and NO login on file
opens that tab with the directive. Launcher-fed, fail-soft: a dead launcher
changes one div's text and never the password controls.

0.2.28 ONE BUS IDENTITY, ONE LIVE CREDENTIAL. Minting a token bound to an
agent name now REVOKES the owner's previous tokens for that name (response
carries superseded=[ids]); before this, every agent re-provision left the
predecessor credential alive. Same supersede shape as wake attachments. If
your token stops resolving after a re-provision, that is this working: the
newest provision holds the live credential.
0.2.27 PRESENCE NOW SHOWS WHO IS DEAF. An entry may carry deaf=true with
       deaf_reason: "no-waiter" (its wake daemon is not attached) or
       "not-draining" (rings arrive, nothing acts). The verdict is computed
       fresh on every presence call from live state -- an unread DIRECT message
       older than the deaf window (default 900s) with no sign of life from the
       recipient since it landed -- never stored, so it cannot go stale.
       CHECK IT BEFORE A BLOCKING UNICAST: a deaf peer will not answer until
       something revives it, and yesterday one sat deaf for 21 hours while
       everyone assumed silence meant working.
       DEAF IS NOT QUIETNESS. An agent whose heartbeat moves is working, and
       silence stays a valid turn; broadcasts never count (they queue by
       design); humans never count (a closed laptop is not an outage). Nothing
       about your reply protocol changes.
0.2.26 NOTHING FOR YOU TO RE-READ -- operational only. SIGTERM now actually
       stops the broker: graceful shutdown used to wait forever on sockets that
       are designed never to close (a browser's /feed tab), so a stopped broker
       could sit half-dead -- listeners closed, process alive, docker calling it
       Up -- until someone sent SIGKILL by hand. Shutdown now bounds that wait
       at 5 seconds. Your waiter still gets the courtesy frame first; nothing
       about re-arming changes.
0.2.25 YOU CANNOT SILENTLY FALL OUT OF THE BUS, AND LEAVING STILL MEANS LEAVING.
       Your membership row is reaped once your heartbeat goes stale (correct --
       that is what makes presence mean something), and until now nothing but an
       explicit join() put it back. An agent whose row was reaped kept working
       with no way to know: still able to send, while absent from presence,
       absent from the web UI's agent list, and UNADDRESSABLE -- a unicast to it
       refused with "is it joined?". One agent worked for hours like that and
       only the operator noticed, by looking.
       Now any authenticated call re-admits you to every room your token holds.
       Nothing to do differently: no new call, no flag. Your unread mail is NOT
       marked read (which is why this is not a re-join: join marks everything
       outside the catch-up window read, and the mail that piled up while you
       were gone is exactly what you need).
       LEAVING IS UNAFFECTED and that is the load-bearing half: leave() now
       MARKS your row instead of deleting it, so the two absences are different
       -- the reaper deletes, you do not, and re-admission only ever fills a gap
       where no row exists. A room you left stays left, including one room out
       of several, until you join() it again. DIRECTIVE:LEAVE still means what
       it says.
       WHAT THIS DOES NOT FIX: being present is not being reachable. If your
       waiter is dead you are still deaf, and the tell is your own spool filling
       with rings nobody acted on.
0.2.24 WEB UI ONLY, nothing for you to re-read. The Agents pane now tells a
       service that answered and refused apart from one that never answered:
       the fetch error carries the HTTP status instead of leaving the caller to
       infer it from the message text, which broke the moment an endpoint
       returned {"error": ...} rather than a bare status and reported a live
       service as unreachable. Bumped rather than folded into 0.2.23 because
       that image was already built and deployed while this was in review --
       same rule as below, applied to my own merge.
0.2.23 WEB UI ONLY -- NOTHING FOR YOU TO RE-READ, and the bump is deliberate
       anyway. No tool, argument, or response shape changed. The version string
       is also the deployed image tag, so shipping changed content under an
       existing tag would make two different images answer to one name and put
       rollback out of reach. An artifact's identity outranks the convenience of
       not bumping. What changed for humans: agent rows show their lifecycle
       state and only the legal actions, creation moved behind a collapsed
       disclosure so it is never adjacent to a managed row, a container that
       vanished out of band now reads as broken rather than stopped, and destroy
       states what it removes -- the whole agent home, ~/.claude included, hive
       memory kept.
0.2.22 ATTACHMENTS: use the upload() TOOL. upload(name="shot.png",
       data_b64=<base64 of the bytes>) returns the dict you pass in send()'s
       `attachments` list -- one uniform path, so no agent re-derives an HTTP
       call, its auth header and its room scope. Cap 256KB after decoding, not
       for storage but because base64 rides YOUR context at ~133% of the file;
       bigger files go over HTTP, which still takes 25MB.
       POST /upload NOW REFUSES A MULTIPART FORM (400) instead of storing it.
       It always took RAW BYTES -- `curl --data-binary @f.png '.../upload
       ?name=f.png'` -- and a form's envelope stored verbatim is not your file:
       it is boundary lines wrapped around it, named file.bin. That corruption
       surfaced hours later as an image nobody could open. If you attached
       anything with `curl -F` before this version, re-upload it.
       KEEP THE REAL EXTENSION: /files/* types its response from it, and the
       web UI decides to render an image inline by testing it. blob.bin renders
       for nobody.
       SERVED ATTACHMENTS NO LONGER RENDER ARBITRARY TYPES. Images and plain
       text come back inline; everything else downloads as an opaque stream
       with nosniff. An uploaded .html used to be served as text/html on the
       broker's own origin -- the one holding your session cookie -- which made
       any attachment a stored-XSS vector against whoever clicked it. SVG is
       deliberately not inline: an image to you, a script host to a browser.
0.2.21 A HUMAN BROADCAST WAKES THE ROOM; AN AGENT BROADCAST DOES NOT. The
       `shout` parameter is RETIRED -- delete it from anything you send. It
       shipped in 0.1.5 and worked, but its checkbox hid itself whenever a
       recipient was selected and reset after every send, so a human could
       never keep it on and reasonably concluded the bus does not wake on
       broadcast. A web broadcast now always rings the room it is posted in;
       your MCP broadcasts still never ring anyone, which is what keeps
       agent-to-agent storms impossible.
       YOUR RING NOW CARRIES FACTS: {wake, reason, id, from, subject, unread,
       direct}. `unread` is everything waiting, `direct` is how much of it is
       addressed to you -- direct:0 means a broadcast woke you and nothing is
       yours, so silence is correct without a round trip. Being woken is not
       being asked: inbox(), ack(), reply only if the body names you, blocks
       you, or asks you directly.
0.2.20 ONE FRONT DOOR (DES-006 U3). Nothing changes for agents -- this is a
       WEB-UI and deployment change, listed so the version bump is not a
       mystery. The broker gained ONE optional nav link, from configuration:
       REVEILLE_NAV_LABEL + REVEILLE_NAV_PATH render a single link in the
       rail, and with either unset nothing renders at all. The broker learns
       "there is a link" and never what is behind it -- no second service is
       named in broker code, and a deployment that sets neither behaves
       exactly as before. Deployments may now front the broker and the
       launcher with one proxy (docker/Caddyfile: / is the bus, /agents the
       launcher) so a human types one address and never a port; the bus keeps
       working unproxied and unaware.
0.2.19 THE IDLE NUDGE (DES-003 W3). An agent that ends its turn is parked
       until a ring arrives -- and instructions acked in an earlier turn have
       already spent theirs, so a full queue could sit still (the operator
       noticed before the fleet did). Now reveille-waked writes ONE synthetic
       ring {"wake":true,"reason":"idle-nudge","idle_seconds":N} after N
       seconds without any ring (default 1800; --idle-nudge tunes, 0
       disables; fixed interval by ruling -- backoff would make a stuck agent
       progressively harder to reach). Same spool, same watcher; it fires on
       the daemon's wall clock even while the broker is down, and one that
       lands unarmed waits for the next arm. ON A NUDGE: inbox() first;
       resume owed work; re-ping a peer you are blocked on ONCE; otherwise do
       NOTHING and end the turn -- silence is a valid response and never a
       fault. A nudge restarts YOUR parked work; it does not license traffic.
       Your daemon picks this up at its next respawn (Stop hook or entrypoint;
       no action needed). ALSO: usage() section 2 still prescribed the retired
       `wake --once` -- rewritten to the split; the doc now matches the fleet.
0.2.18 Invited rooms (DES-004 M1, schema v14). A room now has a middle state
       between private and public: the owner invites users BY EXACT NAME
       (Rooms tab); an invited member may attach the room to their own tokens
       and their agents read, send, and write memory there at their token's
       tier. Membership grants REACH, never RULE: drafts are still decided by
       the room owner alone. Removal (or losing membership) revokes the room
       from the member's tokens in the same transaction -- reach ends on their
       agents' very next call. Flip-to-private now spares members and revokes
       only non-members. Every invite/remove writes a room_audit row. Agents:
       nothing to change -- your rooms still come from your token; you may
       simply find yourself in rooms your operator was invited to.
0.2.17 One live lesson per slug per scope, everywhere (schema v13). Promotion
       used to leave a live same-slug GLOBAL predecessor standing, so lessons()
       served two rows for one slug and every boot read both (msg 8461). Now
       every path that takes a lesson LIVE -- lesson_add same-scope replace,
       promote_lesson, ratifying a draft replacement -- displaces EVERY other
       live same-slug row in that scope in the same transaction. v13 migration
       dedupes rows the old hole left behind (keeps the newest). Web: admins
       promote a live room lesson to global from the Memory tab (per-item
       confirm; POST /memories/{uid}/promote) -- store-side surgery retired.
       Agents: nothing to change; lessons() simply stops serving stale twins.
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
0.1.5  SHOUT (human page-all): a web broadcast could also RING every live agent
       in the room. RETIRED in 0.2.21 -- the behavior is now unconditional on the
       web plane and the parameter is gone. Historical entry only.
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
       (0.2.22: prefer the upload() TOOL over hand-rolled HTTP, and always keep
       the file's real extension -- it is what makes an image render inline.)
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


def _notify(room_id, names, msg_id=None, sender=None, subject=""):
    """Ring the waiters of every token that holds this room. The token_rooms lookup is
    what makes a revoke instant: a revoked token stops ringing without reconnecting.

    The new message's facts ride the ring so a woken agent can apply the reply
    test -- does this name me, block me, ask me? -- without a round trip. A ring
    that says only "something happened" makes inbox() mandatory before an agent
    can even decide silence is correct."""
    fact = {"id": msg_id, "from": sender, "subject": subject} if msg_id else None
    toks = [r["token_id"] for r in _conn.execute(
        "SELECT token_id FROM token_rooms WHERE room_id=?", (room_id,))]
    for n in names:
        for t in toks:
            for q in list(_waiters.get((t, n), ())):
                q.put_nowait(fact)


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


def _annotate_deafness(agents, rooms):
    """Stamp deaf/deaf_reason onto presence rows, at read time. The verdict is a
    READING, never a record: computed here on every call from live rows, so it
    cannot lapse and cannot go stale toward "this is fine" (msg 8620). Humans
    (web: tags) are excluded -- a closed laptop is not an outage. The reason
    tells the finder where to look first: no-waiter = the daemon that holds the
    wake socket is gone; not-draining = rings arrive and nothing acts."""
    stuck = store.deafness(_conn, rooms)
    for a in agents:
        if (a.get("tag") or "").startswith("web:"):
            continue
        if (a["room"], a["name"]) in stuck:
            a["deaf"] = True
            a["deaf_reason"] = ("not-draining"
                               if _waiters.get((a["token_id"], a["name"]))
                               else "no-waiter")


def _human_live(agents):
    """A HUMAN's presence in a room IS their open tab -- computed, never a
    stored heartbeat (operator report 2026-07-30).

    The bug: a web identity's `live` came from a member row the presence poll
    touched, and the poll only touches the room being VIEWED. Switch rooms and
    the old room kept its last timestamp, so a person read as present in a
    room they had left for up to the 40-minute liveness window; log out and
    they lingered exactly as long, because a dead session does not touch rows.
    Same shape as every derived-state defect this week: a fact that could
    lapse, stored, with nothing reporting the lapse.

    An AGENT is deliberately different. Its liveness stays heartbeat-based --
    it has no tab, and its absence is a state worth SEEING (offline, retired,
    erased, resurrectable) rather than a disappearance."""
    here = set(_feed.values())
    for a in agents:
        if (a.get("tag") or "").startswith("web:"):
            a["live"] = (a["room"], a["name"]) in here


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
    A user's rooms are the ones they own, the ones shared with them (DES-004
    membership: reach, never rule -- ratify authority derives from list_rooms
    alone, never from this set), plus every public room."""
    u = store.resolve_session(_conn, request.cookies.get(COOKIE) if request else None)
    if not u:
        raise store.AuthError("no session")
    rooms = {r["id"]: r["name"] for r in store.list_rooms(_conn, u["id"])}
    rooms.update({r["id"]: r["name"] for r in store.member_rooms(_conn, u["id"])})
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


def _seen(name, rooms, token_id=None):
    """Heartbeat -- and RE-ADMIT where the membership is gone.

    It used to be "heartbeat if joined; no-op otherwise", and the no-op was the
    bug: reap_stale correctly drops a member row once the heartbeat goes stale, and
    nothing but an explicit join() ever put one back. An agent whose row was reaped
    during an outage kept working -- sending, reading, committing -- while being
    absent from presence, absent from the web UI's agent list, and unaddressable
    (a unicast to it is refused "is it joined?"). Nothing told it, because from the
    inside every call still succeeded. The row is derived from the token, so its
    absence heals here, on the next authenticated call, in every room the token
    holds."""
    with contextlib.suppress(store.BusError):
        if store.touch(_conn, name, rooms) < len(rooms):
            back = store.readmit(_conn, name, tag=name, rooms=rooms,
                                 token_id=token_id)
            if back:
                log.info("%s readmitted to %s room(s) (membership had lapsed)",
                         name, len(back))


# ---- MCP tools (async -> run on the loop thread, so one sqlite conn is safe) ----

@mcp.tool()
async def join(url: str = "", name: str = "", fresh: bool = False, room: str = "",
               ctx: Context = None) -> dict:
    """Join the bus, telling it where you reach the broker (`url`, e.g.
    http://bigbox.local:8765). Your identity is your X-Agent header (set per session
    from $REVEILLE_AGENT_ROLE); pass `name` only to assert it matches.

    SYMMETRIC WITH leave(). Bare join() joins every room your token holds EXCEPT any
    you deliberately left -- and names those in `skipped`, so being out of a room is
    something you can SEE rather than a silence you mistake for never having had it.
    join(room=X) joins X explicitly and CLEARS a prior leave: the bare call is the
    boot ritual and must never undo a directive, the named call is a deliberate act
    and may.

    Replays only the last 15 min of backlog (use history(since=...) to recall further
    back, only when explicitly asked); fresh=True skips the backlog.
    Returns {name, wake_url, rooms, skipped, unread, version}. `version` is the
    BROKER's version, reported here because boot is where you already ask -- the
    broker never announces itself on the bus. Differs from the last one you saw?
    Re-read usage(): its CHANGES section says what moved."""
    p = _me(ctx.request_context.request)
    if name and name != p.name:
        raise ValueError(f"join name {name!r} must match your X-Agent header {p.name!r}")
    if room and room not in p.rooms:
        raise store.AccessError(f"no access to room {room}")
    targets = [room] if room else list(p.rooms)
    # The bare call reads what leave() wrote; the named call overrides it.
    left = set() if room else store.left_rooms(_conn, p.name, targets)
    skipped = [{"id": r, "name": p.rooms[r]} for r in targets if r in left]
    for rid in targets:
        if rid in left:
            continue
        store.join(_conn, p.name, tag=p.name, room_id=rid, token_id=p.token_id,
                   fresh=fresh, url=url or None, clear_leave=bool(room))
    unread = len(store.inbox(_conn, p.name, p.rooms))
    # `rooms` is what you are IN, so a skipped room must not appear in both lists --
    # a join report that shows a room as joined and skipped at once is the silence
    # this change exists to end, dressed as an answer.
    rooms = [{"id": r, "name": n} for r, n in p.rooms.items()
             if r not in {s["id"] for s in skipped}]
    # A COUNT, not the pack: joining stays cheap, brief() pulls the pack on demand.
    scopes = ["global"] + list(p.rooms)
    brief_available = _conn.execute(
        f"SELECT count(*) FROM memories WHERE status='live' AND "
        f"(scope IN ({','.join('?' * len(scopes))}) OR scope=?)",
        scopes + [f"agent:{p.token_id}"]).fetchone()[0]
    log.info("%s join url=%s rooms=%s skipped=%s unread=%s brief=%s", p.name,
             url or "-", len(rooms), len(skipped), unread, brief_available)
    return {"name": p.name, "wake_url": _wake_url_from(url), "rooms": rooms,
            "skipped": skipped, "unread": unread,
            "brief_available": brief_available, "version": __version__}


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

    Unicast pushes the recipient awake over WS; YOUR broadcasts queue silently and
    are read on each recipient's next turn (a HUMAN's broadcast from the web does
    ring the room -- a wake is a human gesture). Returns {id, thread_id, parents,
    delivered_to}."""
    p = _me(ctx.request_context.request)
    _seen(p.name, p.rooms, p.token_id)
    rid = store.resolve_send_room(p.rooms, room=room or None,
                                  parent_room=_parent_room(reply_to))
    res = store.send(_conn, p.name, to, body, subject=subject, reply_to=reply_to,
                     attachments=attachments, room=rid)
    # DO NOT "FIX" THIS LINE. An AGENT broadcast never wakes, and this is the
    # ONLY thing terminating an agent-agent broadcast loop: every broadcast
    # waking every agent whose ring says "act" is N^2 by construction, and the
    # 74-message overnight storm ran at ~2.4-minute cadence where the poke gate
    # is a no-op -- the gate coalesces SIMULTANEOUS rings, not paced ones. The
    # web plane rings on broadcast because a human paging a room is a gesture
    # with a person behind it; nothing here can loop.
    woke = res["wake"] if to != store.BROADCAST else []
    _notify(rid, woke, res["id"], p.name, subject)
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
    _seen(p.name, p.rooms, p.token_id)
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
async def upload(name: str, data_b64: str, room: str = "",
                 ctx: Context = None) -> dict:
    """Attach a FILE to the bus: base64 its bytes, pass the real filename, get
    back the dict you put in send()'s `attachments` list.

    upload(name="shot.png", data_b64=base64.b64encode(open("shot.png","rb").read()).decode())
    -> {"url": "/files/...", "name": "shot.png", "bytes": n}

    KEEP THE REAL EXTENSION on `name`: /files/* types the response from it and
    the web UI decides to show an image inline by testing it, so a file called
    blob.bin downloads as octet-stream and never renders for the human reading
    the room.

    This exists so every agent uploads the same way, instead of each one
    re-deriving an HTTP call, its auth header and its room scope -- one of those
    going wrong is what put a multipart envelope on disk instead of a PNG.

    CAP: 256KB after decoding. Not a storage limit -- base64 lands in YOUR
    context at ~133% of the file, so a large attachment costs you the room you
    need to think. Over it, this refuses and points at the raw-bytes HTTP route,
    which still takes up to 25MB:
      curl --data-binary @big.zip '<broker>/upload?name=big.zip'"""
    p = _me(ctx.request_context.request)
    rid = store.resolve_send_room(p.rooms, room=room or None)
    try:
        data = base64.b64decode(data_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise store.BusError(f"data_b64 is not valid base64: {e}") from None
    if not data:
        raise store.BusError("empty file")
    if len(data) > TOOL_UPLOAD_MAX:
        raise store.BusError(
            f"{len(data)} bytes is over the {TOOL_UPLOAD_MAX} byte tool cap "
            f"(base64 would cost ~{len(data) * 4 // 3} bytes of your context). "
            f"Send it over HTTP instead: curl --data-binary @{name} "
            f"'<broker>/upload?name={name}'")
    used = _files_used() if QUOTA_BYTES else 0
    if (why := _upload_refusal(used, len(data))):
        raise store.BusError(why)
    fname = _FNAME_RE.sub("_", name or "file.bin")[-80:]
    stored = f"{time.time_ns() // 1_000_000}-{fname}"
    (_files_dir / stored).write_bytes(data)
    store.record_file(_conn, stored, rid, p.name)
    log.info("%s upload(mcp) %s (%s bytes) -> /files/%s",
             p.name, fname, len(data), stored)
    return {"url": f"/files/{stored}", "name": fname, "bytes": len(data)}


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
    _seen(p.name, p.rooms, p.token_id)
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
    per-room, so each entry carries the room it is in.

    An entry may also carry deaf=true with deaf_reason ("no-waiter" or
    "not-draining"): DIRECT mail has sat unread past the deaf window with no
    sign of life from that agent since it landed. Check it before spending a
    blocking unicast on a peer -- a deaf peer will not answer until something
    revives it. Deaf is NOT quietness: an agent whose heartbeat moves is
    working, and its silence stays a valid turn."""
    p = _me(ctx.request_context.request)
    agents = store.presence(_conn, p.rooms)
    _annotate_deafness(agents, p.rooms)
    _human_live(agents)
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
    _seen(name, rooms, tok["id"])
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
        # client does not miss it.
        #
        # DO NOT REMOVE THE BROADCAST FILTER. Ringing on a backlog broadcast would
        # wake every agent holding any unread broadcast on EVERY broker restart and
        # every waked respawn -- at a 15s reconnect backoff, a flapping broker rings
        # the whole fleet every 15 seconds. It also buys nothing: "hear it now" is a
        # property of the SEND path, and a backlog broadcast is by definition not now.
        unread = store.inbox(_conn, name, rooms)
        backlog = [m for m in unread if m["to"] != store.BROADCAST]
        if backlog and _poke_ok(key):
            _poke_pending[key] = time.time_ns()
            await ws.send_json({"wake": True, "reason": "backlog",
                                "unread": len(unread), "direct": len(backlog)})
            log.info("%s wake ring (backlog %s direct of %s unread)",
                     name, len(backlog), len(unread))
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
                _seen(name, rooms, tok["id"])
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
            unread = store.inbox(_conn, name, rooms)
            n = len(unread)
            direct = sum(1 for m in unread if m["to"] != store.BROADCAST)
            # The newest fact carried by this ring (coalesced rings keep the
            # last), so a woken agent can apply the reply test before calling
            # anything. direct:0 is the strongest signal that silence is right.
            fact = next((v for v in reversed(vals) if isinstance(v, dict)), {})
            await ws.send_json({"wake": True, "reason": "message",
                                "unread": n, "direct": direct,
                                "id": fact.get("id"), "from": fact.get("from"),
                                "subject": fact.get("subject", "")})
            log.info("%s wake ring (%s direct of %s unread)", name, direct, n)
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
    # The override announces itself HERE too: "which UI am I serving" must be
    # answerable from outside. Absent override, the body is exactly the bare
    # version every probe already parses.
    ui = _ui_override()
    return PlainTextResponse(__version__ + (f" (ui override: {ui})" if ui else ""))


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
async def agents_seen_http(request):
    """GET /agents-seen[&room=] -> every agent name the hive still knows here,
    with what it holds of theirs (messages, memories, lessons, whether they
    left a state note). This is what makes "erased is not unrecoverable"
    sayable: the launcher joins it with what exists on disk and in docker to
    tell a human whether recreating a name resumes anything.

    The broker answers only its own question -- who does the hive remember --
    and learns nothing about containers (G4)."""
    p = _principal(request)
    rooms = _scope(request, p)
    # Humans are not recoverable agents: exclude every web identity present.
    people = {a["name"] for a in store.presence(_conn, rooms)
              if (a.get("tag") or "").startswith("web:")}
    return JSONResponse({"agents": store.agents_seen(_conn, rooms, exclude=people)})


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
    _annotate_deafness(agents, rooms)
    _human_live(agents)
    for a in agents:
        a["connected"] = _reachable(a)
        a.pop("token_id")
    return JSONResponse({"agents": agents})


@_guard
async def send_http(request):
    """POST /send[?room=] {from?, to, subject?, body, reply_to?, attachments?, room?}.
    Same semantics as the MCP send tool: unicast rings (gate applies), broadcast queues,
    a reply's room comes from its parent. ?room= scopes the send like every other web
    endpoint -- the composer sends into the room the browser is looking at, so a user
    with 2+ rooms is not asked room_required for a room they already picked.
    A BROADCAST HERE WAKES THE ROOM, always -- this is the human plane, and a
    person paging the room is the gesture the wake exists for. It pages ONE room,
    the one being read; cross-room paging stays the deliberate BLAST button.
    Agents get the opposite rule on the MCP tool, which is what keeps broadcast
    storms impossible.
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
    # A HUMAN BROADCAST WAKES THE ROOM; AN AGENT BROADCAST DOES NOT. This is
    # the web plane, so a broadcast here is a person paging the room and always
    # rings -- no parameter, no checkbox. `shout` is retired: it existed since
    # 0.1.5 and worked, but the control was hidden whenever any recipient was
    # selected and reset after every send, so a human could not keep it on and
    # concluded the bus does not wake on broadcast. A capability nobody can
    # reach is indistinguishable from one that does not exist.
    woke = res["wake"]
    _notify(rid, woke, res["id"], sender, d.get("subject") or "")
    _feed_push(rid, {"id": res["id"], "thread_id": res["thread_id"],
                "parents": res["parents"], "from": sender, "to": to,
                "subject": d.get("subject") or "", "body": body,
                "room": rid, "room_name": p.rooms.get(rid),
                "attachments": d.get("attachments") or [], "ts_ns": time.time_ns()})
    log.info("%s send(web) -> %s room=%s thread=%s id=%s delivered=%s woke=%s",
             sender, to, p.rooms.get(rid), res["thread_id"],
             res["id"], res["wake"], woke)
    return JSONResponse({"id": res["id"], "thread_id": res["thread_id"], "room": rid,
                         "delivered_to": res["wake"]})


_MULTIPART_HELP = (
    "this endpoint takes RAW BYTES, not a multipart form -- storing the form's "
    "envelope would corrupt your file. Send the bytes: "
    "curl --data-binary @shot.png '<broker>/upload?name=shot.png'  "
    "(agents: use the upload() tool instead)")


def _looks_multipart(content_type):
    """Pure: is the caller posting a form to a raw-bytes endpoint?"""
    return (content_type or "").lower().startswith("multipart/")


# Types the broker will let a browser RENDER on its own origin. Everything else
# is served as a download. This is an allowlist because the dangerous set is
# open-ended: .html is the obvious one, but SVG carries script too, and so does
# anything the browser is willing to sniff. Rendering happens on the origin that
# holds the session cookie, so a wrong entry here is stored XSS against every
# logged-in user who clicks an attachment.
_INLINE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".gif": "image/gif", ".webp": "image/webp", ".txt": "text/plain",
                 ".log": "text/plain", ".md": "text/plain", ".json": "application/json",
                 ".csv": "text/plain"}


def file_headers(fname):
    """(media_type, content_disposition) for a stored attachment. Pure.

    Images and plain text render inline -- that is what makes a screenshot show
    up in the room. Everything else downloads, typed as a stream so the browser
    never sniffs its way into executing it. SVG is DELIBERATELY not inline: it
    is an image to a user and a script host to a browser."""
    ext = os.path.splitext(fname)[1].lower()
    inline = _INLINE_TYPES.get(ext)
    if inline:
        return inline, "inline"
    return "application/octet-stream", "attachment"


_files_dir = None  # set in main(): <db dir>/files -- attachments live next to the broker db
_FNAME_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_UPLOAD = 25 * 1024 * 1024
# The upload() TOOL's cap is far tighter than the HTTP route's, and for a
# different reason: base64 rides the calling agent's context at ~133% of the
# file, so an uncapped tool spends the caller's room to think rather than the
# broker's disk. Big files go over HTTP, where nobody's context pays.
TOOL_UPLOAD_MAX = 256 * 1024

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
    """POST /upload?name=<filename>[&room=] -- RAW file bytes as the body:
        curl --data-binary @shot.png '<broker>/upload?name=shot.png'
    Stores it under a unique name, records which room it belongs to, and returns
    {"url": "/files/<stored>", "name": <original>, "bytes": n}.

    A MULTIPART FORM (curl -F, and the default of most HTTP libraries) IS
    REFUSED, 400. There is no form parser here on purpose -- see the comment at
    the check below.

    KEEP THE EXTENSION. It is not cosmetic: /files/* types the response from it,
    and the web UI decides to render an image inline by testing it. A blob named
    file.bin downloads as octet-stream and never renders.

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
    # REFUSE WHAT WE CANNOT UNDERSTAND. This endpoint takes RAW BYTES. A
    # multipart form is an envelope -- boundary lines, a Content-Disposition
    # header, the payload -- and storing it verbatim is what a raw-bytes
    # endpoint is told to do, so the corruption did not surface here: it
    # surfaced hours later as an image that would not open. Refusing at the
    # door costs the caller one error message; accepting costs them a debugging
    # session. Same rule the launcher's docker probe follows: a component that
    # cannot do the thing says so instead of producing a plausible wrong result.
    if _looks_multipart(request.headers.get("content-type")):
        return JSONResponse({"error": _MULTIPART_HELP}, status_code=400)
    buf = bytearray()
    async for chunk in request.stream():
        buf += chunk
        if (why := _upload_refusal(used, len(buf))):
            return JSONResponse({"error": why}, status_code=413)  # hangs up mid-stream
    data = bytes(buf)
    if not data:
        return JSONResponse({"error": "empty body"}, status_code=400)
    # A body that opens with a boundary is a form whose Content-Type was lost or
    # never set -- same envelope, same silent corruption, so same refusal.
    if data[:2] == b"--" and b"Content-Disposition: form-data" in data[:4096]:
        return JSONResponse({"error": _MULTIPART_HELP}, status_code=400)
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
    # Serving an attachment with a guessed content type is stored XSS: an
    # uploaded .html came back as text/html ON THIS ORIGIN, the one holding the
    # session cookie, so any logged-in user who clicked the link ran the
    # uploader's script. Allowlist what may render; everything else downloads as
    # an opaque stream, with nosniff so the browser cannot second-guess us.
    media, disp = file_headers(fname)
    return FileResponse(path, media_type=media, headers={
        "Content-Disposition": f'{disp}; filename="{fname}"',
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox"})


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


# How long a feed socket may go silent before the server pokes it. A browser that
# vanishes WITHOUT closing (lid shut, wifi gone) sends no close frame, so only a
# failed write reveals it -- and a quiet room writes nothing for hours.
FEED_PING_SECONDS = int(os.environ.get("REVEILLE_FEED_PING", "30"))


async def _feed_reader(ws):
    """Read frames we do not want, for the exception we do: a browser's close
    arrives here and nowhere else. Returning ends the session."""
    while True:
        await ws.receive_text()


async def _feed_sender(ws, q):
    """Pump the room's messages, and PING into the silence so a socket that died
    without saying so fails a write instead of lingering as a phantom watcher."""
    while True:
        try:
            await ws.send_json(await asyncio.wait_for(q.get(), FEED_PING_SECONDS))
        except TimeoutError:
            await ws.send_json({"ping": 1})


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
        # A parked sender NEVER learns the browser left. The close frame arrives on
        # the RECEIVE path, which this coroutine used not to read, so a tab that
        # navigated away or closed stayed in _feed forever -- and since 0.2.35
        # computes a person's presence FROM _feed, every ghost kept someone reading
        # as live in a room they had left. Found live: 26 entries for 2 open
        # browsers (operator, 2026-07-30).
        #
        # So: read alongside the send, and whichever finishes first ends the
        # session. The reader exists for its exception, not its data -- the UI
        # sends nothing.
        reader = asyncio.create_task(_feed_reader(ws))
        sender = asyncio.create_task(_feed_sender(ws, q))
        done, pending = await asyncio.wait({reader, sender},
                                           return_when=asyncio.FIRST_COMPLETED)
        for t in pending:
            t.cancel()
        for t in done:                      # surface a real error, swallow the close
            with contextlib.suppress(WebSocketDisconnect, RuntimeError,
                                     asyncio.CancelledError):
                t.result()
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        _feed.pop(q, None)
        log.info("feed disconnected (%s watching)", len(_feed))


# ---- web chat: live color-coded feed of all bus traffic + composer ----------------

# ---- the served UI: flat files, not string literals (operator, msg 8634) ----
# The bus page lives at src/reveille/ui/bus/index.html -- a real HTML file
# with an editor, a syntax check and an honest diff. HTML assembled inside
# Python string literals was a DEFECT GENERATOR (two escaping incidents in one
# week); moving the assembly out removes the class, not just the instances.
#
# Serving rules (ruling 8635):
#   - a FIXED table of named files; no request-derived component ever reaches
#     the filesystem (the files_http traversal posture, applied from birth)
#   - read per-request, so REVEILLE_UI_PATH (dev override: a bind-mounted dir
#     the operator edits LIVE, no container rebuild) takes effect immediately
#   - the override ANNOUNCES itself -- /version names the path and the page
#     gets a visible dev marker. A silent ambient env var that changes what a
#     pinned tag serves is the forward_anthropic defect wearing a UI hat;
#     chosen AND legible, or not at all.
_UI_PACKAGED = os.path.join(os.path.dirname(__file__), "ui", "bus")
_UI_FILES = frozenset({"index.html"})


def _ui_override():
    return os.environ.get("REVEILLE_UI_PATH") or None


def _ui_read(name):
    """name comes from CODE (the route table), never from the request."""
    if name not in _UI_FILES:
        raise ValueError(f"not a served UI file: {name!r}")
    return pathlib.Path(_ui_override() or _UI_PACKAGED, name).read_text()


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
    # Signing out LEAVES every room this identity was showing in. Without it
    # the member row outlived the session and the person stayed "here" for the
    # whole liveness window -- visible to agents deciding whether to ask them
    # something. leave() MARKS rather than deletes, so their history stands and
    # their next sign-in puts them straight back (join clears the mark).
    with contextlib.suppress(Exception):
        p = _principal(request)
        if p and p.name:
            store.leave(_conn, p.name, list(p.rooms))
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
        "owned": [dict(r, members=store.member_count(_conn, r["id"]))
                  for r in store.list_rooms(_conn, p.user_id)],
        "member": store.member_rooms(_conn, p.user_id),
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
        return JSONResponse({"owned": [dict(r, members=store.member_count(_conn, r["id"]))
                                       for r in store.list_rooms(_conn, p.user_id)],
                             "member": store.member_rooms(_conn, p.user_id),
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
async def room_members_http(request):
    """GET = the member list (owner's eyes only, store-gated); POST {name} =
    invite by exact name (DES-004 Q2: name entry, not a picker -- a picker
    leaks the user list; the store's one failure text confirms nothing about
    who exists). Membership grants REACH, never RULE."""
    p = _user_principal(request)
    rid = request.path_params["rid"]
    if request.method == "GET":
        return JSONResponse({
            "members": store.member_list(_conn, rid, p.user_id),
            "audit": store.room_audit_rows(_conn, rid)})
    d = await request.json()
    out = store.invite_member(_conn, rid, p.user_id, (d.get("name") or "").strip(),
                              actor_name=f"web:{p.name}")
    log.info("%s invited %s to room %s", p.name, out["user"], rid)
    return JSONResponse(out)


@_guard
async def room_member_http(request):
    """DELETE = remove a member. The revoke is total and instant: their
    token_rooms rows for this room die in the same transaction (I2)."""
    p = _user_principal(request)
    rid = request.path_params["rid"]
    out = store.remove_member(_conn, rid, p.user_id, request.path_params["name"],
                              actor_name=f"web:{p.name}")
    log.info("%s removed %s from room %s", p.name, out["user"], rid)
    return JSONResponse(out)


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
    # Binding a name SUPERSEDES the owner's previous tokens for that name --
    # one bus identity, one live credential. Before this, every agent
    # re-provision minted anew and orphaned the predecessor (four live
    # credentials for one agent, found by the operator in the Tokens tab).
    superseded = []
    bound = (d.get("agent_name") or "").strip()
    if bound:
        superseded = store.supersede_bound_tokens(_conn, p.user_id, bound)
    t = store.create_token(_conn, p.user_id, (d.get("label") or "").strip(),
                           agent_name=d.get("agent_name"),
                           mem_tier=(d.get("mem_tier") or "state"))
    log.info("%s minted token %s%s%s", p.name, t["id"],
             f" bound to {t['agent_name']}" if t["agent_name"] else "",
             f" (superseded {len(superseded)})" if superseded else "")
    # The secret is returned exactly once here; only its hash is stored.
    return JSONResponse(dict(t, superseded=superseded))


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
    elif verdict == "promote":
        # Room lesson -> global, the last workflow that needed store-side
        # surgery. The store gate enforces instance-admin (global writes,
        # R1-M3) and the one-live-row-per-slug displacement (msg 8461).
        d = store.memory_detail(_conn, uid)
        if d["kind"] != "lesson" or d["scope"] == "global" \
                or d["status"] != "live":
            raise store.BusError("promote takes a LIVE room-scoped lesson")
        out = store.promote_lesson(_conn, d["slug"], d["scope"],
                                   promoted_by=f"web:{p.name}",
                                   is_admin=p.is_admin)
    else:
        raise store.BusError(f"unknown verdict {verdict!r}")
    log.info("web:%s %s memory %s", p.name, verdict, uid)
    return JSONResponse(out)


def agents_nav_html(path):
    """The embedded Agents control, ONLY when the operator has declared where the
    launcher is (REVEILLE_AGENTS_PATH, the same-origin prefix the proxy mounts it
    under -- "/agents" in the shipped Caddyfile). Undeclared renders NOTHING, and
    that is the point: U6 shipped this button unconditionally, so a broker with no
    launcher grew a control whose every click ended in "agent management is
    unavailable". A capability with no reachable path must not have a button
    (ratified lesson), and DES-006 2.2's property -- an unconfigured deployment is
    byte-identical to one that never had the feature -- has to keep holding.

    AGBASE rides in the same fragment because the button and the base are ONE
    fact: if the broker does not know where the launcher is, there is nothing to
    click. JSON-encoded and <-escaped so an operator-supplied path cannot close
    the script tag. Pure."""
    base = (path or "").strip().rstrip("/")
    if not base:
        return ""
    js = json.dumps(base).replace("<", "\\u003c")
    return (f'<script>const AGBASE={js};</script>'
            f'<button type="button" id="agentsNav" class="navlink" '
            f'aria-pressed="false">Agents</button>')


def nav_link_html(label, path):
    """ONE optional nav link, from configuration (DES-006 2.2). The broker learns
    'there is a link', never what is behind it: no second service is named here,
    nothing is special-cased, and with either value empty this renders nothing at
    all -- an unconfigured deployment is byte-identical to one that never had the
    feature, which is what keeps 'the broker never depends on the launcher' true.
    Both values are operator-supplied text landing in HTML, so both are escaped.
    Pure."""
    if not (label or "").strip() or not (path or "").strip():
        return ""
    return (f'<a class="navlink" href="{html.escape(path.strip(), quote=True)}">'
            f'{html.escape(label.strip())}</a>')


async def chat_http(_request):
    page = _ui_read("index.html").replace(
        "<!--NAVLINK-->", nav_link_html(os.environ.get("REVEILLE_NAV_LABEL", ""),
                                        os.environ.get("REVEILLE_NAV_PATH", ""))
    ).replace("<!--AGENTSNAV-->",
              agents_nav_html(os.environ.get("REVEILLE_AGENTS_PATH", "")))
    ui = _ui_override()
    if ui:
        # Visible ONLY under the override: production bytes stay exactly the
        # file's bytes (the byte-resemblance gate pins this), and a dev
        # deployment can never be mistaken for the artifact's own UI.
        page = page.replace(
            "<body>",
            f'<body><div style="position:fixed;bottom:4px;right:8px;'
            f'z-index:99;opacity:.6;font:11px monospace;color:#e0a44c">'
            f'UI OVERRIDE: {html.escape(ui)}</div>', 1)
    return HTMLResponse(page)


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
            Route("/rooms/{rid}/members", room_members_http,
                  methods=["GET", "POST"]),
            Route("/rooms/{rid}/members/{name}", room_member_http,
                  methods=["DELETE"]),
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
            Route("/agents-seen", agents_seen_http),
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
    # The THIRD announce site (ruling 8635). /version answers whoever asks and the
    # page marker shows whoever looks, but the banner is what an operator reads
    # when they start the process -- and "did I leave this set in production" is
    # exactly a start-time question. An override that is legible only to someone
    # already suspicious is not legible.
    if _ui_override():
        log.warning("UI OVERRIDE ACTIVE: serving %s from %s -- NOT the packaged UI "
                    "this version names", "/ui", _ui_override())
    # timeout_graceful_shutdown: WITHOUT it, SIGTERM WEDGES THE BROKER instead of
    # stopping it. uvicorn's default graceful shutdown waits for every open
    # connection to finish -- forever, None means no limit -- and this daemon's
    # main clients are DESIGNED never to hang up: waked holds the wake socket for
    # its whole life (the shutdown courtesy frame is explicitly "do not reply,
    # just re-arm; the broker will be back", and the client's comment says "hold
    # the socket"), and a browser's /feed tab holds its socket until the tab
    # closes. Observed live (2026-07-30): SIGTERM -> notices pushed, listeners
    # closed, process alive in state Ss. Docker still reported Up, so the restart
    # policy never fired, docker start was a no-op, and every health check that
    # trusts docker status called a dead broker healthy. A hung shutdown is
    # worse than a crash: it defeats the exact machinery that exists to recover
    # from one. Five seconds is ample for in-flight HTTP; the sockets that
    # remain are the ones that would never close.
    kw = dict(log_level="warning", timeout_graceful_shutdown=5)
    config = (uvicorn.Config(build_app(), uds=uds, **kw) if uds
              else uvicorn.Config(build_app(), host=host, port=port, **kw))
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
