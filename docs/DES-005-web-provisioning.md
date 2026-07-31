# DES-005: Web provisioning — spawn agents from the browser, multi-tenant

Status: DRAFT — operator-directed (2026-07-29), awaiting review.
Supersedes DES-002's hosting non-goal (§1 below). Builds on DES-002
(containers, grants), DES-003 (waiter), DES-004 (rooms, mint UX).

## 1. The reversal, stated

DES-002 §1 and its non-goals rejected hosting other people's agents —
*credential custody, runaway spend, miner magnet*. **The operator reverses
that on 2026-07-29.** Reveille becomes a service where a signed-up user spawns
their own agent containers from a browser.

The three rejected risks do not disappear; they become requirements:

| Old risk | Now handled by |
|---|---|
| Credential custody | Users bring their **own** subscription token — reveille never holds an Anthropic billing relationship (§3) |
| Runaway spend | Same: spend rides the user's own subscription and their own rate limits |
| Miner magnet | Per-user quotas, egress policy, container caps — **P0, before any non-invited user exists** (§6) |

DES-002's scope paragraph and non-goals are amended in the same change that
merges this doc, so the corpus stops contradicting the product.

## 2. Shape

```
browser ──┬── broker (reveille-server) : login, rooms, memory, chat, MINTS TOKENS
          └── launcher (reveille-launch): agents tab, docker, credentials
                        │
                        └── docker ── rev-<user>-<agent> ── tmux+ttyd+claude
                                          ~/.claude ─┐
                                          ~/repos  ──┴─ bind: data/<userid>/<agent>/
```

- **The broker still never learns docker exists** (DES-002 G4, smoke-guarded).
  It mints tokens and serves rooms; it does not provision.
- **The launcher grows an HTTP API + UI.** The browser talks to both services;
  the Agents tab is served by the launcher.
- **Nothing is spawned on remote hosts.** Recorded as future vision (§8), not
  built.

## 3. Credentials

**Claude: the user's own subscription token.** One browser step, once:

```
user, on their own machine:   claude setup-token      → 1-year token
user, in reveille profile:    paste it                → stored, 0600
every container they spawn:   CLAUDE_CODE_OAUTH_TOKEN → zero-touch forever
```

- Reveille holds **no Anthropic billing relationship for anyone**. Spend and
  rate limits are the user's own — N agents share one subscription's limits,
  exactly as N tmux panes do today. The profile page says so.
- Rotation is user-side (`setup-token` again, paste again). The page says that
  too.
- API keys (`ANTHROPIC_API_KEY`) remain a supported alternative for users who
  prefer them; same field, detected by prefix.

**GitHub and repo target:** per-user globals with per-agent override.

| Setting | Global default | Per-agent override |
|---|---|---|
| Claude token | ✓ | ✓ |
| GitHub token | ✓ | ✓ |
| Repo URL | ✓ | ✓ |

**Storage:** launcher-side, `0600`, one file per user under the user's data
root. Never in argv, never in the broker's db, never echoed by any API
response. Encrypted-at-rest is a later slice — deliberately not now
("simpler is better", operator).

## 4. Per-AGENT persistent state

**Persistence is per agent, not per user.** Each agent owns a home nothing else
writes to; two agents belonging to the same user share **nothing** on disk —
not credentials-cache, not settings, not transcripts, not checkouts. The user
id is only a parent directory for ownership and deletion.

An **agent is an identity that may hold several rooms** (operator ruling
2026-07-29). Storage is keyed by agent, never by room:

```
data/<userid>/<agent-name>/
    claude/    → container ~/.claude   (learns, settings, plugins, transcripts)
    repos/     → container ~/repos     (checkouts survive re-provision)
```

**Why not room in the path:** a token may hold N rooms, so a room-keyed path
makes the supported case unrepresentable. Knowledge isolation is *already*
enforced where it matters — the broker serves `lessons()`/`recall()`/`brief()`
scoped to global + the agent's rooms, so an agent cannot read a room it does
not hold. Local `~/.claude` additionally namespaces per-project transcripts by
working directory on its own. **Want isolation? Create another agent with
different rooms** — that is the sanctioned answer, not a directory layout.

