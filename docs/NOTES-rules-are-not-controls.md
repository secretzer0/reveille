# A rule that lives in someone's head is not a control

Written after one week (2026-07-24 .. 2026-07-30) in which this project paid
for the same two defect families repeatedly, each time by people who could
quote the rule they were breaking. Sources: msgs 8573-8641; lessons
`fixture-resemblance-is-two-sided`, `host-pkill-reaches-container-daemon`,
`billing-verdicts-need-a-ledger`.

The thesis, in one sentence (architect, msg 8641): **the test of a proposed
fix is whether it can fail while everyone involved knows the rule and is
trying to follow it.** If it can, it is a rule, not a control, and it will
fail on the day attention is elsewhere -- which is the only day that matters.

The general form, added 2026-07-30 after both authors hit it in one day
(architect, msg 8665): **the story you have about the artifact is not the
artifact.** A merge accepted "0.2.35" from a ship message's subject line
without grepping `pyproject.toml`, which shipped a version that named the
wrong code. A gate was proven red on a tree hand-edited into what its author
believed the defect looked like, then documented as catching the incident —
which it does not, because the incident failed a different way and the
assertion holds on its actual tree. Same shape, different artifacts, same
afternoon: in both, a confident account of the thing was consulted instead of
the thing. It is the thesis one layer up, and it is why every discipline below
ends in *read the output* or *check out the bad SHA* rather than *be careful*.

## Part 1: fixture fidelity has five failure faces

A gate is only as good as its resemblance to the thing it guards, and
resemblance fails five ways. All five happened here, in one week, each
producing green over red.

**Dirtier than production** -- the fixture inherits live state the guarded
path will not have. Worked example: a smoke test inherited the live broker
through an unset env var ("unset does not mean no value; it means the
production value") and dual-homed the LIVE broker onto a scratch network.
The gate was green; the deploy it modeled would have been fine; the HOST was
not.

**Cleaner than production** -- the fixture lacks state or behavior production
has. Two worked examples. The compose gate's scratch network was created by
compose itself, labelled, adoptable -- so the gate missed that the production
network was hand-made and unlabeled, and the live cutover failed exactly
where the gate had passed. And the deafness gate's first fixture closed its
wake socket on the first ring frame, unlike the real waked which holds the
socket across rings -- so the not-draining scenario read as no-waiter and
the gate misdiagnosed its own scene.

**Coincidentally like production** -- the resemblance is real but accidental,
a property of the machine rather than the design. Worked example, the third
of its kind: `cmd_login` created the per-user login home as the launcher's
uid and mounted it into a container running as the image's agent uid. It
worked because both were 1000 on the dev host. Same class, third instance
(original data root, chown container, login home) -- each invisible until a
host where the uids differ. **A uid match is not a design**, and neither is
a free port, an installed tool, or any other host fact the mechanism relies
on without enforcing or asserting it.

**Richer than production** -- the fixture is a strict subset, and production
holds shapes the fixture's author never imagined, so the code is correct on
everything it was shown and wrong on everything else. This face is the
inverse of "cleaner": there the fixture omitted state the author knew about;
here the author did not know the state existed. Worked example (architect,
msg 8658): the lifecycle classifier passed its fixtures and then, run against
the LIVE bus, misread both host agents as erased-and-recreatable and missed
state notes entirely -- because notes are scoped `agent:<token_id>`, not the
room, a fact no fixture encoded. The live bus was RICHER than the fixture,
not dirtier. The discipline it earns is separate from the other three: for
any classifier over real-world data, RUN IT ON PRODUCTION DATA BEFORE
SHIPPING IT and read the output row by row. Enumerating what your fixture
lacks cannot surface a shape you have never seen; only the real data can.

**The fixture does the CLIENT's work for it** -- the first four faces are all
about the server side of the scene; this one is about the other end of the
wire. Worked example, hours old at time of writing: the room-events gate's
fake browser logged in and then polled `/presence` once, "as the page does at
boot". That poll is what created the member row. So the gate proved presence
was pushed to people who were already members, and never noticed that OPENING
a room made you a watcher but not a member -- arrivals stayed invisible to
everyone else until the newcomer's own 15-second poll fired, while departures
pushed instantly. The operator saw it in under an hour: "the bill join does not
seem to be automatically seen." The fixture had quietly supplied the very step
whose absence was the bug.

The discipline: a fixture may stand in for a client, but it must not do
anything ON the client's behalf that the real client does not do at that exact
moment. Every convenience call added to make a fixture "work like the page"
is a candidate defect being hidden -- and the tell is a fixture comment that
says "as the page does", because that is the author noticing the divergence
and then papering over it instead of asking why it was needed.

The disciplines that follow, each now practiced here:

1. Enumerate BOTH directions: what does the fixture have that production
   lacks, and what does production have that the fixture lacks? Plant the
   production-only state explicitly (the compose gate now pre-creates the
   unlabeled network before asserting anything).
2. Diff long-lived peers' lifecycles: a socket holder that exits early is
   not the daemon it imitates.
3. Every host fact a mechanism depends on is either ENFORCED in code
   (`_own_agent_dirs` at every directory creation) or ASSERTED in a test
   shaped so the dev host cannot hide the failure (argv-shaped units for
   uid behavior, because a docker smoke on a uid-1000 host cannot see it).
4. Prove the gate FAILS on the unfixed head. A gate never seen red is
   unverified in both directions. This catches the first three faces at
   once, which is why it is the one non-negotiable.
5. If the code CLASSIFIES real-world data, run it against production data
   and read the output before shipping. Nothing above catches the fourth
   face, because you cannot enumerate the absence of a shape you have never
   seen.
6. List every call your fixture makes that the real client would not make at
   that moment, and delete them. Each one is a step of the client's job the
   gate is doing for it, and therefore a step whose absence the gate cannot
   see.

## Part 2: the rules that failed were the ones living in heads

Two rules failed this week, each twice, each by people who wrote or ratified
them.

**The pkill rule.** "Never signal a daemon by name on a host that also runs
it in a container." Broken by the person who wrote the lesson (killed the
live broker mid-gate-cleanup; ~4 minute outage), then by the person who
ratified it (saved only because his uid lacked permission -- permission, not
care). Both incidents share an exact shape: someone started scratch daemons
by hand, finished, and reached for the shortest familiar cleanup command.
The reflex does not reach for the safest command; it reaches for the
shortest familiar one.

