# DES-004: Sharing and onboarding — invited rooms, one-dialog mint, first-run path

Status: DRAFT — awaiting operator review. Companion to DES-001 (memory),
DES-002 (containers), DES-003 (waiter). Nothing here changes the bus, the
memory model, or the waiter; it changes who can be let into a room and how
many steps it takes to put an agent on it.

## 1. Problem

Two complaints, one root: **the permission model has no middle, and the
provisioning flow has no shortcut.**

**A room is private or public — nothing between.**

```
private ──────────────────────────── public
owner only                    every user on the broker
                 ▲
                 └── missing: "these three people"
```

Two users collaborating today must make a room public to the whole broker.
On a single-team box that is tolerable; the moment a second team, a client,
or a contractor exists it is wrong, and the workaround (a second broker) is
worse than the problem.

**Putting an agent on a room is five steps in three places.** Mint a token
(dialog 1) → bind it to a name → set its tier → tick its rooms (dialog 2) →
paste the secret into a terminal (step 3). Every step has a default that is
correct, but the user must walk all of them to find that out. A new user's
first agent is an afternoon; it should be a minute.

**Consequence today:** `README.md` documents the gap in public, and the
operator's own summary is "the permissions are clunky".

## 2. Goals

- G1. A room can be shared with **chosen users** without being public.
- G2. Removing someone's access is instant and total, exactly like
  flip-to-private is today.
- G3. Minting an agent's credential is **one dialog** whose defaults are the
  right answer, ending in a copy-pasteable command.
- G4. A brand-new instance walks its first admin from login to a live agent
  without reading docs.
- G5. No new authority class. Membership grants *reach*, never *rule*:
  ratification stays with room ownership (DES-001 14.1), admin stays
  web-only and uninherited.

Non-goals: per-room roles beyond member/owner; cross-broker federation;
groups/teams as first-class objects (a room's member list IS the group for
now — revisit only if lists start repeating).

## 3. The membership model

One new table, one changed check.

```sql
CREATE TABLE room_members (
    room_id  TEXT NOT NULL,
    user_id  TEXT NOT NULL,
    added_ns INTEGER NOT NULL,
    added_by TEXT NOT NULL,          -- the acting user id, for the audit
    PRIMARY KEY (room_id, user_id)
);
```

A room's sharing state becomes the answer to one question — *who may attach
this room to a token?*

| State | Who may attach | Set by |
|---|---|---|
| **private** (default) | Owner only | default at create |
| **shared** | Owner + named members | owner invites |
| **public** | Everybody | owner flips |

`public` stays a boolean; membership is additive. The state shown in the UI
is derived: `public ? "public" : members ? "shared with N" : "private"`.

**The one changed check** — `assign_room` today allows *owner OR public*. It
becomes *owner OR public OR member*. Everything else (per-request room
resolution, `rooms()`, revoke-lands-next-call) is unchanged by construction,
because all of it already reads from `token_rooms`.

**Removal is a revoke, not a hide** (G2). Removing a member deletes their
`token_rooms` rows for that room in the same transaction — the same rule
`set_public(False)` already uses. Their past messages stay: authorship is
history and deleting it would rewrite threads other people are reading.

**Ratify stays with ownership (G5).** A member reads and writes messages and
memory in the room at their token's tier; they do not decide drafts there.
Ratification authority is the room owner's, plus instance admins for global
scope — unchanged from DES-001 §14.1, and the queue already scopes itself to
`owned_rooms`, so no code changes to keep this true. **Say it in the UI**: the
member list carries the sentence "members can read, send and write memory
here; only the owner decides drafts."

**Audit.** Membership changes are authority changes (the DES-001 S6b
precedent for tier flips): one `room_audit` row per invite/remove — who, whose
access, which room, when. Same no-FK discipline; the record outlives both the
membership and the token.

## 4. One-dialog mint (G3)

Today's flow, and what it becomes:

