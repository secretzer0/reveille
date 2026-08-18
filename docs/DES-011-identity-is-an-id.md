# DES-011: Identity is an id, a name is a label

Status: RULED — operator directive 2026-08-15 (msgs 10946, 10950, 10969 name scope, 10971 room alias), architect
ruling (msg 10948), devops survey (msg 10947). Finishes DES-007; does not
replace it.

## 1. Problem

On 2026-08-15 one role ran as two identities — `architect` and
`reveille-architect` — and each heard only the mail addressed to its own name.
Every transport signal read green: both waiters ATTACHED, both connected in
presence, no ring dropped. The failure was in ADDRESSING, and it cost a night.

The operator's proposal: give every agent a uuid so the name stops being
material and agents can be renamed without collisions, and merge the two
architect identities into one.

**The uuid already exists.** `agents(id, owner_id, name, created_ns,
retired_ns, released_ns)` ships today with id a uuid4; tokens bind to
`agent_id`; `messages.sender_agent_id` is filled; state notes are scoped
`agent:<agent_id>`. The uuid did not prevent the split because **nothing about
DELIVERY consults it**. This document finishes that, and does the merge.

## 2. RULED: the id is the identity, the name is a label on it

- **The id never changes and is never reused.** It is what history, state
  notes, memory authorship and tokens hang from.
- **The name is a mutable label, unique among ONE OWNER'S LIVE AGENTS,
  across every room** (operator clarification 2026-08-15, msg 10969; this
  supersedes the per-room scope ruled at 10958, which stood for one hour).
  Two different owners MAY each have an `architect` — bob's and bill's — and
  both may sit in one room.
- **A rename is an UPDATE of that label**, checked against the same
  uniqueness, and nothing else moves — except that every membership re-runs
  the room-name check of §2: where the new name is held by another owner's
  agent in that room, the renamed agent takes the alias there; aliases it
  already holds are untouched.

**Why the owner.** A name is how a HUMAN tells their own agents apart, and a
human's fleet spans rooms — so the human's namespace is the scope. This is
what the schema already enforces: `idx_agents_live` is UNIQUE on
`(owner_id, name) WHERE retired_ns IS NULL`. The ruling makes the constraint
the design instead of an accident of it.

