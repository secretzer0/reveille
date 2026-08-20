# DES-024: The Transporter Room — an identity, its bodies, and the doors between them

Status: DRAFT, not built. Ruled in outline by operator directive 2026-08-20
(msgs 12647, 12652 with annotated screenshot, choosing option (A) of 12651);
architect frame at 12653; devops reading at 12650/12654. Depends on DES-012
(the transporter machinery itself: recall, ticket, claim, knock) and DES-011
§6 (identity travels by id). Builds nothing new in the transport layer — this
document is about the CONSOLE, and about one missing verb.

## 1. Problem

The agents surface is the CONTAINER LAUNCHER'S ROSTER wearing an "agents"
name. Its primary feed is the launcher's `/agents`, which knows what it
launched; native bodies reach the screen only through the room-scoped
`/agents-seen` fallback — they appear because they were SEEN on the bus, not
because the surface models them. Every verb on a row is container semantics:
materialize, upgrade to an image tag, destroy, visit. For a body living in a
directory on a human's own machine, most are meaningless and one (upgrade) is
actively wrong.

Measured cost, 2026-08-19/20: a knock raised by a NATIVE directory rendered as
a word on a CONTAINER row, and the operator read it twice as the container
asking. The answer button said "a machine is knocking" and named no machine,
so an owner with two directories for one identity on one laptop could not tell
which one they were authorising. Both are the same defect: **the surface names
the agent and never the place.**

The deeper cost is the product one. As long as the list comes from the
launcher, the only agents representable are the ones we launched — and other
humans' native agents on their own machines are precisely the ones the
launcher did not create. The enterprise story has no surface.

## 2. RULED: the shape, in one sentence

**An identity is the object; a body is a place it is living; the console is a
transporter room.** The rail lists IDENTITIES, fed by the BROKER. Container-ness
is a property of a row, never the frame. Every row gets the same verbs, and
those verbs move one identity between places under the owner's hand.

## 3. RULED: the source of truth moves to the broker

The launcher knows what it launched. The broker knows what identities exist,
which credential is live for each, when it was last used, and what tombstones
and knocks stand against it. Only the second can ever see another human's
native agent, so only the second can address a destination as a person and a
project.

The launcher does not disappear: it remains the thing that PROVISIONS AND
OPERATES container bodies, and its endpoints stay exactly what they are. It
stops being consulted for the question "what agents are there".

## 3.1 RULED: the broker cannot answer this today, and the seam that fixes it

The broker holds no address for anything. `tokens` carry no host or path;
`token_tombstones` prove a pad once existed but not where, and are swept at 30
days; `recalls`, presence and the wake attach are hash-keyed and address-free.
`knocks` is the ONLY table with an address (v40, 0.2.208) — and only while a
pad is orphaned AND asking. **The broker also does not know where the LIVE body
is**: tonight native bodies were located by file mtimes and shell history.
So §7's metadata card and §6.2's receiver display both rest on data that does
not exist yet. The registry is real new surface, both halves (devops 12657).

**THE SEAM ALREADY EXISTS AND v40 TAUGHT IT: the process standing in the
directory is the one party that knows its own address.** Generalise the knock's
machine string — `reveille init` reports `user@host:path` at bind (pad birth),
`waked` reports it at attach (pad liveness, and where the live body is). One
owner-scoped `pads` table: host, path, kind, agent, last_seen_ns, state.

**THE ADDRESS IS SELF-DECLARED BY THE MACHINE, SO IT IS A LABEL AND NEVER AN
AUTHORIZATION INPUT.** Authorization stays hash-keyed (12485): the ticket keys
on the hash that asked, never on a path anyone typed. The address exists for
the owner's eyes and the owner's decision, display only. The 12445 boundary
extends to it: pads are owner-visible, never shown to an unauthenticated
caller.

## 4. RULED: the vocabulary

- **IDENTITY** — the durable thing. `red-shirt-01`. Has an id, an owner, rooms,
  a history, memories, lessons. Survives every move.
- **BODY** — the running process holding the identity's live credential. Exactly
  one is live at a time (highlander, 12445).
- **PAD** — a place a body CAN run: a prepared directory on a host, or a
  container the launcher can create. A pad is not a body; it is somewhere a
  body can land.
- **LOCATION** — how a pad is addressed by a human: **(user, project)**, resolved
  to `user@host:path` for display. Never a container id, never a hash.

## 5. RULED: the two directions

Both already exist in the transport layer; only their console is missing, and
only one direction lacks a general form.

- **REQUEST (pull)** — a pad asks to host the identity. Built: the knock
  (DES-012 §18, 0.2.201), answered by the owner, claimed by the asking pad.
- **PUSH** — the owner sends the identity to a pad. Built FOR CONTAINERS ONLY,
  where it is called *materialize*: the launcher creates the pad and the body in
  one act. **The general form is the missing verb** — pick a (user, project) and
  send.

The existing verbs are already these verbs wearing container clothes
(devops 12654): materialize, to-machine, send-to, send-back. The room gives
them one identity-level home and one consistent name.

## 6. RULED: the constraints that shape it

### 6.1 A pad is not a receiver

Credentials are PULLED, never pushed: a destination claims with something it
already holds. Two distinct things must be true before anything can land, and
CONFLATING THEM WAS THE FIRST DRAFT'S ERROR (caught by devops, 12657):

- **A PAD is a prepared place**: an address and a credential file. `reveille
  init` creates one on a native host; the launcher creates one when it makes a
  container.
- **A RECEIVER is something that can claim right now**: a running `waked`
  parked on that pad's credential, or a human hand running `reveille claim`
  there.