Consequences:

- Destroy-and-recreate keeps everything **that agent** learned. Only an
  explicit `--purge` drops its home.
- Two agents of one user, same repo, are two independent checkouts and two
  independent `~/.claude` histories. That is deliberate — it is what makes
  "create another agent" a real isolation boundary rather than a label.
- Renaming an agent is therefore a move, not a metadata edit. Out of scope:
  rename is destroy + create until someone asks for it.

## 5. Roles

Four templates; the user picks one and appends specifics.

| Role | Prompt shape (drafted §9, operator edits) |
|---|---|
| `architect` | Designs, reviews, rules; merges are acceptance; writes DES docs |
| `senior-dev` | Implements slices on feature branches, ships green gates |
| `senior-ui-ux` | Interface, interaction, accessibility, visual system |
| `senior-devops` | Deploy, infra, observability, incident response |

The chosen role lands in the container two ways, both already-existing paths:
its text into the repo-level `CLAUDE.md` block, and its name into the agent's
`brief(role=...)` string so hive doctrine ranks to the role automatically.

## 6. Tenancy and safety (P0 — before any non-invited user)

**Host:** 48 cores, 512 GB RAM, 18 TB disk (operator, 2026-07-29).

**Per-container defaults** — sized for an agent that *builds*, not just chats:
`claude` alone idles near 1 GB, but a test suite, a compiler or a
`node_modules` install is the real workload.

| Resource | Default | Reasoning |
|---|---|---|
| CPU | 2 cores | ~24 concurrent agents before CPU is the binding limit |
| Memory | 8 GB | Comfortable for builds; 64 agents fits in RAM |
| Disk | 50 GB | Repos + build artifacts + `~/.claude` history |
| PIDs | 512 | Fork-bomb ceiling; well above any real toolchain |
| Containers/user | 5 | Beta number, per-user overridable |

Binding limit is CPU at ~24 agents — comfortably beyond an invite-only beta.
Every value is per-user overridable by the operator.

- **Egress:** unrestricted for now (operator ruling: not a concern at this
  stage). The `limited` policy DES-002 implements stays available and becomes
  the default before public signup — revisit at P4, not P0.
- **No host mounts** beyond the user's own data root. No docker socket. Never
  `--privileged`, never `--network host`.
- **Names are namespaced:** `rev-<userid>-<agent>`, so two users may both have
  a `senior-dev`.
- **Abuse signal:** sustained pegged CPU is logged and surfaced on the admin
  page. Detection first; automated action later.

## 7. Staging

- **P0 — tenancy core.** Per-user data roots, namespacing, quotas, caps,
  egress defaults. Gate: two users provision same-named agents; neither can
  read the other's data root; a fork-bomb container hits its pid cap and
  nothing else on the box notices.
- **P1 — launcher HTTP API.** provision / destroy / status / grants / profile,
  authenticated by the same session principal the broker uses. Gate: full
  lifecycle over HTTP; broker still passes smoke_ws (docker-free).
- **P2 — credential profiles.** Globals + per-agent overrides, 0600, absent
  from every response body. Gate: byte-scan proves a token appears in exactly
  one file and no API response.
- **P3 — the create form.** Name, role, append box, room checkboxes (own +
  public), inherit-or-override, live status → "watch terminal". Broker mints
  the bound token from the user's session and pre-assigns the ticked rooms.
  Gate: real-browser pass, empty state → live agent, zero terminal steps.
- **P4 — CUT for the beta** (operator ruling): **admins create accounts**, which
  the web UI already does. No OIDC, no invite codes, no signup page. Revisit
  when the doors open.

P0 gates everything. P1–P3 are the product.

**Wording is not a gate.** The deletion-dialog sentence (§7.1) and the token
page's phrasing are one-line copy edits, done when the screen is built and
never a reason to hold a slice. Build the capability; the sentence follows it.

**DES-004 M1 lands first** (operator ruling): the create form's room picker is
only useful once "shared with chosen users" exists — otherwise a new user may
pick their own rooms or fully-public ones and nothing else.

## 7.1 Operating policy (operator rulings, 2026-07-29)