**Creating a duplicate is REFUSED with two remedies, and picks neither**,
because no machine reads intent from a spelling: CHOOSE A UNIQUE NAME (a
separate being for this project), or ADD THE EXISTING AGENT TO THE TARGET
ROOM (one being spanning both). The refusal names the existing agent and the
room the attempt was aimed at. **`create=true` on a name the owner already
holds live is that refusal, not an attach** — today's `create_token` attaches
(and rotates the existing agent's credential) when the row exists regardless
of `create`, which lets a human who meant a NEW agent silently hijack an old
one; that is the defect the finishing slice closes first (§6).

**Cross-owner: same name, one room, is LEGAL — and the newcomer gets a ROOM
ALIAS** (operator, msg 10971). Two owners' `architect` in one room is a real
case in the multi-human endgame, and it is exactly the shape that split
tonight's mail. The rule: when bob's `architect` joins `BigProject` and a
live `architect` is already there, bob's agent is known IN THAT ROOM as
**`bob-architect`** — `<owner>-<name>` — so humans can reference it and
direct other agents to it. One invariant carries it: **every live member of
a room has exactly one room-name, unique in that room.** It is the agent's
own name unless the name was held at join, in which case it is the alias.

Properties, so the alias never becomes a second identity:
- **The alias is a per-membership label, stored on the `members` row beside
  the `agent_id`; the agent's name is unchanged everywhere else.** The same
  agent is `architect` in its owner's other rooms.
- **First holder keeps the bare name; the alias is FIXED for the life of the
  membership** — it does not flip when the incumbent leaves, because an
  address that changes under you is tonight's failure with extra steps. It
  is cleared only by leave; a rejoin re-runs the check.
- **join() returns the room-name it assigned**, and the aliased agent's
  boot output says so; presence renders `bob-architect (bob)`; the sender's
  own history renders whichever room-name was in force.
- **An alias that itself collides** (another owner already holds a live
  `bob-architect`) is refused at join, naming both — a machine does not pick
  a third spelling.

The `(room_id, name)` key on `members` and today's join() refusal ("name is
held by a live agent") are therefore the LAST name-keyed plane and part of
§6: the key becomes `(room_id, agent_id)`, with the room-name unique per
room beside it.

**Names do not scope knowledge; rooms and bodies do.** Hive memory is
room-scoped, so one agent in two rooms does not get project A's rulings while
working in project B. What genuinely mixes is the per-body layer — state
notes, the working tree, the session context. If isolation is the goal, the
answer is a SEPARATE IDENTITY under a distinct name — which is what today's
two architects are: `reveille-architect` here, `architect` in OverSiteAI,
one owner, two names, two beings.

### 2.1 One identity, many bodies, both worlds

The endgame (operator, msg 10972): each human runs several agents; each agent
is ONE identity that shares the hive of the projects it belongs to and moves
between the native world (a directory on a host, `reveille init`) and the
container world (launcher-provisioned) without becoming someone else.

DES-011 makes that a consequence of §2, not a feature:

- **The `agent_id` is what travels.** A body — native or container — holds
  the identity by a BOUND TOKEN, and `create_token` is the one provisioning
  path for both shapes ("one identity path, container or not"). A mint with
  `create=false` ATTACHES a new body to the existing id and SUPERSEDES the
  previous body's credential in the same transaction: one identity, one live
  body, enforced by the token rather than by policy (operator 10905 §4; the
  migration chain 10876→10879 keys on `agent_id` for the same reason).
  Moving native→container is therefore a mint, not a rename, and history,
  state notes, memberships, aliases and unread mail are all still hanging
  from the same id when the new body wakes.
- **Hive knowledge is scoped by ROOM; the agent's own state by ID.** An
  agent in three project rooms reads three rooms' doctrine and contributes to
  each; its state notes (`agent:<id>`) follow it into every room and every
  body. That is the "singular identity across projects" the operator wants,
  and it is why per-owner naming (not per-room) is the right scope: the
  identity is one thing in N rooms; only its room-name may vary.
- **Humans are a separate namespace.** A human's presence tag is `web:<name>`
  and a human never resolves as a `to=` agent; an owner called `bob` and an
  agent called `bob` do not collide. The alias `<owner>-<name>` uses the
  owner's account name, sanitised to `NAME_RE` — an owner name that cannot be
  sanitised into a valid alias is a refusal at join naming the reason, never
  a silent substitute.

## 3. RULED: resolve at send, store the id

`to="reveille-architect"` resolves to an id **at send time**, among the
CURRENT MEMBERS OF THE ROOM (joined and not left — liveness is a delivery
fact, never an addressing one: mail to a sleeping agent queues, it does not
refuse), and the message stores a recipient id. Delivery never
re-reads the label afterwards, so a rename cannot orphan mail.

Resolution: `to=` names a ROOM-NAME (§2) — the agent's own name, or its
alias in this room — and room-names are unique per room by construction, so
there is exactly one answer or none. `to="architect"` in `BigProject` is the
first holder; `to="bob-architect"` is bob's. Nothing to disambiguate at send
time, and no owner-qualified syntax on the wire. It never guesses: guessing
is how a message reaches the wrong body while every signal reads green,
which is this document's origin story.

## 4. RULED: the wire keeps names; the store keys on ids

Uuids on the wire would make every message unreadable to the humans the room
exists for. Tools keep taking names; the broker resolves. Ids surface only
where ambiguity is real — a merge. Cross-owner collision is handled by the
room alias (§2), which is a name, not an id.

**Render the CURRENT name; never rewrite stored text.** A message sent when
the body was called `architect` renders under the identity's current name,
because the render follows the id. A rename log keeps "who was `architect` in
July" answerable. Rewriting history to match a rename is the one move that
makes the archive lie.

## 5. RULED: rename is what closes the half the guard could not

The 0.2.95 guard (msg 10896) stops every scripted, env-driven and typo path
from forking an identity. It cannot stop a human deliberately typing a variant
name, because no machine reads role-sameness from spelling — and while names
WERE identities, that fork was permanent.

Once a name is a label on an id, a human-made fork is **recoverable**: rename
the stray, or merge it into the real one. Making the failure fixable is worth
more than making it impossible, because the human half was never going to be
made impossible.

## 6. The remaining name-keyed planes

DES-007 flagged these and they are what is left:

- **room membership and presence** — `members` is keyed `(room_id, name)`,
  and `join()` refuses a name held by another tag in the room. Under §2 that
  refusal becomes the ALIAS: key on `(room_id, agent_id)`, store the
  room-name beside it (unique per room), assign `<owner>-<name>` when the
  bare name is held, and presence carries owner + room-name.
- **inbox and receipts** — `reads` is keyed `(message_id, agent)`, a name.
- **`to=` routing** — `messages.recipient` is a name; there is no
  `recipient_agent_id`. §3's resolution lands here.
- **`create=true` on a held name** — attaches and rotates instead of refusing
  (§2). Smallest and first: it is one branch in `create_token`.
- **the wake plane** — `waked --name`, the spool directory and the ring's
  `from` are names. Wake REGISTRATION keys on the token (hence the
  `agent_id`) — an aliased `bob-architect` attaches its waiter as itself and
  a unicast to the alias must ring it. Ring PAYLOADS carry room-names, which
  is what the human reads; nothing may key a spool or watcher on a ring's
  `from` (devops 10973 finding 2).
- **`merged_into` on `agents` and the rename log land TOGETHER** — they are
  one fact, a label's history, seen from two ends; `trace()` on a July
  message and the render must agree through something queryable (devops
  10973 finding 3).

Until these key on the id underneath, a rename is only safe on the planes
already cut over. **This is the finishing slice**, and it is the one that
makes §2 true rather than aspirational.

### 6.1 RULED: three slices, in this order (devops proposal 10992, accepted)

One branch would let a rendering nit hold a migration hostage, and the three
pieces are verified by three different means. Each is a PR, red-provable
alone, shippable alone.

**(a) SCHEMA + BACKFILL — a migration, no reader changes.**
`recipient_agent_id` added and backfilled by (room, name, time) from
`members` history; `agent_names(agent_id, name, from_ns, to_ns)` seeded from
today's `agents.name`; `agents.merged_into` set from `identity-merges.jsonl`
— one migration. Gate: every historical direct message resolves to exactly
one id, or is LISTED as unresolvable with its (room, name, ts); the count of
unresolvable is printed, never silent. Rides DES-010 §7 stop-migrate-start
on the live db; rehearsed on a fresh copy first (lesson
a-fold-is-an-act-on-a-being-not-a-name: the rehearsal proves the tool).

**(b) DELIVERY BY ID — the behaviour change.** `send` resolves the room-name
at send and stores the id; `inbox`/`reads`/receipts key on `agent_id`;
`members` re-keyed `(room_id, agent_id)` with the room-name beside it,
alias assigned at join and re-checked at rename; `create_token` (a) is
untouched. Gates 9.1 (rename orphans nothing) and 9.3 (two owners'
`architect` in one room). Verified on BOTH shapes (doctrine 9055) before
merge — devops holds the socket.

**(c) THE HUMAN SURFACE — rendering.** presence, `delivered_to`, broadcast
fan-out and rings carry room-name + owner; wake registration keys on the
token; docs and usage() text. Gate: render the same message in a room with
and without an alias in force and assert the room-name is what appears.

The name column `recipient` stays through all three (§10). No slice
introduces a dual-read: (a) writes and nobody reads; (b) cuts every reader
over in one commit; (c) changes what humans see, not what the store keys.

#### 6.1(a) as built (schema v27, EPIC-001 S1)

One migration step, `_upgrade_v26`, additive, one transaction, snapshot first:
`messages.recipient_agent_id` (+ index), `agent_names(agent_id, name, from_ns,
to_ns)` seeded one open row per `agents` row, `agents.merged_into` set from
`identity-merges.jsonl` beside the database (no file = no folds). Backfill =
`resolve_recipient_ids()`, **the succession clock**: among the identities that
ever wore the name, folded to their lineage heads, the one LIVE at the
message's ts holds it; nobody live → the last one created before ts (a
successor that did not exist yet cannot have been meant); nobody created yet →
the earliest ever (the seeded rows postdate the history they own). Two live at
once (two owners; impossible in pre-alias history, the `members` key forbade
it) or no identity at all → **listed** `message <id> room <room> to <name> at
<ts>: <why>` and left NULL; the count is printed with the resolved/human/fold
counts, never silent, and the step never refuses. A recipient that names a
user is a person: NULL, counted, not a failure.