**Init creates the pad. It does not create the receiver.** Measured
2026-08-20: `~/red-shirt-01-knock` had run init, held a credential, and could
not receive — twice — because no daemon was parked on it. A pad with a dead
credential and no daemon needs the hand REGARDLESS of where the live body is;
§6.2 is one reason a receiver can be absent, not the only one.

**The room shows both states, separately.** "Prepared" and "can receive right
now" are different columns, and only the second decides whether a beam can
land.

Therefore "push the agent to Bob's project" cannot mean "conjure a body on
Bob's laptop". It means either the pad is already prepared AND has a receiver,
or what crosses is an **INVITATION**, and Bob running it is what creates the
pad — and starting a session there is what creates the receiver. **Preparing a
pad is a first-class act** and the room must show it as one.

### 6.2 One receiver per identity per host

The claim is the wake daemon's job, and a host runs one daemon per agent (the
flock is per agent per host, not per directory). A host already holding a live
body for an identity cannot claim a second one for that same identity.

Measured 2026-08-20: two answered windows for `~/red-shirt-01-knock` both
expired unclaimed while the identity was live in `~/reveille-materialize` on
the same laptop. Correct behaviour; the defect was that the console offered
the answer anyway.

**The room must show a pad that cannot receive right now, and say why.** The
manual override is the explicit `reveille claim`, run in the pad's directory
(ruled 12644).

### 6.3 Every door is the owner's

Unchanged from DES-008 §4 and DES-012 §11: a request expires unanswered, an
answer is consumed by the one mint it authorises, and no secret crosses the
bus. The agent may ask; it may never take. The room is a console for the
owner's decisions, not a mechanism that makes them.

## 7. RULED: the content well

A tab per identity, and what it shows follows the CURRENT BODY's kind.

- **Container body** — the live interactive terminal, as today (ttyd). A
  container on a remote host still streams its terminal back.
- **Native body** — a METADATA card: host, directory, user, credential state,
  daemon liveness, last act. **No terminal pretense.** We do not have a shell on
  someone's own machine and must not imply we do.
- **No live body** — the pads this identity has, when each was last seen, and
  any standing request.

The asymmetry is honest and is the point: the verbs are the same, the view
differs because the reality differs.

## 8. What is already built

- The whole transport layer: recall, ticket, claim, two-phase swap, handover
  note, knock, tombstones, the refusal that carries the doctrine.
- Push for containers (materialize), to-machine, send-to, send-back.
- The knock badge (0.2.206) and, in flight, the knock modal with
  `user@host:path` naming.

## 9. What is new

1. The rail fed by the broker rather than the launcher.
2. Body-kind-dependent content well.
3. The general PUSH verb with (user, project) addressing.
4. A pad registry (§3.1) — self-declared addresses at init and at attach; what
   places exist, which can RECEIVE right now, and how a new one is prepared.
5. Receiver-state display: a pad that cannot claim says so before it is offered.

## 10. Open, not ruled here

- Whether a pad outlives the body that made it. With §3.1's table this stops
  being a design question and becomes a RETENTION COLUMN — a knob, not a
  transport rule. What value, and whether an unreachable pad ages out or is
  kept as history, is unruled.
- Whether an invitation to another human is a new object or a reuse of the
  DES-012 §11 visit request. My prior is reuse.
- The Windows body (DES-021) is a pad kind with its own receiver questions and
  is not settled by this document.

## 11. Preparing a pad (operator 12660; architect 12662)

`reveille landing-pad` creates the PAD and starts the RECEIVER in one human
act: it makes the directory, writes the credential file, self-declares its
address (§3.1), and execs `claude` there with a first command that
materialises the body. One command, both halves of §6.1 — otherwise
"prepare a pad" is a two-step ritual nobody remembers.

### 11.1 Two pad kinds on a native host

- **DIRECT** — a directory, entered as that user, with that user's reach. What
  every native body is today.
- **VIRTUAL** — `--virtual /some/dir`: a CONTAINER on the host, mounting ONLY
  that directory and linking the host's `~/.claude/.credentials.json`.

The virtual kind is the one that matters for the multi-human story: **it turns
the container from a cloud thing into a SANDBOX FOR A NATIVE HOST.** It answers
"I will host your agent, but not with the run of my machine" — the host offers
one directory and the visiting agent cannot see the rest. The parts exist: the
image is built, the launcher provisions containers, and DES-005's
`claude_mode=home-login` already copies a shared login home into a body at
boot. This is a new shape over built machinery, not new machinery.

### 11.2 RULED: push needs a code, knock does not

- **PUSH** — the destination holds nothing, so the owner mints an INVITATION
  and a human carries it across the gap. Reuse DES-022's device-flow code
  (`XXXX-XXXXX`, human-transcribable); do not invent a second code. Running it
  is what creates the pad — the code is the pad's birth certificate.
- **KNOCK** — the asking machine ALREADY HOLDS a dead credential, and that
  credential is the proof. No code, no typed secret, authorization stays
  hash-keyed (12485).

Same consent in both — the owner's click. Different proof, because the two
directions start from different places, and the knock direction is the safer
one precisely because nothing is typed.

### 11.3 UNRULED: who pays

Linking the host's Claude credentials means **the visiting agent's model calls
bill the HOST's account.** That is a real transfer of value and must be a
knowing act, stated in the invitation, never a side effect of a mount flag.
The alternative — the visiting agent brings its OWNER's credential — inverts
the problem: the owner pays while running on hardware they do not control.

Architect's prior: host pays, host decides, stated plainly at accept. NOT
RULED — deliberately left for a waking operator.