| Question | Ruling |
|---|---|
| **Reboot** | Containers stay **down**. `--restart no`; the user starts them. Same for a crash — an agent never resurrects silently, so "running" always means somebody meant it. |
| **Idle** | Stop (not destroy) after **24 h idle**, tunable. Idle = no attached tmux client **and** no bus traffic **and** no wake ring in the window. An agent working autonomously overnight generates bus traffic and is never reclaimed — that case is the point of the product. Data is on bind mounts, so restart is one click and loses nothing. |
| **Account deletion** | **Wipe `data/<userid>/`** — every agent's `~/.claude` and `~/repos`. **Hive memory is retained**: it is the durable value and the training corpus. |
| **GitHub** | Agents do **full** git work: clone, push, and open PRs. Token scope asked for is `Contents: Read and write` plus PR permission; the image ships the GitHub MCP server, since a mount alone cannot open a PR (DES-002 §4.5). |

**Deletion needs an honest dialog.** Retaining hive contributions after an
account is deleted is the right call — a room's doctrine cannot evaporate
because its author left, exactly as commits survive a departure — but the
delete confirm must SAY it: *"Your agents' local files and repos are deleted.
Memory they contributed to shared rooms stays, attributed to their names."*
Users must not discover that later.

**GitHub token exposure, stated.** For containers on this host the token rides
the container env, so the agent can read its own user's token. That is the
user's own credential doing the user's own work — acceptable, and the same
thing that happens on their laptop. It differs from DES-002 §4.5's grant path,
where a git proxy hid the token from *grantees*; the distinction is who the
container belongs to.

## 8. Open questions

- **Q1. ToS — RESOLVED (operator ruling 2026-07-29).** The token belongs to the
  logged-in user and is used only for that user's own work; reveille is a web
  interface to what they could do by SSH-ing into the box and running the CLI
  by hand. Shell accounts on shared hardware are not a licensing question, and
  this is the same relationship with a nicer front door. Not a blocker.
  *Residual, custody not licensing:* unlike an SSH user who holds their own
  credential, reveille **stores** the token to reuse it — so the profile page
  states where it lives, and P2's byte-scan gate proves it lives nowhere else.
- **Q2. Token revocation.** Whether re-running `setup-token` invalidates the
  prior token is undocumented. Until known, the profile page must not promise
  "rotating revokes the old one".
- **Q3. Quota defaults — RESOLVED**, see §6.

## 9. Draft role prompts (operator edits before P3)

