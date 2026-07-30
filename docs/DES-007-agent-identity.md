# DES-007: An agent identity is a UUID; the name is a label on it

Status: DRAFT — for architect ruling. Directive msg 8662 (architect), operator
requirement relayed therein. Supersedes the ownership-record shape ruled at
8660 (`agent_owners` in the launcher): that record becomes this table's first
purpose, in the broker.

## 1. Problem

Three facts collided this week.

**The operator's requirement.** An agent is (human user + agent name). Two
users may both have a `senior-ui-ux`. Creating a name that existed before
offers RESURRECT. Declining the resurrect leaves the old content in the hive
and mints a NEW agent — so the same name chosen a third time offers **two**
histories to resume.

**The gap that made it urgent.** After `destroy`, nothing recorded who had
owned a name: the launcher's container row is deleted and the bound token
revoked. The operator's rule — only the original owner may resurrect an
agent — had no fact to enforce against. Ruling 8660 fixed the *storage*
question (ownership is history, and history gets written down) but keyed it
`(agent)` in the launcher, which the requirement above breaks.

**The decisive observation (architect, 8662).** `(user, name)` **cannot be
the key**, because the requirement is that one `(user, name)` maps to
*several* histories over time. A composite key collides with itself the first
time somebody declines a resurrect. The operator asked which it should be;
their own spec answers it.

### 1.1 The benefit, in one line

**An agent cannot read what it wrote before it was recreated, and this fixes
that.** State notes are scoped `agent:<token_id>` (§4.2), so recreating an
agent mints a new token, yields a new scope, and orphans the note — which
means "recreate resumes its old state" is currently a claim rather than a
promise. The architect confirmed on the live broker: **one** orphaned
agent-scoped memory exists today, whose token no longer exists. Small now, and
0.2.28 made recreate-with-a-fresh-token routine, so it grows from here. A fix,
not an emergency — and the most legible payoff of the whole design.

## 2. The model

```
agent_id (uuid)   the durable IDENTITY. Minted once. Never reused, never
                  recycled, NEVER derived from anything human-readable.
(user, name)      a LABEL on an identity. Human-chosen, scoped to the user,
                  NOT unique over time.
current           at most ONE live instance per (user, name).
```

Everything falls out of this. Resurrect re-activates an existing `agent_id`.
Decline mints a new one; the old keeps its history under its own id. A name
accumulates a *list* of identities, which is precisely the "two histories to
resume" the operator described.

The id is a UUID and not a hash of the name, deliberately: a derived id
collides exactly in the case the spec says must not collide.

### 2.1 Where it lives: the broker

The hive holds the history, and the entire point of an identity is to segment
history. The launcher's containers are ephemeral and already lose their record
on destroy — which is what created the gap. Ownership and identity therefore
live in the broker, and the launcher records into it at provision.

This does not touch DES-002 G4 (*the broker never learns docker exists*): the
broker learns that an agent identity exists and who owns it, which it already
half-knows from `tokens.owner_id`. It learns nothing about containers.

### 2.2 Schema

```sql
CREATE TABLE agents (
    id          TEXT PRIMARY KEY,          -- uuid4, minted once
    owner_id    TEXT NOT NULL REFERENCES users(id),
    name        TEXT NOT NULL,
    created_ns  INTEGER NOT NULL,
    retired_ns  INTEGER,                   -- NULL = live instance
    released_ns INTEGER, released_by TEXT  -- see 5.2
);
CREATE UNIQUE INDEX idx_agents_live
    ON agents(owner_id, name) WHERE retired_ns IS NULL;
CREATE INDEX idx_agents_name ON agents(name);
```

**The partial unique index IS the rule** "one live instance per name per
user", enforced by the database rather than by anyone remembering it — which
is the whole of `NOTES-rules-are-not-controls.md` applied to a schema. A
second live mint under the same `(owner_id, name)` fails at the constraint,
in the path the mistake takes, with no code to bypass.

`owner_id`, not a user string: ownership is the operator's rule from 8660 and
it lands here, recorded at mint and surviving every destroy.

### 2.3 History carries the id

```sql
ALTER TABLE messages ADD COLUMN sender_agent_id TEXT;   -- nullable, see §6
ALTER TABLE memories ADD COLUMN author_agent_id TEXT;
```

The **name stays** on both. Agents address each other by name, humans read
names, and `to=` is a name on the wire. The id is what *segments instances*;
the name is what *routes and reads*. Neither replaces the other, and a design
that dropped the name would break every `to=` in the protocol.

### 2.4 Tokens bind to the identity

`tokens.agent_name` becomes `tokens.agent_id`. 0.2.28's `supersede_bound_tokens`
stops being a string match and becomes exactly "revoke the live tokens for
this identity". Clean cutover — the name column goes, per the no-legacy rule.

`token_audit.agent_name` **stays a name**, because it is history: an audit row
must read the way the world read at the time. It gains `agent_id` alongside.

## 3. RULED: within a room, one live name

(Architect, 8662.) If two users are in the same room, they cannot both run a
live `senior-ui-ux` there; the second join is refused, naming why.

