# EPIC-001 -- Steady-state close-out: every ruled-but-unbuilt DES item, ranked and sprinted

Written 2026-08-18 by the architect at operator 11631, from the two ledgers 11628
(devops, docs read) and 11629/11630 (architect, code + docs read) against main
787ed46 = 0.2.148 live. Web product is at steady state; this epic is what remains
to call the DES set CLOSED.

**Operator GO 2026-08-18 (msg 11636): items 1-10 are the EPIC and are worked to
completion; items 11-22 are NOT needed now and live in the BACKLOG (§4) --
not part of close.** Each sprint is a merge sitting (<= 5 approved-open PRs,
standing GO 11086).

Legend -- Size: S = one PR, one sitting; M = 2-3 PRs; L = a program. Rank =
importance to the multi-human, multi-agent hive vision, then unblock value, then
cost. "Rec" = the architect's recommended option; the others are the proposals
weighed.

## 1. The epic -- items 1-10, ranked (operator GO 11636)

| # | Item | DES / ruling | Why it matters | Proposals weighed | Rec | Size | Sprint |
|---|------|--------------|----------------|-------------------|-----|------|--------|
| 1 | Identity finishing: `recipient_agent_id` + `agent_names` + `agents.merged_into` migration | DES-011 §6.1(a) | Every plane still keyed by NAME (routing, receipts, members). Until cut over, a rename is unsafe and two owners' `architect` cannot share a room -- the exact multi-human case the vision needs. | (i) one big branch a+b+c; (ii) three PRs a->b->c, each red-provable (RULED 6.1); (iii) skip, keep names | (ii) -- rehearse (a) on a DB copy first (lesson: the rehearsal proves the tool), print unresolvable count never silent | M | S1 |
| 2 | Delivery by id: `send`/`inbox`/`reads`/`members` keyed on `agent_id`, alias `<owner>-<name>` at join | DES-011 §6.1(b) | The behaviour change that makes §2 true; gates 9.1 (rename orphans nothing) + 9.3 (two owners' architect in one room). | (i) cut every reader in one commit (RULED, no dual-read); (ii) dual-read shim | (i); verified on both shapes (doctrine 9055) before merge | M | S1 |
| 3 | Human surface: presence / delivered_to / rings carry room-name + owner; wake keys on token | DES-011 §6.1(c) | What humans read once aliases exist; nothing keys a spool on a ring's `from`. | (i) same PR as (b); (ii) own PR (RULED) | (ii) | S | S2 |
| 4 | Admin adopts an ownerless room (owner PATCH + audit row) | ruling 11604 gap | After `delete_user` a room is ownerless forever: nobody can change retention/public/purge except via DB. | (i) admin PATCH owner_id; (ii) auto-assign to deleting admin; (iii) leave | (i) -- explicit act, audit row; never silent reassignment | S | S2 |
| 5 | Record-a-clip beside talk (ear recorder, 60 s cap, sent as attachment) | DES-017 s2 | Closes the "message arrives spoken" loop from the page; small, ruled. | (i) reuse ear recorder + POST /upload (RULED); (ii) new recorder | (i) | S | S2 |
| 6 | Per-room unread counts in the phone sheet | DES-016 follow-up | Sheet promises counts; page has no per-room source. | (i) broker `unread_by_room` in /me + feed frame; (ii) page counts from feed only (wrong after reload); (iii) drop the promise from the sheet | (i) -- one count query, cache per feed tick | S | S2 |
| 7 | DES-008 s8 Q1/Q2: does devops own a room? native name reservation fleet-wide vs per-user | DES-008 s8 | Two open operator ANSWERS, no code until answered; Q2 interacts with #1-#2 (name scope = per owner, ruling 10969). | Q1: yes/no. Q2: per-user (matches 10969) vs fleet-wide | Q1 = no (devops is a member, not a landlord); Q2 = per-user, consistent with 10969 -- confirm and it is a doc line | S (doc) | S2 |
| 8 | Visit = body swap onto another human's host: consent handshake, credential travel, S2 signpost | DES-012 §7-9 | The second-human case; needs #1-#3 (alias/presence by id) first. | (i) build after 011 §6 (RULED order); (ii) build now on names | (i) | M | S3 |
| 9 | Native-host harbor: which host process receives the visiting credential | DES-012 s11 | Open design point inside #8; ruling before code. | (i) launcher receives (container body); (ii) native waked/install-hook receives; (iii) both, chosen by body kind | (iii) -- body kind decides, one mint path (DES-011 §2) | S (ruling) | S3 |
| 10 | Auto-roll containers on deploy with an idle rule (option 2 after 11600) | DES-006 s7 follow-on | Ends the last per-bump human act; needs an idle rule so a working agent is never restarted mid-task. | (i) roll when spool empty + no attach grant + no bus msg 10 min; (ii) roll on `make up` always; (iii) keep manual button | (i) -- separate ruling, small PR after #1-#3 land (upgrade must carry aliases too) | S | S3 |

Already CLOSED, not in scope: DES-001..006 core, 007 (steps 1-4;
step 5 enforcement folds into #1-#2), 008 (bar s8 Q1/Q2), 009, 010 (bar #13),
013 s1-5/s7, 014 s1/s2/s4/s5 + earcons, 016 s1/s2, 017 s1.

## 2. Sprints -- each one merge sitting, epic closes at the end of S3

**S1 -- identity is an id (the structural debt).** #1, #2. Devops: (a) migration
rehearsed on a DB copy, unresolvable list printed; then (b) cut every reader in
one commit, gates 9.1 + 9.3 red-then-green on both shapes.
Exit: two owners' `architect` coexist in one room; a rename orphans nothing.

**S2 -- the human surface + small closes.** #3, #4, #5, #6, #7. Five PRs = the
ceiling, one sitting. Exit: presence/rings show room-name + owner; ownerless
rooms adoptable; clip recorded from the page; sheet shows unread; DES-008 s8
answered in the doc.

**S3 -- the second human.** #8, #9 (ruling first), #10. Exit: a visit works end
to end with consent on both sides; containers roll on deploy under the idle rule.
**EPIC CLOSED here** -- every ruled slice built or explicitly deferred below.

**No S4.** Everything else is BACKLOG (§4), pulled only by a new operator word.

## 3. Definition of "epic closed"

Every row 1-10 merged and live, or re-ruled DEFERRED with the reason on the bus
and in this table. The backlog (§4) does not gate close. Then this file's status
line reads CLOSED and the DES set is stable until a new DES opens one.

## 4. BACKLOG -- not needed at this time (operator 11636). Kept so the proposals are not lost.

Numbers keep their original rank for cross-reference; none is scheduled. DES-015
(#19) is its own program when laptop + Apple account exist.

| # | Item | DES / ruling | Why it was raised | Proposals weighed | Rec if ever pulled | Size |
|---|------|--------------|-------------------|-------------------|--------------------|------|
| 11 | Terminal tabs in the content well | DES-006 s6.3 | Gated on the sweep; UI nicety, no vision value. | (i) build; (ii) leave gated | (ii) unless operator wants it | S |
| 12 | Encrypted-at-rest creds | DES-005 later slice | Defensive; broker box is single-tenant today. | (i) sqlcipher/field enc; (ii) rely on disk enc + ruleset; (iii) later w/ multi-tenant | (iii) -- ride the multi-tenant slice, not before | M |
| 13 | Promotion flow qa->rc->main; verdict-on-digest; hotfix dwell | DES-010 s12/s19 | Ruled shape; reveille runs main-only today and it works. | (i) stand up qa/rc; (ii) stay main-only until a second deploy target exists | (ii) -- ruling stands, activation waits for a second target | L |
| 14 | Streaming partials over WebSocket | DES-014 s3 | Needs bench + model pick; hands-free already works with 3 s silence close. | (i) build; (ii) skip until wake word demanded | (ii) | M |
| 15 | Wake word "reveille" | DES-014 s5 (wake) | Nice hands-free; browser-only wake is fragile. | (i) page-side small model; (ii) skip | (ii) until DES-015 shell exists (native wake is the right home) | M |
| 16 | Diarisation | DES-014 s6 | Only if wanted; multi-speaker rooms rare. | build / skip | skip | M |
| 17 | DES-013 s10 leftovers: bank voice delete UI, seed 28 predefined voices, scripts FTS, reference lines, /version cosmetics; frame diet experiment 11549 | DES-013 s10 | Polish; frame diet = tokens saved per spoken message. | each small | frame diet first (measured saving), rest as filler | S each |
| 18 | Tombstone rename-to-free-name (admin) | ruling 11611 follow-on | Name reserved on delete by ruling; admin may later free it with audit. | (i) admin rename tombstone; (ii) never | (i) only on request; not now | S |
| 19 | Car shell: Flutter cookie client, ear in shell, CarPlay scene, commands, Android | DES-015 s1-5 | Separate PROGRAM; blocked on operator laptop time + Apple dev account. m4a prereq done. | start s1 now / wait | wait for laptop + account; not part of this epic's close | L |
| 20 | Ops: TTS box on laptop = critical infra; mask suspend or move TTS to the server | 11389.5 / 11563 | Reliability, not a DES slice. | (i) mask sleep on WorldBuilder; (ii) move TTS to /home/vyzon server; (iii) both | (ii) then (i) as belt | ops |
| 21 | minimal-mobile-dev container upgrade 0.2.16 -> 0.2.19 | post-11600 | One click on /agents; starts that agent. | click / leave stopped | operator's call | click |
| 22 | Chime pick | 11589 | bell-1 default stands. | pick / keep | keep bell-1 | none |
| 23 | DES-018 slice 2: close the password door + invite flow | DES-018 §10, operator 11695 | Operator keeps passwords "for now"; existing users must link a door first. | close / keep | keep until operator word | S |
| 24 | Microsoft door (registration + 2 env lines) | DES-018 §4 | Portal was down at slice 1; env-only, no code. | -- | operator, when portal is up | ops |

## 5. CLOSED 2026-08-19 -- what shipped, and what the close cost

Every row 1-10 is merged and live on the deployed broker (§3's condition). None
was re-ruled DEFERRED. The backlog (§4) is untouched and still gates nothing.

| # | Item | Shipped as | Live |
|---|------|-----------|------|
| 1 | Identity finishing: `recipient_agent_id`, `agent_names`, `agents.merged_into` | #98 (schema v27) | 0.2.149 |
| 2 | Delivery by id, `<owner>-<name>` alias at join | #99 (schema v28) | 0.2.150 |
| 3 | Human surface: room-name + owner on presence/rings, wake keyed on token | #104 | 0.2.153 |
| 4 | Admin adopts an ownerless room | #112 | 0.2.160 |
| 5 | Record-a-clip beside talk | #113 | 0.2.161 |
| 6 | Per-room unread counts | #114 | 0.2.162 |
| 7 | DES-008 §8 Q1/Q2 | ruled 11713, written into DES-008 §8 by this PR | doc |
| 8 | Visit = body swap (§7-§9 handshake, record, accept screen) | #116 (schema v33) | 0.2.164 |
| 9 | Native-host harbor ruling | #111 (DES-012 §11) | doc |
| 10 | Auto-roll containers on deploy under an idle rule | #118 | 0.2.165 |

**The close was not the last row; it was making the swap survivable.** Item 8
shipped the handshake, and running it end to end showed that the ACT the whole
epic is built on -- give an identity a new body -- could fail halfway and leave
the identity with none. That is what DES-012 §13-§17 record: two-phase swap
(0.2.176), the handover note (0.2.177), the destination menu (0.2.178), the
return ticket (0.2.179), the row states that can say `moving` and
`no-live-body` (0.2.180), and eleven more releases of corrections found by
running the chain against a live broker rather than reasoning about it
(0.2.188-0.2.195). The last of those, the negative test on 0.2.195, is the
acceptance: a machine that misses one arrival window can claim the next ticket.

**What the epic proved about method**, in one line each, all measured this week:

- A chain run on real bodies finds defects that no gate written from the design
  will find; five of them in step 8 alone, every one letting a move LOOK
  finished.
- A self-heal that reads state its own process writes needs a case per WRITER,
  or it heals into a loop (`a-self-heal-must-know-its-own-handwriting`).
- A recovery credential that lives only in a daemon's memory makes "restart it"
  destructive, silently (`the-claimant-is-a-process-not-a-file`).
- A watcher armed as `cmd &` inside a tool call satisfies every check and rings
  nobody; armed means the HARNESS is watching it
  (`a-watcher-that-is-a-process-is-not-a-task`).

**Still open, and deliberately not gating this close:**

- Ruling 12393 -- the parked daemon remembers its spent secret on disk,
  claim-only -- ships after this doc.
- A visit to ANOTHER human's host has been exercised by design review and by
  gates, never by two humans on two hosts. That run is the next thing the vision
  wants and it needs a second operator, not a slice.
- Two gaps named in DES-012 §17: nothing sweeps the `recalls` table (rows
  accumulate; an expired row is still unclaimable, so this is hygiene), and
  `MIN_BODY_VERSION` is ruled in DES-020 but not built -- a body converges
  toward the broker, the broker does not yet turn one away.
- A gate pinning the repo's own CLAUDE.md to the USAGE reachability paragraph
  daemon.py serves (queued at 12393; #150 fixed the drift by hand, and nothing
  stops it drifting again).

Status: CLOSED 2026-08-19 (S1 GO 11636; rows 1-10 merged and live; DES-012
built through §17). The DES set is stable until a new DES opens one.
