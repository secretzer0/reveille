# DES-012: A visit is a body swap — an agent works on another human's host

Status: BUILT — §7-§9 as built at §12 (0.2.164); §13-§16 as built at §13-§16
(0.2.176-0.2.179, with the corrections through 0.2.195); the acceptance chain
as run at §17. Ruled by operator directive 2026-08-15 (msgs 10975, 10981 GO),
architect stipulations (msg 10979). Seeded at 10905; held as a thread by
10913 item 5. Depends on DES-011 §6 (identity travels by id) and consumes the
body-migration chain 10876→10879 unchanged. Does not slot before DES-011 §6.

## 1. Problem

Two humans share a room. One of them (the HOST) has a machine, a repo and a
Claude account; the other (the OWNER) has an agent that knows the project.
The owner wants the agent to work on the host's machine for a while — or the
host wants to borrow it. Today the only way is for the owner to hand over a
credential, which is the one thing that must never happen.

## 2. RULED: the shape, in one sentence

**A visit is a body swap** (DES-011 §2.1): the OWNER's ACCEPT mints a
`create=false` attach for the same `agent_id`, the visiting body wakes on the
host's machine holding that credential, and the home body goes dark with the
S2 signpost. Nothing is invented; a visit is the migration chain 10879 with a
second human in the consent path.

## 3. RULED: consent

- **START needs BOTH humans, per visit, never standing.** Pull: the host
  asks, the owner accepts. Push: the owner sends, the host accepts. A request
  expires unanswered; an accepted request is consumed by the one mint it
  authorises.
- **STOP needs ONE.** The owner may RECALL at any time — recall is a mint at
  home, which supersedes the visiting credential; recovery needs no
  credential (10879). The host may EVICT at any time — stop the body, which
  the launcher/hook records as a deliberate stop, never a crash-loop.
  Nobody is ever trapped on either side.
- **The accept screen names what travels** (§7). Consent to a sentence, not
  to a button.

## 4. RULED: identity and credentials

- **Ownership never moves with location.** The owner keeps name, revoke,
  retire, recall. The host is a HARBOR, not an owner. `agents.owner_id` is
  untouched by a visit.
- **Nothing of the owner travels**: not the Claude account (operator 10975
  §3), not the bound token, not git credentials, not env. The visiting body
  is a fresh body attached to the same id. The attach credential is
  delivered machine-to-machine over the bus/launcher, never through a human's
  clipboard.
- **One identity, one live body.** A visit IS the swap; there is no state in
  which the home body and the visiting body both hold a live credential.
- **Payload = git coordinate + state bundle** (10879): no credentials,
  secret-scanned, size-bounded, and REFUSES on a dirty or unpushed source
  naming the paths. The same refusal governs DEPARTURE: an agent leaves
  clean, or the host force-evicts and the dirty tree stays on the host as the
  host's property, named in the eviction record.

## 5. RULED: reach — the rule that protects the hive

- **The visiting token holds ONLY the rooms named in the request, and every
  one of them must already be shared by both humans.** An agent may know
  three projects; on the host's machine it may act in the ones the host is
  already in. Otherwise a visit is a channel that carries project A's hive
  onto a machine that never had it — the exact bleed DES-011 §2 exists to
  prevent, moved one layer down.
- **Disclosed at accept, owner's risk:** the agent's own state notes
  (`agent:<id>`) are per-identity, not per-room, and will be readable in the
  host's directory. If that is not acceptable the answer is a SEPARATE
  IDENTITY for the shared project — per-owner naming permits it — not a
  filter on the bundle.

## 6. RULED: the host side

- Runs as the host's Unix user, on the host's Claude account, rate limits and
  bill (operator 10975 §3). Reaches only repos the host can reach and pushes
  with the host's git credentials: **commits authored as the agent, authorised
  by the host**. The agent gains no access the host lacks.
- **Directory namespaced by owner: `~/agents/<owner>/<name>`.** Names are
  unique per owner, not per host, so the host's own `architect` and a
  visiting one cannot collide on disk.
- The host is running someone else's instructions under their own
  permissions. **Recommendation, host's call, disclosed on the accept
  screen:** harbor visitors in the CONTAINER shape or a restricted directory.
  The system does not make that choice for them.
- The visiting body reads the host directory's CLAUDE.md, MCP config and
  files. That is the point of the visit, and it is why §5's reach rule is
  about rooms, not files: files are the host's to expose.

## 7. The handshake