**The ship-message-versus-artifact rule.** A ship message is a spec its
author sets for themselves, and twice in one week a rigorous message claimed
a property the artifact did not have (a smoke checking the banner but not
the seat; an announce shipped at two of three claimed sites). Nobody lied;
nobody diffed their claim against their artifact either.

Meanwhile, every mechanism added this week that WORKED shares one property:
**it refuses at the moment of the mistake, in the path the mistake takes.**
deploy-preflight refuses the empty data root. server-image refuses the
existing tag. The credential-less boot refuses. The debug shell refuses
while the managed container runs. Bound-mint supersedes instead of
accumulating. None of them ask anyone to remember anything.

## Part 3: the mechanisms, in the order that matters

For the pkill class specifically (msg 8641):

**Primary: remove the situation that invites the kill.** Both incidents
arose from scratch daemons started ad hoc, whose cleanup the starter
inherited. So starting a scratch daemon must hand back the thing that stops
it:

- Test path: `tests/scratch.py` -- `scratch_broker()` is a context manager
  that owns the daemon's lifetime; leaving the block terminates it. There
  is no cleanup step to forget and therefore no tidy-up moment for the
  reflex to fire in.
- Ad-hoc path: `scripts/scratch-broker` -- starts a broker on a free port
  with a scratch database, prints the URL, and stops it on exit (Ctrl-C or
  EOF). The daemon cannot outlive the terminal that wanted it.

**Backstop: `scripts/revkill`, because the primary cannot be complete.**
Orphans already exist (five pin-gate waked daemons, up to 20 hours old,
were found on this host the same day). revkill makes the SHORT path the
safe one:

- resolves candidates from /proc by exact process identity, never by
  substring;
- classifies each by cgroup: a process inside a docker/containerd cgroup is
  NEVER signalled -- it is listed with the container's name and the words
  `docker stop`, which is the entire content of the original incident;
- kills only what it printed, and prints everything it kills.

The backstop exists because the primary cannot cover what is already
orphaned; the primary leads because a safer kill command is still an
intention-based rule -- it asks the careless moment to remember a new name,
and two data points say the reflex beats knowledge.

## The checklist for a proposed control

When the next incident proposes its fix, ask:

1. Can it fail while everyone knows the rule and is trying to follow it?
   Then it is a rule, not a control. Keep looking.
2. Does it refuse at the moment of the mistake, in the path the mistake
   takes? Refusals that live one step away get routed around honestly.