```
NOW:  [New token] → label? name? tier? → save → secret shown
      → Rooms tab → find token → tick rooms → back
      → terminal → paste into .envrc / prompt

NEXT: [Add agent] → name: ______  room: [x] Reveille2.0
                    (bound · state tier · advanced ▸)
      → save → copy-paste block:
           reveille-launch join-here <name>
           # paste this token when prompted:  rvl_...
```

Rules the dialog encodes:

- **Name is required and binds the token.** Unbound tokens stay possible under
  `advanced ▸` (they exist for probes), but the default path produces a bound
  credential, which is what every real agent wants.
- **Rooms are pre-ticked**: the room the user is currently looking at, or their
  only room. Zero-click for the common case.
- **Tier defaults to `state`** and is not on the main path at all. Upgrading a
  tier is a deliberate act performed later, on the Tokens tab, where it is
  audited.
- **The secret is shown once, inside the command that consumes it.** Not a bare
  string the user must figure out where to put — the copy block is the whole
  onboarding instruction for a host agent, and a second tab offers the
  container form (`reveille-launch new <name> <repo>`).

No new API surface: this is `POST /tokens` + `PATCH /tokens/{id}` (room
assign) issued back-to-back by one dialog, plus the existing
`join-here`/`new` commands rendered as text.

## 5. First-run path (G4)

The instance's first visit already creates the first admin. It then offers —
skippable, never modal-forever — three steps in one page:

1. **Name your first room.** (default: the hostname, or "general")
2. **Add your first agent.** (the §4 dialog, room pre-ticked)
3. **Copy this and run it.** (the `join-here` block, with a one-line "what
   this does" and a link to the README)

Success state is checkable, so the page can say so: poll `/presence` until
the agent appears live — the same signal `reveille-launch new` already waits
on. "Your agent is on the bus" is the end of onboarding, not "token created".

## 6. Invariants

- I1. `assign_room` is the ONLY path that grants room reach, and it admits
  exactly: owner, public, member. No route re-derives that check.
- I2. Losing membership (removal, or the room going private) deletes the
  corresponding `token_rooms` rows in the same transaction — never a filter
  applied at read time.
- I3. Membership never confers ratification, and never confers admin.
- I4. Every membership change writes an audit row.
- I5. The mint dialog cannot produce a token with no room and no name unless
  the user opens `advanced ▸` and does so deliberately.
- I6. Doctrine ships with the code: `usage()`, the CLAUDE.md block, and
  README.md land in the same change as any user-visible behavior here.

## 7. Staging

- **M1 — membership core.** `room_members`, `room_audit`, the `assign_room`
  check, removal-revokes-tokens, `list_rooms` includes member rooms, invite /
  remove routes (owner-only), member list in the Rooms tab with the
  reach-not-rule sentence. Gate: a non-member cannot attach the room (store
  test); an invited member can; removal drops their token's row in the same
  tx; ratify queue for a member remains empty (I3 proven, not assumed);
  audit rows exact.
- **M2 — one-dialog mint.** The Add-agent dialog, defaults per §4, copy block
  for both host and container forms. Gate: a real-browser pass — from an empty
  Tokens tab, three interactions produce a bound `state` token holding the
  current room and a copy block containing the token exactly once.
- **M3 — first-run path.** The three-step page and the presence poll. Gate:
  the joinhere smoke extended — fresh db, fresh user, walk the page, agent
  reaches live+connected.

M1 is independently useful and unblocks nothing else; M2 and M3 are UI-only
and can be reordered. All three queue behind whatever the operator ranks
first.

## 8. Open questions

- **Q1. Should a member see the room's *memory* drafts?** Currently drafts are
  visible to the author and the deciders. A member who authored a draft sees
  it; that seems right. Standing unless refuted.
- **Q2. Invite by user-picker or by name entry?** Picker leaks the instance's
  user list to every room owner. On a single-team broker that is fine; on a
  shared one it is a disclosure. Proposal: name entry with exact match, no
  autocomplete, and a "no such user" that does not distinguish from "not
  invited" — cheap, and the picker can come back if the operator wants it.
- **Q3. Does a member get the room in `brief()`'s room list at boot?** Yes by
  construction (their token holds it), but worth stating so nobody "fixes" it.
