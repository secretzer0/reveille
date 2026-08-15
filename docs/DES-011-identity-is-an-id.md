# DES-011: Identity is an id, a name is a label

Status: RULED — operator directive 2026-08-15 (msgs 10946, 10950), architect
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
- **The name is a mutable label, unique per OWNER among LIVE agents.** Two
  humans may each have an `architect`. One human may not have two.
- **A rename is an UPDATE of that label**, checked against the same
  uniqueness, and nothing else moves.

## 3. RULED: resolve at send, store the id

`to="reveille-architect"` resolves to an id **at send time**, within the
sender's owner scope, and the message stores a recipient id. Delivery never
re-reads the label afterwards, so a rename cannot orphan mail.

A bare cross-owner name **resolves or refuses with the candidates listed**. It
never guesses: guessing is how a message reaches the wrong body while every
signal reads green, which is this document's origin story.

## 4. RULED: the wire keeps names; the store keys on ids

Uuids on the wire would make every message unreadable to the humans the room
exists for. Tools keep taking names; the broker resolves. Ids surface only
where ambiguity is real — cross-owner addressing, and a merge.

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

- **room membership and presence** — `members` is keyed `(room_id, name)`.
- **inbox and receipts** — `reads` is keyed `(message_id, agent)`, a name.
- **`to=` routing** — `messages.recipient` is a name; there is no
  `recipient_agent_id`.

Until these key on the id underneath, a rename is only safe on the planes
already cut over. **This is the finishing slice**, and it is the one that
makes §2 true rather than aspirational.

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

**All four fold onto `3b29c8b1`.** The operator's goal is one being with its
whole history, and the 37 state notes on the retired `9f8c13fa` are exactly
the institutional memory worth recovering — leaving them attached to a retired
id is losing them in place.

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

Folding makes that later backfill SAFE rather than risky: once both
`architect` and `reveille-architect` belong to one identity, a name-to-id
backfill has exactly one answer for either spelling. Doing the fold first is
what removes the ambiguity the backfill would otherwise have to guess at.

## 8. What the uuid does NOT buy

Two of one owner's live agents still cannot share a name: addressing has to
resolve, and "resolve or refuse" needs a unique answer. What the id buys is
that renaming is SAFE and that a fork is REVERSIBLE — not that names stop
mattering.

## 9. Gates

1. **A rename orphans nothing**: send, rename the recipient, assert the unread
   still arrives and history still renders.
2. **The merge loses nothing**: counts per table before and after, and the
   survivor's inbox contains what both bodies were sent.
3. **A cross-owner bare name refuses with candidates** rather than guessing.
4. **Two live agents of one owner cannot share a name**, before and after a
   rename.

## 10. Open

- Whether `recipient_agent_id` is added beside `recipient` or replaces it. The
  render rule (§4) means the name column has no readers left once the id
  lands, but removing it is a data-loss decision and gets its own slice.
- The rename log's shape — an `agent_names` history table, or an audit row.