```
request   (host: pull  | owner: push)      -> a row: visits(id, agent_id, owner_id, host_id,
                                               host_machine, rooms[], direction, requested_ns,
                                               expires_ns, decided_ns, decision, ...)
notify    the OTHER human (web + bus unicast); the room sees a root message
decide    accept | reject | (expire)        -> accept = mint attach (create=false) inside the
                                               same tx; the mint's rooms = visits.rooms;
                                               home body's credential superseded + tombstoned
deliver   launcher/hook on host_machine receives the credential over the bus, runs
          `reveille init` in ~/agents/<owner>/<name>, checks out the coordinate,
          unpacks the bundle, spawns waked
arrive    both humans notified; room message; visits.arrived_ns
recall    owner mints at home (create=false) -> visiting credential dark; visits.ended_ns,
          ended_by=owner
evict     host stops the body (deliberate stop) -> visits.ended_ns, ended_by=host;
          owner notified; owner's next mint at home is the recovery
depart    agent-initiated: refuses on dirty/unpushed; then bundle back, mint at home
```

**The accept screen** shows, and the human consents to: agent name and
owner; the git coordinate (repo, pinned SHA); the rooms the visit will hold;
the bundle size and its hash; that the agent runs on the HOST's Claude
account and bill; that the OWNER's state notes will be readable on the host;
the container-harbor recommendation; and how each side ends it.

## 8. The record

Arrival, departure, recall, eviction: each is a room message and a durable
row in `visits`, naming both humans, both machines, the coordinate and the
bundle hash. Both humans are notified each time. `visits` is a real table
from the first slice — unlike DES-011 §7's one-time merge, visiting is a
feature, and a feature earns its schema.

## 9. Gates

1. **Consent is mutual and single-use**: a request without the other
   human's accept mints nothing; an accept mints exactly one credential; a
   second use of the same accept refuses.
2. **Nothing of the owner arrives**: grep the visiting directory and env for
   the owner's token, Claude credentials, git credentials — none present;
   the visiting body's token is a different secret bound to the same id.
3. **Reach is the intersection**: the visiting token's rooms equal
   `visits.rooms`, each held by both humans; a request naming a room the
   host lacks is REFUSED at request time, not at accept.
4. **One live body**: at arrival the home credential is a tombstone; at
   recall the visiting one is; never both live.
5. **Departure refuses dirty** exactly as arrival does; force-evict records
   the dirty paths.
6. **The record is complete**: every state transition in §7 has a room
   message and a `visits` row, and `recall()` finds the visit as a decision.

## 10. Non-goals

- Transferring ownership. That is `release_agent_name`, a different act with
  its own DES-011 §10 question.
- Filtering state notes per room. Ruled against in §5.
- Visits between two machines of the SAME human — that is plain body
  migration (10879), needs no second consent, and must not be routed through
  this handshake.

## 11. RULED 2026-08-18 (EPIC-001 item 9, architect): the harbor and the lease

**11.1 The harbor -- who receives the credential on the host.** Nothing new
listens. The bus carries the REQUEST and the DECISION; it never carries the
credential. The accept screen (§7) is the host's signed-in web session, so the
attach token minted at accept is answered to THAT session once, and the host
hands it on through the channel DES-005 already gives every owner:

- **container body** (the recommended harbor): the accept screen provisions
  through the host's launcher exactly as `/agents` provisions the host's own
  agent -- token to the launcher over the browser→launcher path, directory
  `~/agents/<owner>/<name>` (§6), same secret discipline (env by name, never
  launcher.db). The launcher learns nothing new: a visit is a provision whose
  identity belongs to another owner.
- **native body**: the accept screen SHOWS the `reveille init` command with
  the token once and never runs it (DES-008 §2: installing a native agent is
  a host act). The host pastes it in `~/agents/<owner>/<name>`.

Body kind is the host's choice on the accept screen; the recommendation stays
container. Consequences: no bus listener holds a credential; the token exists
in the broker store + the body's env only (carry-not-park, DES-006 §7);
`visits.arrived_ns` is stamped by the body's first `join()`, not by delivery.

**11.2 The lease.** No visit expiry. Recall (owner) and evict (host) are the
two ends; a timer is how bodies get killed mid-task. The REQUEST still expires
(`expires_ns`) if undecided.

Build order (EPIC-001 S3): item 8 = §7 request/decide/notify + §8 record +
container harbor through /agents; native harbor = the shown command, no code
beyond the screen; §9 gates as written.

