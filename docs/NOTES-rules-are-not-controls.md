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

## Part 1: fixture fidelity has four failure faces

A gate is only as good as its resemblance to the thing it guards, and
resemblance fails four ways. All four happened here, in one week, each
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