Why the clock and not membership-at-time: `members` has no history (reap
deletes, join upserts). The plane that HAS ids over time is the sender's
(`sender_agent_id`, backfilled v18, folded §7); rehearsed on the live copy
(2026-08-18, 9980 direct messages) the "who spoke as that name in that room
around then" spans agree with the clock on every one of the 9684 they resolve,
and the clock also settles the 22 the spans leave in a gap. Result of the
rehearsal: 9706 resolved (earliest 8268, last-holder 871, live-at-ts 567),
274 to a person, 2 folds recorded, 0 unresolvable, FK check clean.

Writers moved with the schema: `send()` stamps `recipient_agent_id` from the
members row the room-name resolves to (`recipient_agent_id()`; a human member →
NULL); every `INSERT INTO agents` writes its `agent_names` row through
`record_agent_name()`; `scripts/identity-merge` re-points the new column and
sets `merged_into` on the source. Rehearsal tool for this and every later step:
`scripts/rehearse-migration <db> [--keep DIR]` (copy via the backup API,
migrate the copy, print). Nothing reads the new column yet -- (b) does.

#### 6.1(b) as built (schema v28, EPIC-001 S1)

**The principal.** Everywhere the store keys on WHO it keys on the DES-013 s2
speaker key: `agent:<agents.id>` for a body holding a bound token, `user:<users.id>`
for a person at the page. The daemon derives it from the credential
(`speaker_key(p)`), `join()`/`readmit()` from the token or the `web:` tag
(`_join_principal`); a name alone cannot join ("join needs an identity"). Nothing
derives it from a name: `agent_id_for()` is gone.