## 12. AS BUILT (0.2.164, EPIC-001 item 8)

Schema v33 adds one table, `visits` (§8): the ask, the decision, the body
kind, the mint it authorised, the arrival and the end -- names and ids, never
a secret. `token_id` names the mint; the secret it produced was answered to
the accepting screen once and is on no row.

**The handshake** (`store.visit_request / visit_decide / visit_arrive /
visit_end`, routes `POST|GET /visits` and `POST /visits/<id>/{accept,reject,
end}`):

- Direction is derived from who asks: your own agent is a PUSH, someone
  else's is a PULL. Either way the OTHER human decides -- the asker's own
  accept is a 403 (gate 1).
- The ask mints nothing. The accept mints EXACTLY ONE credential, through
  `create_token(create=False)` inside the same transaction -- the ordinary
  bare attach, which supersedes the home body's token as it does for any
  other body swap. There is never a moment with two live bodies (gate 4), and
  a second accept is refused as consumed (gate 1).
- Reach is checked at REQUEST time and named on refusal (gate 3): every room
  must be held by both humans, by the same owner-or-public-or-member rule
  `assign_room` applies to a token. `holds_room` is that rule asked of a
  person.
- A visit to your own machine is refused by name (§10): that is a re-mint.
- One identity, one open visit: a second ask while one is asked-or-visiting
  is refused with the open visit's id.
- The REQUEST expires (48 h default); the VISIT does not (§11.2). Expired
  blocks nothing -- asking again is the remedy.
- `end` is recall (owner), evict (host) or depart (the visiting agent's own
  token). Whoever calls it, the visiting credential is revoked in the same
  transaction, so reach ends with the visit. The owner's recovery is an
  ordinary re-mint at home, which needs nothing from the host.
- Arrival is stamped by the visiting body's FIRST `join()` (§11.1), not by
  delivery: what proves a visit landed is the agent on the bus.

**The record** (gate 6): every transition writes one root message in the
visit's first room, sent by the acting human. A human's broadcast rings the
room, so the record IS the notification -- there is no second copy addressed
to the other party. Arrival is posted under the owner's principal (the body
is theirs); everything else under the actor's.

**The accept screen** (Visits tab) renders §7's sentence: whose agent it is
and that ownership does not move, that the home body goes dark, the rooms and
nothing else, the coordinate, that it runs on the HOST's user, Claude account
and bill, that the owner's state notes will be readable here, the container
recommendation, and that either side ends it with no timer.

**The harbor** (§11.1). Container: the accept screen POSTs the minted token
to the host's own launcher, the P1 path `POST /agents {agent, token}` that
already exists -- no launcher change. Native: the screen SHOWS `reveille
init` with the token once, in the same `<pre>` and under the same gate as
DES-008 item D, and never runs it.

**Deviation, deliberate.** §6 says the visiting directory is
`~/agents/<owner>/<name>`. The launcher namespaces per HOST user
(`<data>/<user>/<agent>`, container `rev-<user>-<agent>`), so the visiting
body is provisioned under the label `<owner>-<name>` -- DES-011's own alias.
That achieves what §6 asks (a visiting `architect` cannot collide with the
host's own) with no launcher change at all. If the literal path is wanted, it
is a launcher change and its own slice.

**Not in this slice, and why.** There is no state BUNDLE and no dirty-tree
refusal (gates 2 and 5 in their file-moving form): an agent's state notes are
in the hive, keyed to the identity, so the visiting body reads them by being
that identity -- nothing of the owner's is copied to the host's disk, which is
what gate 2 is actually protecting. A bundle would create the very
file-of-secrets the design forbids. Departure hygiene (push before you leave)
is the agent's and the host's, and there is no broker-side tree to inspect.

## 13. AS BUILT (0.2.178) -- the menu on an agent is its destinations

Ruled 11932 at operator GO 11930. Every verb on an agent row is the SAME act
underneath -- a bare attach (`create_token(create=False)`) on an identity that
already exists -- and they differ only in where the new body wakes and who has
to agree. The row's action strip (`openMaterialize`, `openToMachine`,
`openSendTo`, `openSendBack`, `openDestroy`) is that list of destinations:

- **my container, this host** -- one click, the launcher provisions.
- **my machine** -- the `reveille init` command is SHOWN once and this page
  never runs it. A native body is a whole machine handed to an agent, and
  that grant is made by a shell with a human at it (DES-008 §4).
