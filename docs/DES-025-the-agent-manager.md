# DES-025: The agent manager — a role is a record, a name is an identity

Status: RULED, not built. Ruled by the architect at msgs 13455 and 13458
(13458 amends 13455 on the rename question, taking red-shirt-01's reading at
13454); operator ask 13453, operator GO 13465. Depends on DES-005 (web
provisioning, role templates §9), DES-007/DES-011 (identity is an id) and
DES-024 (the console lists identities). Builds nothing in the transport or
credential layers.

## 1. Problem

The operator asked for three things: rename an agent, change its role, and add
custom roles — "because what we have is pure shit". Two of the three are
symptoms of one defect, and the third is a different feature wearing the same
word.

**A role is not stored anywhere.** `ROLE_PROMPTS` is a dict literal in
`scripts/reveille_launch.py`. The container receives only the COMPOSED PROMPT
TEXT in `REVEILLE_ROLE_PROMPT`. The role's NAME is recovered afterwards by
`split_role_prompt`, which takes the **longest matching prefix** of that text
against the same dict. Three consequences, all live today:

- the catalog cannot be extended without editing source, so "add a role" is a
  deploy;
- two roles whose text shares a prefix are one edit away from resolving to
  each other, and the resolution is silent;
- rewording any prompt makes every existing container's role unrecoverable —
  the edit form then shows an opaque blob, or the wrong role selected.

This is structure re-derived from a rendering. The fleet already holds the
lesson (`a-boundary-only-a-human-can-see-is-not-a-parse`): structure comes
from the writer's record, never from a pattern over its rendering.

**Changing a role already works.** `role` and `append` are in
`EDITABLE_FIELDS`; `PUT /agents/{agent}/config` applies them and reads back a
per-field verdict; the page renders a role `<select>` and an append box in the
edit modal. What is missing is a place to find it and a catalog worth
choosing from. Item 2 is a surfacing job, not a build.

**Rename is not a label edit.** The agent's name IS its bus identity and it is
load-bearing in at least six places (red-shirt-01, msg 13454):

1. `$REVEILLE_AGENT_ROLE`, read per process by `waked` and every session;
2. the broker's per-room alias `<owner>-<name>` (DES-011 §2) — `join()`
   returns `as` per room;
3. credential and ticket rows keyed to the identity (a recall ticket is keyed
   to a credential);
4. the spool path `~/.reveille/spool/<name>/` and the `waked` flock inside it;
5. `wip/<name>/<ts>` branches, and authorship on every message ever written;
6. presence, filters, and the room's rendered feed.

## 2. RULED: the shape, in one sentence

**A role is a RECORD composed from a catalog at provision time; a name is an
IDENTITY that never moves, and what a human wants to change is its LABEL.**

## 3. RULED: roles become data

### 3.1 The catalog

Three tiers, resolved in one order — the same shape `resolve_credentials`
already uses (override > global):

    per-user custom  >  boot env override  >  shipped default

- **shipped default** — today's `ROLE_PROMPTS` text, unchanged, as the
  fallback.
- **boot env override** — `REVEILLE_ROLE_PROMPT_<ROLE>`, read at launcher
  start. A role with no override keeps its shipped text. No new file format,
  no config language: the operator asked for boot-time env defaults and this
  is exactly that and nothing more.
- **per-user custom** — `roles: {name: text}` in the user profile. The name is
  validated at WRITE time, the same discipline as `claude_mode` and
  `multi_driver`: an unknown or malformed choice is refused where it is
  written, never discovered as a silent default at provision time. A role text
  is a choice, not a secret, so `masked_profile` passes it through.

### 3.2 WHAT A RUNNING SYSTEM READS: neither

A running body reads exactly one thing: the composed prompt in its own env.
The catalog is consulted at **one moment** — compose time, when an agent is
provisioned or its role edited.

This is the answer to the one-state-one-writer challenge (red-shirt-01,
13454): the catalog is an INPUT TO AN ACT, not a live value anything reads, so
there is no second writer. Editing a role's text later does NOT reach running
bodies, and the edit surface must say so at the point of edit — *"takes effect
the next time an agent is provisioned with this role"* — because a user who
believes otherwise will think the system is ignoring them.