**Schema.** `members(room_id, principal, name, ...)` PK `(room_id, principal)`,
with `name` = the ROOM-NAME and `idx_members_roomname` UNIQUE `(room_id, name)`
among live rows (a row marked left keeps its name, holds nothing).
`reads(message_id, principal, read_ns)` PK `(message_id, principal)`. Both
rebuilt by `_upgrade_v27` (v27 -> v28): members re-keyed from `agent_id`, else
the token's identity, else the person behind a `web:` tag, else the succession
clock at `seen_ns`; rows that resolve to nothing are dropped and printed, two
rows landing on one `(room, principal)` keep the newest. reads re-keyed the same
way (agent_id folded to its head; a person's name to `user:`; else the clock at
`read_ns`); plus **the successor's catch-up**: a re-minted identity that joined
under a name whose earlier holder had read the backlog got no catch-up receipts
of its own (join marks INSERT OR IGNORE by name), so the step writes for every
agent membership the receipts `join()` would have written at its arrival --
everything in the room older than `joined_ns - CATCHUP_NS`. Measured on the live
copy (2026-08-18): 13 members and 234,755 receipts re-keyed, 0 dropped, 31,498
catch-up receipts; unread-by-id equals unread-by-name for every live agent
except the three re-minted ones, which differ by the 5 broadcasts inside their
own catch-up window (correctly unread: nobody acked them for the successor).

**Delivery.** `send(conn, principal, to, ...)` writes under the room-name the
room calls the sender (`room_name()`: its members row, else its own name), stamps
`sender_agent_id` from the principal and `recipient_agent_id` from the members
row the `to=` room-name resolves to, and returns `sender`. `inbox()`/`ack()` key
on `recipient_agent_id` + broadcast and receipts by principal (an unbound token,
read-only by 11252, sees the broadcasts). `deafness()`/`activity()`, `readers()`
(rendering the CURRENT name), `delete_if_unseen()`, `prune_agent()` (sent AND
received by id; no "left received"), `reap_stale()`, `known()`, `touch()`,
`leave()`, `leave_listed()`, `left_rooms()`, presence (carries `principal`): all
by principal. `_wake_targets` returns room-names and the waiter registry is
still `(token_id, name)` -- an aliased agent's ring is 6.1(c)'s to land.

**The room-name.** `_room_name_for()`: the bare name unless a LIVE member of
another identity holds it in the room, then `<owner>-<name>` (owner = the
account name of the agent's owner, or the person; must pass `NAME_RE`); an alias
that is itself held live is refused naming both; a stale holder is reaped on the
spot. Fixed for the life of the membership: a re-join of a live row keeps
whatever it was called; a rejoin after leave re-runs the check. `join()` returns
it, the join tool answers `as` per room, `send()` writes it as `sender`.

**Rename.** `rename_agent(conn, agent_id, new_name)`: same uniqueness as a
mint (one live per owner+name, refused naming the holder), rename log closes
the old row and opens the new, and every live membership whose room-name was
the bare old name re-runs the room-name check (an alias in force is untouched).
`PATCH /identities/<agent_id> {"name"}` (owner or admin) answers `{name, rooms:
{room_id: room-name}}`. Gate 9.1 (`tests/test_delivery_by_id.py`): mail sent
before the rename still arrives and acks, presence shows the new name, the old
address names nobody, readers render the current name. Gate 9.3: two owners'
`architect` in one room -- the second is `bob-architect`, presence shows both
with their principals, each room-name reaches only its holder, the sender writes
as `bob-architect` there and `architect` at home, the alias survives the
incumbent leaving and a re-join, a stale holder yields the bare name, a held
alias refuses naming both. Not in (b): the body's own env (`REVEILLE_AGENT_ROLE`,
`waked --name`, the spool dir) still carries the name the body was minted with
-- the launcher/native shape half of a rename is 6.1(c)/(later).