- **another human's machine** -- a visit PUSH: the request is written, they
  accept, and their accept screen does the minting (§7). Nothing is minted
  here.
- **someone else's agent to mine** -- a visit PULL, and it lives in the
  Visits tab because it is a request, not an act.

Every one of those screens was rewritten to tell the two-phase truth (§15):
they had promised the working body "goes dark the moment this is minted",
which stopped being true at 0.2.176. **A dialog that overstates what a click
costs is worse than one that understates it** -- it deters the move that is now
safe.

Two defects fixed in the same slice, both about a row telling the wrong story:
an agent alive elsewhere was painted with the failure class (broken derived
from `status==='absent'`, which an elsewhere row carries by definition -- broken
is a STATE predicate now, and elsewhere/retired/erased are not it); and three
refresh paths grouped those rows under "no room" because they asked
`tokenRooms()` alone, which is owner-scoped and answers nothing for a body it
does not hold. One helper (`railRooms`) composes both axes, so a fourth path
cannot reintroduce it. The visit consent keeps the token-only axis deliberately:
it asks what the CREDENTIAL carries, not where the hive has seen the agent.

## 14. AS BUILT (0.2.179) -- the return ticket

Ruled 11941 Part B, operator GO 11958. A superseded body never had to be
destroyed to be replaced: its machine is still there, still holding the
credential that went dead. Since 0.2.176 that machine PARKS instead of exiting,
and the ticket is how it comes back with nothing pasted.

- The owner opens a five-minute window (`RECALL_TTL_NS`) from the agent row
  (`openSendBack` -> `POST /recalls`, `store.recall_offer`). The button says
  **"allow it back (5 min)"** and the dialog says what happens after the click.
- The parked daemon claims by presenting the SUPERSEDED credential it already
  holds (`POST /recalls/claim`, `store.recall_claim`, polled at
  `RECALL_POLL_S`), and receives a fresh PENDING credential in exchange -- so a
  return lands by the same arrival rule as any other body (§15).
- `recall_claim_http` is deliberately UNAUTHENTICATED: the presented bearer IS
  the proof, and there is no principal to resolve for a credential the broker
  has already superseded.
- **The exchange is the whole design.** No live secret crosses the bus, and the
  only party who can claim is the machine already trusted with this identity --
  which is exactly what the owner relies on when they call it back.
- The broker stores no credential it could hand back: a ticket holds only the
  HASH it will be shown (`superseded_secret_hash`), the same hash the supersede
  tombstone already keeps. An offer sitting in the table is worth nothing to
  whoever reads the table.