If anyone later proposes that a running body re-read its role at runtime, THAT
is the change which creates a second writer, and it is refused then.

### 3.3 The record

The agent's record carries `role` (the NAME) and `append` (the user's text) as
fields. The env keeps carrying the composed prompt, because that is what the
body reads — but the record is the truth thereafter.

`split_role_prompt` demotes to a MIGRATION FALLBACK for containers provisioned
before this lands. It is never consulted for an agent whose record has a role.
A field that can be read must not be guessed.

## 4. RULED: rename is a DISPLAY NAME; the identity is immutable

The identity name stays exactly what it is — every one of the six places in §1
keeps working because nothing about them changes.

A **display name** is per agent, stored in the user profile beside the other
per-agent settings, RENDER-ONLY, and changes nothing any process reads.

**The rule that keeps it from becoming a trap:** wherever a human must TYPE or
COPY an address — send-to, a CLI invocation, a spool path, anything that
resolves to an identity — the surface shows the IDENTITY, or shows both as
`Display (identity)`. A display name alone, on a surface where the identity is
what works, is how someone comes to type a name that does not exist.

The reasoning is the tombstone ruling's (msg 11611): the name that authored a
message must keep meaning what it meant, or a reader inherits someone else's
history.

### 4.1 DEFERRED, not refused: true identity rename

If the display name proves insufficient in use, identity rename becomes its
own design with its own migration. What such a migration would have to move,
atomically enough that a failure is recoverable:

- the container `rev-<user>-<agent>` and the data root `<root>/<user>/<agent>`;
- launcher.db rows (containers, grants, sessions_seen);
- the broker-side name, claimed under the existing per-owner uniqueness rule —
  the new name must be CLAIMABLE BEFORE ANYTHING MOVES, and refused if taken;
- a live `waked` holding a flock in the old spool path, and the credential its
  return ticket is keyed to.

And one thing it must NOT do: **rewrite history.** Messages carry both
`sender` text and `sender_agent_id`. Identity is the id (DES-011); the name is
its current label; the display resolves through the id, and rows too old to
carry an id keep the text they were written under. A record edited to stay
current stops being a record.

The cost is stated at the moment of the act, in the confirm, not discovered
after: **a rename recreates the container, and every re-provision rotates the
agent's bus credential** (msgs 8606/8699).

## 5. RULED: the manager is a surface, not new machinery

One page listing a user's agents: display name, identity, role, append, and
the per-user role catalog. It drives the endpoints that already exist —
`/agents`, `GET/PUT /agents/{agent}/config` — plus profile writes for the
display name and the custom roles.

Every destructive or authority-changing action gets its own per-item confirm
naming what it does. A generic "are you sure" is not a confirm.

## 6. Migration

- Agents provisioned before this keep working: no record ⇒ `split_role_prompt`
  answers, exactly as today.
- The first edit of such an agent WRITES the record, so the fallback drains by
  use rather than by a migration pass.
- No display name ⇒ the identity is the label. The page renders one name and
  nothing changes for anyone who never sets one.

## 7. Gates

1. An unknown role name is refused at profile-WRITE time, not at provision.
2. Resolution order: per-user custom beats a boot env override beats the
   shipped default, asserted in one test over one profile.
3. An agent whose record carries a role does NOT consult `split_role_prompt`
   — the negative that keeps the fallback from quietly becoming the path again.
4. A role whose text is edited does not change any running body: assert the
   container's env is untouched by a catalog write.
5. Display name is render-only: no process input (env, spool path, container
   name, broker name) contains it.
6. Every surface that takes a typed address shows the identity.

## 8. Not ruled here

- True identity rename (§4.1) — deferred by design, its own document if asked
  for.
- Whether the manager replaces the existing per-agent modals or sits beside
  them; that is a UI/UX call for the owner of that surface.
- Role catalog sharing between users. Names are per owner (msg 10969) and
  nothing here changes that; a shared catalog would be a new ruling.