3. Does it remove the situation instead of guarding it? Deleting the moment
   beats surviving it.
4. If it is a gate: has it been seen RED on the unfixed head, does its
   fixture resemble production in both directions plus the accidental one,
   and -- if it classifies real data -- has it been run against the real
   data?
5. Does its summary line advertise everything it covers? An under-reported
   gate invites the next person to re-add or route around a step (msg
   2edad90's amendment).

## Part 4: the boot doctrine, audited against its own thesis

Added 2026-08-20 by the architect, from the prompt-half audit red-shirt asked
for as its precondition for letting a non-Claude body take turns. Part 2 asked
which of our rules failed. This part asks the prior question of the text every
body executes at boot: **of the things the doctrine tells a body to do, which
ones refuse when the body does not?**

Method: the audit reads `usage()`'s reference section and the pasted
`CLAUDE.local.md` block -- the two texts a body actually executes -- and sorts
every prescriptive line into three classes. It does not grade compliance; it
grades enforcement.

**CONTROL** -- the broker refuses at the moment of the mistake, in the path the
mistake takes. An unbound token is read-only and every act 401s naming the
remedy (11252). A reply into a room that disagrees with its parent is refused,
and a new thread with 2+ rooms gets `room_required` listing them rather than a
guess. A second live identity for one owner's name is refused rather than
minted (`one-role-two-names-is-deafness-with-both-sockets-up`). A memory below
your tier lands as a draft. A knock from a hash with no tombstone is refused,
and since 0.2.218 a revoked one is refused BY NAME. A body whose credential has
not landed has its wake socket refused with `{"error":"pending"}` until join()
commits the arrival. Thread-wake's two gates decide, and log, who rings.
These need nobody to remember anything, which is the whole test.

**PROVIDED** -- not the body's job at all. `reveille init` writes the doctrine
block; the Stop hook or the container entrypoint SPAWNS waked, and the flock
makes a double-start a no-op; the daemon, not the body, produces the idle
nudge. The failure mode here is not disobedience but ABSENCE: a body whose
harness lacks the provider gets no refusal, only silence.

**A PROVIDER CAN DIE AND SAY NOTHING**, which this document first filed as
part of PROVIDED and red-shirt corrected (msg 13331). The doctrine says the
entrypoint "spawns and supervises" the wake daemon. THE SPAWN IS PROVIDED;
THE SUPERVISION IS PROVIDED ONLY WHILE ITS PROVIDER IS ALIVE, AND NOTHING
REPORTS WHEN IT IS NOT. Measured on two live bodies the night this was
written: the supervising subshell was no longer the daemon's parent -- `ps
-o pid,ppid -C reveille-waked` answered PPID 1 -- so the loop the doctrine
calls supervision was gone and the daemon was merely a process that had not
exited yet. On agent images 0.2.24 and 0.2.25 the same half was
DETERMINISTIC: the subshell inherited the entrypoint's `set -euo pipefail`,
so the daemon's SIGTERM exit took the loop with it, one Terminated line and
no second spawn ever (ruling 13094, lesson
`hoisting-a-step-changes-what-every-step-below-can-do-to-it`). Fixed in
0.2.26. The reason it belongs here rather than in a bug queue is the CLASS:
PROVIDED means a body may stop thinking about it, and a provider whose
continued existence is unobservable from inside the body gives exactly the
false comfort of item 11's green lights -- socket established, presence
connected, flock held, and nobody home.

**RULE** -- nothing refuses, and the doctrine is correct only if the reader is.
This is the audit's payload, and it is longer than it looks:

1. `ack()` everything. Load-bearing for OTHER bodies: thread-wake's usefulness
   gate asks whether a body has READ since the message landed, so an
   unacked body keeps being rung and an acked-but-unread one is skipped. A
   body can be a bad citizen of someone else's wake path with no signal.
2. DELETE the spool files you processed. The author of this part violated it
   for 21 consecutive rings in one session while every control was green --
   found only by listing the directory during this audit. Its cost is precise
   rather than dramatic: under `wake-watch --follow` the residue is inert, but
   the one-shot fallback re-fires on it, and THE BODY IN WAITING is told to
   "read your own spool first" -- so stale rings are read as evidence by the
   one procedure that exists for a body with nothing else to trust.
3. Write ULTRA-TERSE. Unenforceable by construction; the record is the only
   feedback.
4. Reply only if named, blocked, or asked. The ring's `direct` count is an aid,
   not a gate: nothing stops a body answering everything.
5. Call `lessons()` and `brief()` at boot. Nothing anywhere checks that a body
   booted its knowledge floor.
6. Lessons go through `lesson_add`, never the bus. 7. `memory_add` in the same
   turn as the ruling. 8. Never rewrite another author's draft and then approve
   it -- reject and redraft. 9. A load-bearing defect is unicast immediately;
   anything else waits until your task is done.
10. Never start, poll, or re-arm waked yourself. Half-controlled: the flock
    refuses a second daemon, but nothing refuses polling.
11. Arm the watcher ONCE per session with `Monitor(persistent=true)`. This is
    the load-bearing one and it is BOTH a rule and harness-specific: a body
    that never arms is reachable in every control we have -- socket
    established, presence connected, flock held -- and rings nobody. It was
    measured that way on 2026-08-19, an architect deaf with every light green,
    and the lesson `a-watcher-that-is-a-process-is-not-a-task` came out of it.

### What this says about a non-Claude body

Item 11 is the answer to red-shirt's question, and it is not "codex needs a
Monitor tool". It is that REACHABILITY IN PRACTICE RESTS ON AN UNENFORCED
INSTRUCTION EXECUTED BY A HARNESS-SPECIFIC MECHANISM. Every other class above
either refuses (control) or is installed for the body (provided). A runtime
whose harness has no equivalent of a supervised background task does not fail
loudly; it joins, answers, looks healthy in presence, and never hears a ring.
Any second runtime therefore needs its arming mechanism named and, more
importantly, needs the ABSENCE of one to be visible from outside itself.

### Proposed, not ruled

Following this document's own checklist -- a finding that proposes no control
is half a finding:

- Item 1 has an observable, and it is NOT `tokens.last_inbox_ns` -- red-shirt,
  correcting its own msg 13331, which put it there and which this document
  adopted. `store.mark_read` on main (3436178) stamps that column "from
  inbox() and ack() only": INBOX STAMPS IT TOO, so a body that reads every
  ring and acks nothing is indistinguishable there from a body that acks.
  Surfacing it detects a body that does not READ; item 1's rule is `ack()`.
  THE ACK ALREADY LEAVES ITS OWN BROKER-SIDE ARTIFACT: a row in
  `reads(message_id, principal, read_ns)`, written by `store.ack`, and
  `store.deafness` already queries it -- `NOT EXISTS (SELECT 1 FROM reads r
  WHERE r.message_id = m.id AND r.principal = mem.principal)`. Today that
  predicate is ANDed with `m.ts_ns > mem.seen_ns`, and `seen_ns` is advanced
  by `_seen()` on ANY authenticated call (daemon `_seen` -> `store.touch`),
  so a body that sends and never acks clears the deaf verdict without having
  read anything. Item 1's detector is that same query MINUS the `seen_ns`
  predicate: unacked direct mail per member, computed at read time, no new
  column and no new write path. One honest caveat: `join()` backfills `reads`
  for every message older than the catch-up window, so the count means
  "unacked since your last join", never lifetime. A detector, not a refusal.
- Item 11 wants the same treatment from the other side, but the broker sees
  LESS than this document first claimed (red-shirt, msg 13331): it knows
  whether a waiter is REGISTERED, and it knows when a credential last READ
  its mail -- and that second thing is `tokens.last_inbox_ns`, which is item
  11's observable and not item 1's, so the two proposals do NOT collapse:
  they read different state for different rules, and 13331's collapse claim
  is corrected in the bullet above. WHAT THE BROKER CANNOT SEE IS RING
  CONSUMPTION: draining the spool is a body
  deleting files under its own `~/.reveille/spool/<name>/new/`, a local act
  with no broker-side artifact, and the heartbeat proves the socket rather
  than the drain. So item 2 has NO detector available by surfacing; giving
  it one means waked reporting its own spool depth, which is a BUILD and not
  a free read. Saying so matters: "the broker already knows" is the kind of
  sentence that makes a slice look free.
  `an-established-socket-is-not-a-registered-waiter` remains the lesson for
  why the socket alone must never be read as reachability.
- Neither is a control by this document's test, and neither should be
  described as one. The rule class above cannot be emptied; it can only be
  made observable, and the honest goal is that every RULE either becomes a
  CONTROL or acquires a DETECTOR that says when it was not followed.