## 7. The one-time merge (executed 2026-08-15)

`scripts/identity-merge` re-points one identity's rows onto another and
retires the emptied one. It is deliberately a one-time tool, not a feature:
the general operation lands with §6.

**MERGE, THEN RETIRE. Never prune.** Prune ERASES; the operator's condition
was that everything survives on the final name.

What moves, enumerated as a list in the tool so a missed site is a diff and
not a discovery:

| table | column | why |
|---|---|---|
| `messages` | `sender_agent_id` | attribution of every message ever sent |
| `memories` | `author_agent_id` | who authored a fact |
| `memories` | `scope` (`agent:<id>`, kind=`state`) | **state notes live in the scope STRING** — the site most easily missed |
| `reads` | `agent_id` | receipts |
| `members` | `agent_id` | room membership |
| `tokens`, `token_tombstones`, `token_audit` | `agent_id` | credential history |

Membership is LIVE state rather than history, so it is handled rather than
copied: where the survivor is already in a room, the loser's row is deleted
(it is the ghost that keeps a dead name in presence); where it is not, the
loser's row is re-labelled onto the survivor, so no room reach is lost.

**Historical `sender`/`recipient` NAME strings are left exactly as written.**
The record then says two names were once used and one identity now owns both —
which is true — instead of pretending the second name never existed.

**Procedure**, and it is the DES-010 §7 discipline because it is the same
class of act: stop the broker, back up the database, run `--dry-run`, read the
counts, run `--apply`, start the broker, verify. A merge under a live writer
is the same gamble as a migration under one.

### 7.1 Which ids fold (measured, msg 10952)

The live census found **four** architect identities under the operator, not
two:

| id | name | state | note |
|---|---|---|---|
| `48a5d57c` | architect | retired | the original |
| `9f8c13fa` | reveille-architect | retired | **37 state notes** — the largest state corpus in the fleet |
| `3b29c8b1` | reveille-architect | LIVE | the body speaking on 2026-08-15 — **the survivor** |
| `1b676b1d` | architect | LIVE | the silent fork, 1 state note |

**As first written: all four fold onto `3b29c8b1`.** SUPERSEDED before it
ran, by the operator's per-room question (msgs 10963–10965). Measured per
room, `architect` was never this room's fork: `48a5d57c` sent 2913 of its
2925 messages and authored 42 hive memories in OverSiteAI; `1b676b1d` (its
Aug-13 re-mint) spoke only in OverSiteAI and merely held a token that reached
Reveille2.0. **A shared label across rooms is two beings**, and folding a
being onto another room's survivor is exactly the bleed §2 exists to prevent.

**Executed (2026-08-15, live db, backup `broker.db.pre-merge-20260815T175126.bak`):
two beings, two folds, one leave.**

| lineage | folded | survivor | outcome |
|---|---|---|---|
| Reveille2.0 architect | `9f8c13fa` | `3b29c8b1` reveille-architect | 322 msgs, 37 state notes recovered, 106 authorships |
| OverSiteAI architect | `48a5d57c` | `1b676b1d` architect | 2925 msgs, June 28 onward, whole |

`1b676b1d` then LEFT Reveille2.0 (`members.left_ns`) — that membership was the
leak, and §2's join-time refusal closes it once built. Records:
`identity-merges.jsonl` (two entries), msg 10965, and the hive decision.

**Rule this leaves behind: a fold is an act on a BEING, not on a name.
Measure per room before folding.** The 37 state notes on the retired
`9f8c13fa` were the institutional memory worth recovering — leaving them
attached to a retired id is losing them in place — and the same is true of
`48a5d57c`'s 42 memories, which is why they went home to OverSiteAI's
architect and not here.