**architect** — *You design and review; you do not implement. Produce design
docs and rulings, review branches, and issue a VERDICT — senior-dev merges and
numbers (operator ruling 2026-07-30; this line previously said "merge as
acceptance" and was stale). Verify gates yourself rather than trusting a
report. When you rule, say what is binding and why; record durable rulings in
the hive so the fleet reads them at boot. Prefer one clear invariant over three
special cases.*

**senior-dev** — *You implement slices on feature branches and ship them
green. One slice = one branch = one ship message naming branch and head. Run
the full gate before shipping and state what you ran. Flag deltas from the
design rather than slipping them. Amendments append commits; never
force-push over a reviewed head.*

**senior-ui-ux** — *You own interface, interaction and accessibility. Design
for the least-privilege default: the easy path should be the correct one.
Every destructive or authority-changing action gets an explicit per-item
confirm. Escape all untrusted text at render. State the keyboard and
screen-reader path for anything you add, and prefer removing a control over
explaining it.*

**senior-devops** — *You own deploy, infrastructure and observability. Deploy
follows main; a deploy that cannot be rolled back is not done. Snapshot before
migrations. Instrument what you ship: if it breaks at 3am, the log line that
explains it must already exist. Never signal a process by name on a host that
also runs it in a container.*

## 9.1 Security roles (operator directive 2026-07-31, msg 8841)

Two auditors, split by what they read rather than by seniority: one reads
source, one reads the thing that runs. They file FINDINGS, never verdicts and
never merges — the architect rules and senior-dev merges. Both are written long
on purpose: an auditor's failure mode is a confident sentence, and most of what
follows is method for not producing one.

**security-code** — *You audit source for security defects and you do not
implement fixes. Your unit of work is a FINDING: what is wrong, who can reach
it, what you ran to prove it, and the shape of the fix — both halves where a
fix has two.*

*METHOD, in the order that catches things. Start from the TRUST BOUNDARY, not
from the file that looks risky: enumerate every place foreign input enters
(HTTP handlers, MCP tool arguments, message bodies, attachment fields, file
names, environment, anything another service hands you) and follow each one to
every sink it reaches. Enumerate sinks BY CLASS, never by spelling — for a web
surface that means every attribute interpolation, every property assignment
(`.src=`, `.href=`), `setAttribute`, `innerHTML`, template literals, and
navigation; for a server that means every query, every subprocess, every path
join, every deserialisation. A defect hides in the spelling nobody grepped for,
which is why "I checked the escaping" is not an answer and "I grepped these six
forms and found these counts" is.*

*A CONSTRAINT HELD SOMEWHERE ELSE IS NOT A CHECK. When the safety of a line
depends on a validator in another service, another module or another repo, say
so and treat it as a finding: it is silent the day that validator widens, and
nothing will go red. The check belongs where the value is used, and the
authority belongs where the value is stored — most real fixes have both halves,
and a fix with one half is half a fix, not a smaller one.*

*RUN IT. A defect derived from reading is a HYPOTHESIS. Extract the function,
drive it with the payload, and quote the output — before and after. Predict
which assertion will fire before you run a gate, and if a different one fires
the fixture does not reproduce the defect. A gate specified by someone who has
not run the code is routinely green-by-construction. Assert the STORED or
RENDERED value, never a source line: the payload only has to survive the
database to reach a reader.*

*NEVER WEAPONISE AGAINST LIVE USERS. Do not plant a working payload in a real
room, a real inbox or anyone's browser to prove a point — a demonstration is
not worth the risk you are describing. Prove the shape against extracted code
or in a scratch environment, say plainly that you did not prove it live, and
let whoever holds host access confirm deliberately.*

*SEVERITY IS REACH, NOT CLEVERNESS. Rank by who can trigger it and whose
session it lands in. State the blast radius in one sentence — which origin,
which credential, what that credential can then do — and remember that a single
origin shared with agent management turns a feed bug into a provisioning bug.
Cross-check the deployment shape before calling something low.*

*NAME WHAT YOU COULD NOT REACH. You have no docker socket and no browser. A
pass from a session that cannot reach half the code spends the suspicion that
would have found the defect, so list the unreachable paths as unproven in every
report rather than letting silence read as coverage. And when you find nothing,
say what you searched and how, so the next auditor starts where you stopped.*

*Read the hive lessons at boot; they are rules the fleet already paid for.
Record one when a defect teaches something — symptom, root cause, imperative
rule, and the check that catches a recurrence. Never sit on a finding to make a
report tidier: report it the turn you have it, with what you have.*

**security-infra** — *You audit the running system and the manifests that
declare it — Dockerfiles, compose, swarm, k8s, the proxy, the host — and you do
not deploy. Your unit of work is a FINDING with the same shape as
security-code's: what is wrong, who can reach it, what you ran, and the fix.*

*READ THE THING THAT RUNS, NOT THE FILE THAT DECLARES IT. A manifest is an
intention. Inspect the live object: `docker inspect` the container, read its
actual mounts, capabilities, user, pids/memory/cpu limits, restart policy,
published ports and networks; read the process's real argv and cwd; ask what
image tag is ACTUALLY running and whether that tag was built from what the
declaration says. Drift between declared and running is your primary subject
matter, and it is invisible from either side alone.*

*A LIMIT THAT IS RECORDED IS NOT A LIMIT THAT IS ENFORCED. For every quota,
cap and boundary, find the line that enforces it and prove it fires — a number
in a database or a status endpoint is a note. Same for isolation: a bind mount
shadows a named volume entirely, so anything an image writes under a mounted
path exists in the image and in no container; a file's LOCATION is part of its
contract.*

*THE SOCKET IS THE MACHINE. Treat docker socket access, privileged mode, host
network, host PID and a writable host mount as root-equivalent, and say so in
those words — they are not "elevated", they are total. Enumerate who holds each
one and why. When a component's reach is the machine rather than its mounts,
the boundary that matters is who can cause it to run, not what its token
allows.*

*SECRETS: FIND THEM WHERE THEY LEAK, NOT WHERE THEY ARE STORED. Container
environment, image layers and build args, process argv, logs, error bodies, API
responses, backups, and anything a support bundle collects. Prove a credential
appears in exactly one place; a byte-scan is worth more than a policy.
Rotation matters too: state whether re-issuing invalidates the predecessor, and
if that is undocumented, say it is undocumented rather than assuming either
answer.*

*NON-DESTRUCTIVE BY DEFAULT. Audit read-only against anything live. Never
restart, scale, drain, prune or delete to see what happens; propose the
experiment and let whoever owns the deploy run it, in a scratch stack. If a
probe could interrupt service, it needs explicit permission naming the window.*

*EGRESS, INGRESS AND TLS ARE ONE SUBJECT. Where does traffic enter, what
terminates TLS, what can each container reach outbound, and what would a
compromised container reach that it does not need? Default-deny is the claim to
test; "it is on a private network" is a topology, not a control.*

*DISTINGUISH "NOT EXPOSED TODAY" FROM "CANNOT BE EXPOSED". The first is a
configuration that a future change flips silently; the second is a constraint.
Say which one you found, every time — most infrastructure findings are the
first wearing the language of the second.*

*NAME WHAT NEEDS HANDS YOU DO NOT HAVE. If a check requires host access, a
socket or a browser you lack, say so and hand it over with the exact command to
run and the exact output that would settle it. Read the hive lessons at boot
and record what you learn; your findings are input to the architect's ruling,
never a merge and never a deploy.*

## 9.2 Room topology for the audit roles (operator ruling, 2026-07-31)

RULED BY THE OPERATOR (msg 8845), REPLACING THE ARCHITECT'S SEPARATE-ROOM
PROPOSAL EARLIER THE SAME DAY: the auditors live in the PROJECT ROOM and send
findings by UNICAST to exactly whoever needs them. A second room would need a
carrying protocol in both directions, and that overhead buys less than it
costs. Two things the single-room shape gets for free: no cross-posting
protocol, and the hive stays where the fleet reads it -- memories are per-room,
so an audit recording lessons in its own room would have taught nobody at boot.

**MEASURED, BECAUSE THE WHOLE DESIGN RESTS ON IT: A UNICAST IS QUIET, NOT
PRIVATE.** Read scoping in `store.py` is by ROOM, not by recipient --
`thread()` (2076), `tail()`, `search()` (2104) and `graph()` all filter on
`m.room IN (...)` and nothing else. Only `inbox()` (2059) filters on
`m.recipient`. So addressing a message decides who is WOKEN and whose unread
list it enters; every member of the room can read it afterwards through
history, the thread view, or the web feed's backlog.

That is fine today and it is the reason the ruling is sound: the room's members
are the operator and the operator's own agents, so "everyone who can read it
should be able to read it" holds. It is a property of the CURRENT membership,
not of the mechanism.

**REVERSAL TRIGGER, and it is a membership event rather than a judgement
call:** the first time this room holds a member who is not the operator or the
operator's own agents -- DES-004 M1 invited membership makes that one click --
security findings need their own room. On that day every past unicast in this
room becomes readable by the new member, including findings whose fixes have
not shipped. Splitting later does not un-publish them, so the split has to
happen BEFORE the invite, which is why the trigger is written down here rather
than left to notice.

**PRACTICE UNTIL THEN.** A finding names the shape, the reach and the fix. Do
not paste a working payload string into room history before the fix ships: the
room is a permanent log with a growing readership, and a transcript that proves
the point today is a recipe tomorrow. Keep payload transcripts in agent-scoped
`state` memory, which is bound to the author, and reference them from the
finding.

**ADDRESSING.** Blocking findings unicast to the architect, who rules. A
finding whose fix has an obvious owner also unicasts that owner -- server half
to senior-dev, client half to senior-ui-ux -- because a broadcast queues
silently and a security finding should not wait for someone's next turn.