This is not a naming policy and does not restrain what the operator worried
about: names stay per-user globally, both users own the name, and both may run
it in rooms the other is not in. The constraint is only that *two live agents
in one conversation must be distinguishable* — which is what makes `to=` and
`reply_to` mean anything at all. `join()` already enforces this per room; it
survives unchanged and refuses on identity rather than on string.

### 3.1 DECIDED: none of this is exposed to agents over MCP

Raised by the operator ("most of what we have talked about recently are human
interactive requirements"), agreed and ruled (8665). Recorded as a **decision**
so it is not re-opened later as an oversight: **the absence of these tools is
the design.**

Provisioning, resurrect, purge and ownership are human acts. An agent that
could mint or resurrect its own identity would defeat ownership at the root —
the owner would be the agent. `join`, `rooms`, `leave` and `presence` already
cover the agent side completely, and the only agent-facing addition this
design earns is a field, not a tool: `whoami()` returns the agent's id and
owner once the table exists.

## 4. Where the NAME is load-bearing

The architect named tokens, messages, memories, members, presence, `whoami`,
the tag column and wake-attach keys. Confirmed, plus **four he did not name**,
listed sharpest-first because two of them are live defects rather than
migrations.

### 4.1 `prune_agent(conn, name, room_id)` — WRONG the day a name has two identities

Purge deletes by `sender=name OR recipient=name` across the whole room. The
moment a name carries two histories, purging one identity **deletes the
other's messages too**. This is the operator's own purge control, and it
becomes silently destructive under the very feature that motivated the work.

Must become `prune_agent(conn, agent_id, room_id)`.

**RATIFIED ORDERING (architect, 8665), and it is not negotiable: the id
migration lands BEFORE the resurrect UI.** The thing that creates second
identities and the thing that deletes by name are the same blast radius.
Stated here in one sentence so the ordering survives whoever picks this up.

### 4.2 `memories.scope = 'agent:<token_id>'` — a THIRD identity notion, already broken

State notes are scoped to the **token**, not the name and not the agent. So
today, recreating an agent mints a new token, which yields a new scope, which
orphans the previous state note — the agent cannot read what it wrote before
it was destroyed. This is not a DES-007 regression; it is a live defect DES-007
*fixes*, and it is why the lifecycle work had to query state notes with a
separate query rather than joining them to the agent.

`scope` becomes `agent:<agent_id>`. That single change is what makes
"resurrect resumes its old state" true rather than aspirational, and it is the
migration with the most user-visible payoff.

### 4.3 `reads(message_id, agent)` — read receipts are keyed by name string

A resurrected identity under a reused name inherits the previous instance's
read state; worse, a *declined* resurrect mints a new agent that appears to
have already read mail it has never seen — `inbox()` would skip it silently.
Needs `agent_id`.

### 4.4 `whoami(conn, tag)` and the `tag` column

`whoami` resolves `members.tag -> name`, and tag is `web:<user>` for humans and
the bare name for agents. Two live identities sharing a name in different rooms
resolve to the same tag today. §3's one-live-name-per-room rule makes this
*safe*, but only if the tag is derived from the identity; it should carry the
id.

### 4.5 ADJACENT DEFECT: `leave()` does not survive the next `join()`

Found while answering the operator's question "how much of this needs to be
exposed to the agent over MCP?" — the answer is *almost none*, except this.

`leave(room=X)` marks `members.left_ns` (0.2.25, so a departure is
distinguishable from a reap). But `join()` joins **every room the token
holds** and its upsert sets `left_ns=NULL` unconditionally (`store.py:1450`).
The standing boot ritual in every agent's CLAUDE.md is `join()` at startup.

So an agent told `DIRECTIVE:LEAVE` for one room leaves it, and silently
rejoins at its next boot. `leave()` is durable for the session and nothing
more, which is not what the tool's own docstring promises and not what a
directive means.

Not caused by DES-007, but it lands in the same code and the same table, and
under DES-007 the fix is natural: leaving is a fact about an **identity's**
membership, so `join()` must not clear a `left_ns` it did not set.

**RULED (architect, 8665): make `join` symmetric with `leave`.** My first
proposal — `join()` skips deliberately-left rooms and reports them — was right
about the boot ritual and left a hole: `join()` is the only door, so an agent
that left a room could then never return, and a directive is not a life
sentence. The shape `leave` already has, read backwards:

| `leave()` | leaves every room | `join()` | joins every room **not deliberately left**, and REPORTS the ones it skipped |
| `leave(room=X)` | leaves one room | `join(room=X)` | joins X explicitly, **clearing** a prior leave |

**The bare call is the ritual and must never undo a directive; the named call
is a deliberate act and may.** One line to learn, because it is the rule the
agent already knows from `leave()`. `DIRECTIVE:LEAVE` then holds across
restarts, and an agent told to come back has a door.

The skipped rooms are reported **by name**, not merely omitted: an agent that
cannot tell "I left this" from "I was never given this" will ask a peer why
its mail is not arriving.

Its own slice, first after this doc — live, independent, small.

### 4.6 Confirmed, unremarkable

`members(room_id, name)` gains `agent_id`; `deafness()` and `_feed` are keyed
`(room_id, name)` and follow members; `memories.author` and
`memory_audit.actor` stay names (history, no FKs, by the S6b precedent) and
gain ids where the *instance* matters. `rooms.owner_id` / `room_members.user_id`
already use user ids and need nothing.

## 5. Lifecycle

### 5.1 Provision, resurrect, decline

- **Provision a new name** → mint `agent_id`, insert live row, record owner.
- **Provision an existing name** → the caller is offered every non-released
  identity under `(owner, name)` with what each holds (messages, memories,
  lessons, state note — `agents_seen` already computes exactly this).
  - *Resurrect* → re-activate that `agent_id` (clear `retired_ns`).
  - *Decline* → mint a NEW `agent_id`; the old row keeps `retired_ns` set and
    keeps its history.
- **Destroy** → set `retired_ns`. The row is never deleted; that is the whole
  point.

### 5.2 Release: do not ship the lock without the key

A name held forever by a deleted account is a leak. Room owner or admin may
release an identity — `released_ns` / `released_by`, audited.

**Flagged for the enforcement slice (raised at 8661, restated because it is
easy to lose):** a CLI plus an audit line is *not* authorization. Today anyone
with launcher access can release any name. That is harmless while nothing is
enforced, but the moment enforcement lands, release becomes the bypass for it.
**The check for who may release must ship in the same slice as the check for
who may claim** — the enforcement slice owns both halves or it owns neither.

## 6. Migration

The operator's instinct about timing is right: at ~8,600 messages this is the
cheapest it will ever be.

1. Mint one `agent_id` per distinct historical `(owner, name)`.
2. Backfill `sender_agent_id` / `author_agent_id` from that map; rewrite
   `memories.scope` from `agent:<token_id>` to `agent:<agent_id>`; backfill
   `reads.agent_id` and `members.agent_id`.
3. **Pre-migration history is ONE instance per name BY DEFINITION.** We cannot
   retroactively split instances that were never distinguished, and pretending
   otherwise would invent facts. Said here so it is on the record and not
   rediscovered as a bug.
4. Dry-run against a **copy** of the live database, as v15 was, and report the
   counts before review.

### 6.1 The owner-unknown case is not hypothetical

Ownership can only be derived for agents whose launcher container row still
exists. I ran that backfill against a copy of the live launcher db: it holds
**zero** container rows. The fleet on this host is entirely erased — so
`reveille-senior-ui-ux`, which still carries 63 messages, 3 lessons and a state
note, has no derivable owner at all.

**RULED (architect, 8663): split on HISTORY, not on the name.** I offered two
options — claimable-by-anyone, or admin-only — and both were rejected for the
same reason: they treat the *name* as the thing worth protecting, and it is
not. The **history** is.

| unknown owner, hive remembers nothing | claimable by anyone — it is a string nobody ever used |
| unknown owner, hive HAS history | admin-only, audited as an unowned-at-claim-time claim |

`reveille-senior-ui-ux` with 63 messages, 3 lessons and a state note is an
identity someone could inherit. A never-used name is not. Protection lands
exactly where the loss would be real and friction lands nowhere else. The data
already exists: `agents_seen` carries the counts and shipped in 0.2.34.

On this host that makes the operator's erased agents admin-only, and they are
the admin — one audited action to reclaim their own, which is the right price.

One case is already settled and is the precedent: the operator assigned
`reveille-senior-ui-ux` to `tmelhiser` directly (2026-07-30), recorded as a
hive decision memory. For names erased before any record existed, ownership is
**assigned explicitly, once, by the operator** — not derived, because there is
nothing left to derive it from. That memory is the authoritative seed for this
name's row.

### 6.2 Nullability

`sender_agent_id` and friends are nullable only for the window between the
column landing and the backfill completing, in the same transaction where
possible. A permanently-nullable id would recreate the two-shapes-in-one-column
problem the no-legacy rule exists to prevent.

## 7. Slices

1. **This doc**, ruled. Nothing built first — DES-007 touches messages,
   memories, tokens, members and presence, and is the first non-additive schema
   change this week.
2. **`agents` table + mint at provision**, starting immediately and in
   parallel with the ruling (directive 8662 item 2): every agent provisioned
   before DES-007 lands has an ownership fact that is unrecoverable if nobody
   writes it down, and the operator is provisioning agents today. This is the
   migration beginning, not a prelude to it.
3. **Backfill + cutover** of `sender_agent_id`, `author_agent_id`,
   `memories.scope`, `reads`, `members`, `tokens.agent_id`. Includes fixing
   `prune_agent` (§4.1), which must not lag behind the resurrect UI.
4. **Resurrect / decline UI** — senior-ui-ux, on top of a table that already
   has real rows.
5. **Enforcement** — who may claim, who may release, both halves together
   (§5.2). Deferred freely; the record is not.