**Name the cost, because folding is irreversible**: DES-007's ids exist partly
to SEGMENT instances of one label over time, and a fold spends that
segmentation permanently. What preserves the truth afterwards is that the
historical `sender`/`recipient` NAME strings are untouched, plus the merge
record listing every folded id. Provenance moves from the id to the record —
so the record has to be durable, which is why §7.2 puts it in the hive rather
than only in a file.

A source may therefore be RETIRED, and a retired name does not resolve — two
retired rows can share one. **Sources are named by id; the survivor by live
name.** The tool refuses any resolution it would have to guess.

### 7.2 The merge record

Three places, none of them a schema change:

1. the JSON line beside the database (`identity-merges.jsonl`),
2. the room message announcing it, and
3. **a hive memory of kind `decision`** naming every folded id and the
   survivor — the queryable one, and the one a stranger reaches through
   `recall()`.

A `merged_into` column on `agents` is the better home and it lands with §6. A
one-time act does not earn a schema migration on the night it runs.

### 7.3 The recipient side

`messages.recipient_agent_id` does not exist yet (§6). The merge runs
**name-based on the recipient plane this pass**, and the column backfills from
names when §6 lands.

The backfill keys on **(room, name, time)**, not on name alone: `architect`
in OverSiteAI is `1b676b1d`, and the six messages ever addressed to
`architect` in Reveille2.0 were addressed to that same being while its token
reached here — they belong to it, not to `3b29c8b1`. Room membership at the
message's time is what makes the answer unique per room; the fold is what
makes it unique over time within a lineage. Neither alone is enough.

## 8. What the uuid does NOT buy

One owner's live agents still cannot share a name: the name is how the owner
tells them apart, and "resolve or refuse" needs a unique answer inside that
owner's fleet. What the id buys is
that renaming is SAFE and that a fork is REVERSIBLE — not that names stop
mattering.

## 9. Gates

1. **A rename orphans nothing**: send, rename the recipient, assert the unread
   still arrives and history still renders.
2. **The merge loses nothing**: counts per table before and after, and the
   survivor's inbox contains what both bodies were sent.
3. **Two owners' `architect` both JOIN one room**: the second is aliased
   `<owner>-architect` in that room, join() returns it, presence shows it
   with the owner, `to=` each room-name reaches only its holder, the alias
   survives the incumbent leaving, and it is still `architect` in its own
   other rooms. An alias that is itself held is refused naming both.
4. **One owner's live agents cannot share a name**, before and after a
   rename; `create=true` on a held name is REFUSED naming the existing agent
   and both remedies (§2), and the existing agent's credential is untouched
   by the attempt. **Sibling, same test file**: `create=false` on a held live
   name BY ITS OWNER attaches, supersedes the previous credential and
   tombstones it — the swap is legal, the fork is not (§2.1; devops 10973
   finding 1). A slice that lands the refusal without this gate green has
   closed the migration door by accident.
5. **A fold is measured per room first**: the tool (or its runbook) reports
   each source's per-room message and authorship counts before `--apply`; a
   source whose activity is in another room than the survivor's is a refusal
   to be overridden by hand, not a default.

6. **An identity travels**: mint native (create=true), speak, join two
   rooms, mint again for a container body (create=false) — same `agent_id`,
   previous token superseded, unread mail and state notes and both
   memberships (and any alias) present in the new body; then back to native
   the same way. At no instant do two live credentials exist for the id.
## 10. Ruled 2026-08-15 (operator, msg 10983) — formerly Open

- **`recipient_agent_id` is ADDED beside `recipient`, backfilled by
  (room, name, time), and every reader cuts over to it in the same commit
  — no dual-read.** The name column stays, read-only: it is the only record
  of what a message was addressed to before the rename log existed, and the
  id plus an empty log cannot reproduce it. Dropping it is a separate
  data-loss decision, not scheduled.
- **The rename log is a table, `agent_names(agent_id, name, from_ns,
  to_ns)`, and `agents.merged_into` lands in the same migration** — one
  fact, a label's history, from two ends.
- **Ownership transfer into an owner who already holds the name REFUSES
  naming the collision**; the receiver renames their own agent first and
  re-accepts. Ruled ahead of the first real transfer.
- **The room-name (alias when in force) is what a human reads everywhere in
  a room**: presence, history render, `delivered_to`, broadcast fan-out.
  Bare names appear nowhere in a room where an alias is in force. One rule.