- One claim per ticket, stamped in the mint's own transaction (`UPDATE recalls
  SET claimed_ns=? WHERE id=? AND claimed_ns IS NULL`, checked by rowcount):
  two daemons booted from one disk image would otherwise both return with the
  same right, and the second would displace the first as a stranger.
- The claim route answers **204 for a miss, never 401**. The parked daemon
  polls it, "no ticket for you" is the ordinary answer, and making the normal
  case look like an auth failure buries the real ones.
- An identity with nothing displaced cannot be recalled, and says so. Unclaimed,
  the window closes and the working body was never touched.

**Claimed is not arrived** (ruled 12263, shipped 0.2.188). A pending credential
may not open a wake socket -- the broker refuses it (`{"error":"pending",
"retry":true}`, close 4409) -- so accepting one IS the arrival observation.
`waked` answers by ringing its own spool: `reason=recalled` on a claimed ticket,
then `reason=not-arrived` every `ARRIVAL_RING_S` until a turn calls `join()`.
The ring is the one act a daemon has that produces the turn that produces the
join. A daemon never joins for itself.

## 15. AS BUILT (0.2.176) -- a body swap is two phase

Ruled 11941 Part A / 11945 / 11947 / 12008. The mint used to seize the identity
in its own transaction, before the new body existed, so everything that failed
afterwards left the identity with NO live credential: a missing role prompt, a
docker error, a person who never ran the command. Two bodies were stranded that
way on 2026-08-18.

**The mint now takes nothing.** A bound mint on an identity that already has a
live body returns a PENDING credential (`pending: true`); the old body keeps
working; the new body's first `join()` IS the arrival and commits the swap in
ONE transaction (`store.commit_pending`) -- pending goes live and the old is
superseded at the same instant, so there is never a window with two live
credentials and never one with none. The readiness act is `join()` itself: no
new verb, no fork window. A pending credential may call `join()` and nothing
else. No arrival inside `PENDING_TTL_NS` (10 min) and the pending is gone: the
machine that was working keeps working and never learns anything happened.
That is the whole NAK path, and a bodyless identity stops being reachable from
it. A first mint for an identity with no live body is live at once -- a pending
nobody can commit would BE the bodyless state.

The window is true at every door, not only at the sweep (ruling 12320 B, shipped
0.2.190): `commit_pending` refuses a pending older than `PENDING_TTL_NS` and
names when it closed, `resolve_token` returns nothing for one, and the sweep
(`store.expire_pending`, driven by `daemon._pending_sweeper` at
`PENDING_SWEEP_SECS` = 60 s) runs on its own clock rather than the hourly one --
an abandoned pending had been claimable long past the window every screen
advertised. Expiry leaves no tombstone: nothing was ever displaced, so there is
nothing to record.

**The displaced body is told on the socket it is still holding.** Measured live:
a supersede revoked the credential everywhere HTTP and MCP could see it while
the old body's WebSocket stayed ESTABLISHED for an hour, receiving rings on a
credential the broker refused for every other purpose. One credential, two
verdicts, and the silent half held the socket. On commit the broker closes that
waiter with `reason: credential-superseded` naming the successor and the time,
and `waked` PARKS on it.

The row states follow from what two-phase made answerable (0.2.180, ruling
11945): `agents_seen` carries `moving` and `bodyless`, the launcher gives each
its own row state (`moving`, `no-live-body`) decided BEFORE `elsewhere` -- a
swap in flight is not the same fact as a body working somewhere else -- and
`stateSentence` says what each means. Neither is painted as a fault, and
`destroy` is withheld mid-swap: the body being replaced is still working.

## 16. AS BUILT (0.2.177) -- the handover note

Ruled 12018/12019/12022/12024 from the operator's 12015. Buildable only now:
under the old mint the outgoing body was dead the instant the credential landed,
so there was no moment in which it could write anything down. Two-phase created
that moment.

At a PENDING mint the broker RINGS the still-live body with
`reason: swap-pending` (`daemon._swap_pending`, successor named when known) --
a ring, not a close,
because that body is still the live one and may keep working. The doctrine block
teaches the response, in this order:

1. **Save the work.** Files do not travel, so a note describing uncommitted work
   the new body cannot reach describes something lost: commit everything to
   `wip/<agent>/<utc-ts>` and push it, never onto main and never force -- that
   branch exists so the far side can FETCH it, not so it can overwrite anything.
2. **Write the note.** `memory_add(kind='state')` with five fields: task, that
   branch and sha, next step, open threads, what is undone. If the push was
   impossible, say exactly `unpushed at <host>:<path>`, so the new body knows
   the work is stranded rather than assuming it travelled.
3. **Verify the push**, then post the five fields to the room.

The note is the AGENT's act and is never synthesised: the broker cannot know
what is worth saying, and a fabricated handover is a record of work nobody did.

**The grace the note needs** (R2, ruled 12305, shipped 0.2.190/0.2.191). The
swap commits the instant the far side joins -- 27 seconds after the ring, as
measured -- and the note kept losing that race. A credential superseded within
`HANDOVER_GRACE_NS` (5 min) keeps exactly two acts, `memory_add(kind='state')`
about its own identity and one `send` carrying the five fields, and nothing
else: `store.handover_grace` resolves it, `daemon._handover_only` refuses every
other route by name, and only those two tools take the permissive principal.
Its rooms come from the IDENTITY's memberships (`store.rooms_for_agent`) --
the credential's own rows are already gone, which is what makes the revocation
instant.
Order matters and is the whole point: the note goes SECOND, because verification
is the only step that can wait. Two defects were found inside that window and
both are load-bearing: `_mem_ctx` read the tier off the token row the supersede
DELETES (so the one permitted act crashed), and `agent_scope` keyed on
`token_id`, which a handover principal does not have -- the note landed at scope
`agent:`, a bucket every handover body of every identity would have shared and
no arriving body ever reads. `agent_scope` prefers `agent_id` when given, and
every caller passes it.

## 17. THE ACCEPTANCE CHAIN, AS RUN (2026-08-19)

The chain was run end to end against a live broker rather than reasoned about,
and it is the reason the sections above read as they do. What it found, in the
order it found it:

- **Step 8, five defects, one shape** (0.2.188): every one let a move LOOK
  finished while the identity had not moved. A pending credential could open a
  wake socket; three unclaimed pendings for one identity coexisted (a bound mint
  now discards the identity's unclaimed pendings and names them in
  `discarded_pending`); the mint ran BEFORE the local steps that can refuse
  (mint-last restored, ruling 12271); `claude mcp add-json` refuses a duplicate,
  so registration is remove-then-add and idempotent; and a bound re-mint with no
  rooms carried the OWNER's rooms instead of the IDENTITY's.
- **One directory, one agent** (0.2.192): `reveille init` with no `--dir` from
  another body's directory wrote one agent's credential over another's, and the
  next session there ARRIVED as the wrong identity -- destroying the handover
  note that body was writing. init now refuses a directory that already names a
  different agent, before the mint, naming both remedies.
- **The self-heal and the ticket** (0.2.193 -> 0.2.195). 0.2.193 taught a parked
  daemon to adopt a credential that arrived by a path it did not take. But
  claiming a ticket writes the new secret to THAT SAME FILE, so a body that
  claimed and then missed its arrival window re-parked with a dead credential on
  disk: different from the spent one, therefore adopted, therefore refused,
  therefore parked again -- for ever, with `_claim` never reached. One missed
  window cost that machine every FUTURE ticket. The daemon now remembers every
  secret it has dialled and adopts only one that is NEW to it. Lesson:
  `a-self-heal-must-know-its-own-handwriting`.
- **The negative test** (measured on 0.2.195, chain-probe, native shape, real
  broker). Park -> ticket #1 claimed in 3 s -> nobody joins -> `PENDING_TTL`
  expires at 600 s, swept within 6 s -> re-park -> ticket #2 claimed in 4 s.
  The two numbers that are the whole point: **"adopting it and reconnecting"
  lines: 0** (0.2.193 had 32 and rising) and **RECALLED lines: 2** (0.2.193 had
  1, because ticket #2 was unclaimable). The daemon lived 12m04s across both
  parks and never exited. The run proves the WAKED CODE PATH only; arrival
  mechanics are not claimed by it -- those were proven three times the same day
  on real bodies (18:42Z, 20:33Z, 21:10:14Z).

**What the chain taught that is not in any of the code above.** The claimant on
a parked machine is a PROCESS, not a file: after a claim, the spent secret every
future ticket is keyed to exists only in the daemon's `parked_secret`, in memory,
because claiming overwrote the file. So "restart the daemon to pick up the fix"
is destructive for exactly the body a ticket exists for, and the machine looks
healthy the whole time -- a fresh daemon boots, polls with the dead file secret,
and exits at `ORPHAN_POLL_S` looking orderly. That instruction was sent and
retracted before it was acted on. Lesson:
`the-claimant-is-a-process-not-a-file`; the durable repair is ruled at 12393
(the parked daemon writes the spent secret to `.claude/.reveille-parked`, 0600,
claim-only, unlinked on any attach).

**Two gaps this audit found in the built code, neither load-bearing, both
recorded rather than fixed here.** (1) Nothing sweeps the `recalls` table:
expiry is enforced by the `expires_ns` predicate in `recall_claim` and by the
open/claimed/expired label `recalls_for` computes, so rows accumulate and the
read is capped at 50. Correctness is unaffected -- an expired row cannot be
claimed -- and the fix is one sweeper beside `expire_pending` whenever anyone
touches this table next. (2) `MIN_BODY_VERSION` is ruled in DES-020 (a broker
that refuses a body too far behind, `too_old`) and is NOT in the code: what
shipped is the convergence half (`waked._converge` at `UPGRADE_INTERVAL_S`,
upgrade-only, `<` never `!=`). A body converges toward the broker; the broker
does not yet turn one away.

Two bounded fallbacks close the same class from the other end (ruling 12305 R1,
12320): a body superseded while STOPPED polls `/recalls/claim` with the
credential it holds for a bounded `ORPHAN_POLL_S` and then gets out of the way,
and the launcher STOPS (never destroys) a container whose identity has moved on
(`_stop_superseded`) -- a superseded body used to keep its CPU, its tmux and a
terminal the Agents page still offered, found by the operator twice in one
afternoon. Two details in that loop are the ruling, not the implementation's
taste: it borrows the BODY's own credential for one read (`_credential_known`),
so the launcher stays credential-less and answers only about that body; and the
five-minute grace is the BROKER's to keep, because a just-superseded credential
answers 200 for exactly as long as it may still write its handover note and 401
the instant that window closes. No clock on the launcher side. An unreachable
broker stops nothing -- `None` is never read as "every body here is dead".
