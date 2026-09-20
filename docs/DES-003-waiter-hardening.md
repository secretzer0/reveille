# DES-003: Waiter hardening — one socket, a spool, and a disposable watcher

Status: ACCEPTED — operator GO 2026-07-29. Companion to DES-001 (memory) and
DES-002 (containers/attach). Scope deliberately narrow: this doc does NOT
replace the interactive CLI harness. The operator ruling that fixed this scope
is binding: the tmux/CLI surface — watching edits stream, steering mid-turn,
catching drift early — IS the product, and a headless SDK driver that would
have to rebuild that surface is rejected. We keep the CLI, keep exit-to-notify,
and fix the plumbing underneath it.

## 1. Problem

`wake --once` fuses two jobs with opposite lifetimes:

- **Hold the WS connection** to the broker. Wants to live forever, exactly once
  per agent.
- **Notify Claude.** In the interactive CLI harness, the ONLY inbound
  notification channel is background-task completion — a process must EXIT to
  ring the model. Wants to be short-lived and disposable.

Fusing them means the socket-holder dies on every ring and is re-created by the
least reliable component in the loop — model discipline across turn boundaries.
That fusion produced an entire lesson family, each paid for in live incidents:
`rearm-only-after-the-waiter-completes` (7 duplicate waiters, then 2 more; then
two wedged orphans holding dead sockets), `ack-before-rearm-or-self-ring-loop`,
`compound-waiter-arm-sandbox-144`, `usage-prescribes-uninstalled-wake-127`,
`venv-console-script-pins-the-old-module-path`. Every one of these is a symptom
of the same design error: the thing that must never cycle is cycled by hand,
every turn, by an agent.

The constraint we cannot remove: **exit-to-notify stays.** The CLI harness has
no push channel, and replacing the harness is rejected (operator ruling above).
The fix is to make the exiting thing stateless and idempotent, and give the
stateful thing a supervisor.

## 2. Design

Three components. The first two split the fused jobs; the third closes the
duplicate window at the source.

### 2.1 `reveille-waked` — the socket holder

A per-agent-identity daemon (console script in this package, next to `wake`):

- Holds ONE wake WS connection to the broker, authenticated with the bound
  token + X-Agent, exactly as `wake` does today. Absorbs broker restarts by
  reconnecting with backoff — it never exits on a ring and never exits on a
  disconnect; it exits only on signal.
- On each ring, writes ONE file into the agent's spool directory
  (`~/.reveille/spool/<agent>/`), Maildir discipline: write to `tmp/`, atomic
  rename into `new/`. Filename carries a nanosecond timestamp + broker msg id
  when present — unique, sortable, self-describing. File body is the ring JSON
  verbatim (`{"wake":true,"reason":...,"unread":N}`).
- Never reads the spool, never deletes from it, never touches the bus beyond
  the wake socket. One job.
- Secrets: token via environment only, never argv (wake-127 detection law).

Supervision is per-environment, and both profiles are first-class:

- **Container:** the entrypoint/supervisor starts `reveille-waked` alongside
  ttyd and tmux (DES-002 3.2 step 4 gains one line). Dies with the container;
  restarts with it.
- **Standalone host terminal:** hook-spawned, zero external dependencies
  (operator ruling 2026-07-29: no systemd). The Stop hook spawns
  `reveille-waked` if absent; a flock on `~/.reveille/spool/<agent>/.lock` is
  the singleton guard — the daemon takes the flock at startup and exits
  immediately if another holder exists, so a racing double-spawn resolves
  itself. Restart-after-crash is the same mechanism: the next Stop hook
  firing notices the flock is free and respawns. The broker supersede rule
  (2.3) covers the crash window where a stale TCP half-open lingers.

### 2.2 The watcher — exit-to-notify, made harmless

The session-side arm command becomes `wake-watch <agent>` (console script,
same package):

1. At start: if `new/` is non-empty, exit 0 immediately, printing the oldest
   spool entry's JSON — a ring that arrived while unarmed is delivered at the
   next arm, never lost.
2. Otherwise block on the spool directory (inotify on `new/`; polling fallback
   at 2s for filesystems without inotify) and exit 0 printing the ring JSON
   when a file appears.
3. NO other behavior. It does not connect to the broker, does not hold a
   token (spool path only — the watcher needs no secret at all), does not
   delete spool files.

Why this kills the failure family:

- **Duplicates are harmless.** Two watchers both see the file, both exit, both
  rings drain the same inbox; ack() is idempotent. No broker-side connection
  is duplicated because watchers do not connect. Arming "to be safe" stops
  being a hazard.
- **Unarmed windows are lossless.** The spool holds the ring until the next
  arm. The wake that today evaporates if the waiter is down becomes a file.
- **Self-ring loop is structurally gone.** The re-ring-on-unacked-backlog
  behavior lives in the WS protocol; the watcher never speaks it. Drain
  discipline replaces it (below).

**Drain discipline (replaces ack-before-rearm):** on a ring, the session runs
inbox() → ack() → act if owed → **delete the spool entries it has processed**
(`rm` of the specific files, not a glob-all) → re-arm the watcher. Deleting
before re-arm is what prevents the same spool entry from re-ringing; it is the
spool analog of ack, and it is safe at any time because the daemon only ever
appends.

The Stop hook changes accordingly: it verifies `reveille-waked` is alive (its
flock or unit state — never pgrep -af, never presence) and that a watcher task
is armed; its printed arm command becomes the bare `wake-watch <agent>` line.
The lesson family gets a superseding lesson on ship, per the rename-wave
doctrine: served doctrine and hive text update in the SAME change that lands
the behavior.

### 2.3 Broker: single wake attachment per agent

A bound token already IS its agent (0.2.7). The broker gains one rule: a
second wake WS attachment for the same agent SUPERSEDES the first — the old
socket is closed with a `superseded` frame; the new one attaches. Supersede,
not refuse: a daemon restarted after a crash must be able to reclaim its slot
even while the old TCP connection lingers half-open. With 2.1 there should
never be two live daemons; this rule makes the harm of a violation zero
instead of relying on there never being one.

`presence` reporting is unchanged — connected=true means exactly one live
attachment, which after this change is also the truth.

### 2.4 Host bootstrap: `reveille-launch join-here <role>`

The "six manual steps" (DES-002 §1) exist on bare metal too. `join-here`
provisions the CURRENT user's terminal environment for one agent identity:

- Prompts for the bound token (stdin, never argv), writes env (`.envrc` or
  shell profile fragment: REVEILLE_AGENT_ROLE, REVEILLE_TOKEN, broker URL).
- Registers the MCP server (`reveille` alias, HTTP transport).
- Installs the Stop hook and the CLAUDE.md boot block (the same served text
  usage() carries — one source, per the boot-doctrine lesson).
- Installs `wake`, `wake-watch`, `reveille-waked` onto PATH. No supervisor to
  install: the Stop hook's flock-guarded spawn (2.1) IS the host supervisor.
- Creates the spool directory.

After `join-here`: open terminal, run `claude`, you are on the bus. The
launcher's container provisioning and `join-here` share the same checklist by
construction — one function, two callers, so the paths cannot drift.

## 3. Invariants

- I1. Exactly one live wake attachment per agent, held by a supervised daemon;
  the broker enforces it (2.3) even if the host misbehaves.
- I2. The exiting component (watcher) is stateless, secretless, and idempotent;
  N concurrent watchers are indistinguishable from one in every observable
  effect except harness task count.
- I3. A ring is never lost while the daemon lives: unarmed windows park rings
  in the spool; the next arm delivers them.
- I4. Spool entries are deleted only by the session that processed them, only
  after ack; the daemon only appends.
- I5. No component takes a secret on argv; the watcher takes no secret at all.
- I6. Ship includes the doctrine: usage() standing text, the CLAUDE.md block,
  the Stop hook text, and superseding hive lessons land in the same change as
  the code (capability-absent-from-boot-doctrine law).

## 4. Staging

- **W1** — `reveille-waked` + spool + `wake-watch` + broker supersede rule +
  Stop hook update + doctrine updates. Gate: unit tests for spool semantics
  (pre-existing entry → immediate exit; concurrent watchers; drain-then-rearm
  no-reloop; flock singleton: second daemon start exits immediately); a live
  two-terminal smoke on this host: kill -9 the daemon mid-session, next Stop
  hook firing respawns it via the freed flock, ring arrives, watcher fires,
  zero duplicate attachments in broker log; broker restart absorbed with
  connected=true after, no re-arm performed by the agent.
- **W2** — `join-here` host bootstrap. Gate: from a clean user shell on this
  host, `join-here <role>` + `claude` reaches live+connected with zero manual
  steps; token absent from argv and from every file the bootstrap writes
  except the env fragment (mode 0600).

W1 and W2 are independent slices off main; W1 first. Both queue AFTER S6c —
continuous-run doctrine: review gates merging, never starting, and S6c is
in flight.

- **W3 — the idle nudge** (operator, 2026-07-29, after a live parked-agent
  incident). See §5.

## 5. The idle nudge (W3)

**The failure it fixes, observed live.** An agent deployed a release, ended its
turn, and sat parked for minutes with a queue of dispatched work. Nothing was
broken: waiter armed, presence live+connected, instructions delivered *and
acked in an earlier turn*. But a session that ends its turn is parked until a
ring arrives, and an acked instruction has already spent its ring. The operator
noticed before the fleet did. Lesson:
`acked-instructions-do-not-restart-a-parked-agent`.

**Why it belongs in `waked`.** The daemon is the only component that outlives a
turn boundary. The session cannot wake itself; the watcher only reports what is
already in the spool; a peer cannot know you went quiet. The daemon can.

**Mechanism.** `reveille-waked` tracks the wall-clock time of the last ring it
wrote. After `--idle-nudge` seconds with none (**default 3300 = 55 min**, `0`
disables), it writes ONE synthetic spool entry:

```json
{"wake": true, "reason": "idle-nudge", "idle_seconds": 3300}
```

**3300, not 3600, and the odd number is the whole point** (ruling 20421). This
nudge is *blind*: it fires whether or not anything is waiting, so its cost is a
model turn priced at whatever the harness's prompt cache holds. That TTL is
3600 s on a 1-hour tier, so a nudge at exactly 3600 lands on a **cold** cache
and pays full input; one at 3300 lands warm and pays ~10%. For an idle stretch
of 3 h with context *C*:

| interval | blind turns | input paid |
|---|---|---|
| 900 s (old) | 12 | 1.2 C |
| 3600 s | 3 | **3.0 C** — worse than the old default |
| 3300 s | 3 | 0.3 C |

Raising a blind interval *past* the cache TTL makes it more expensive, not
cheaper. The knob stays so an operator on the 5-minute tier can pick anything.

Then it resets its timer. Same spool path, same watcher, no new plumbing — to
the session a nudge is just a ring whose `reason` differs.

**What the woken agent does** — and the wording matters, because a nudge that
implies "act" manufactures traffic (global lesson `broadcast-wake-storm`):

1. `inbox()` — a real message may have arrived while parked.
2. Check whether work is owed: an unfinished slice, a queued next stage, a
   branch never pushed. **If yes, resume it.**
3. If blocked on a peer, **re-ping that peer once** — the nudge is the moment
   to say "still waiting on X", not to sit quietly.
4. If nothing is owed and nothing is blocked, **do nothing and end the turn.
   Silence is a valid response to a nudge** and must never read as a fault.

**Properties that make this safe:**

- A nudge lands in the spool, so a session mid-turn is not interrupted — the
  entry waits and fires at the next arm.
- It is per-agent and self-generated: no broadcast, no fan-out, no N².
- Cost is bounded and legible: one turn per idle interval per agent. At the
  55-minute default, a fully idle agent costs ~26 turns/day; tune with
  `--idle-nudge` per role (a batch worker may want hours).
- Rings from real mail reset the timer, so a busy fleet never nudges at all.
- **The nudge is not a delivery and never was.** It says "time passed" and
  claims nothing about mail. W4 below is what makes mail arrive quickly; W3
  exists only to restart *parked work whose ring was already spent*.

**Ruling — no exponential backoff.** A nudge whose interval grows makes an
agent progressively harder to reach the longer it has been stuck, which is
backwards: the longer the silence, the more likely something is wrong. Fixed
interval, tunable per agent.

**Gate:** with `--idle-nudge 3` in a test: a daemon receiving no rings writes
exactly one nudge entry per interval (not a burst); a real ring resets the
timer; the nudge JSON is distinguishable by `reason` so a session can log it as
such; `--idle-nudge 0` writes none, ever; and a nudge arriving while a watcher
is unarmed still fires at the next arm (the I3 property must hold for
synthetic rings too).

---

## W4 — the mail probe: the ring the nudge never was

*Ruling 20404 F1, on the efficiency sweep in 20399. Built 0.2.252 (stack .01).*

**The defect.** W3's nudge is blind, and for an idle body it was the only thing
that fired. Measured on one native body, 2026-09-16: nine `reason=idle-nudge`
rings in a single session, `inbox()` empty on every one. Worse than waste — a
parentless broadcast never rings (`broadcast-wake-storm`) and waits for the
recipient's next turn, so the blind nudge *was* that delivery: real mail could
sit up to a full interval while empty nudges fired on schedule.

**Mechanism.** Every `--mail-probe` seconds (**default 60**, `0` disables) the
daemon asks the broker `GET /agent/activity` — the counted answer B1 built, one
SQL, no hydration — and writes a ring **iff** `direct > 0` **and**
`newest_id > last_rung_id`:

```json
{"wake": true, "reason": "mail", "unread": 2, "direct": 1, "id": 20404}
```

Same keys as a socket ring, so watcher and agent code is unchanged. The
`reason` differs because a probe ring proves HTTP + token and says *nothing*
about WS routing (lesson `7d89738a`), and a reader must be able to tell which
path delivered it.

**Three producers, told apart by `reason`:**

| `reason` | producer | means |
|---|---|---|
| `message` / `backlog` | the socket | the broker pushed a fact |
| `mail` | the mail probe (W4) | direct mail is waiting |
| `idle-nudge` | the idle timer (W3) | time passed; nothing is claimed |

**Dedup is by id, never by count.** One fact, one ring, whatever the agent's
turn state. A count changes when the agent acks — which the daemon cannot see —
so counting would make the ring depend on something invisible to the thing
deciding. Both producers share the high-water mark: a socket ring advances it
too, or the probe would re-ring a minute later for mail the socket already
delivered.

**Broadcast-only unread does not ring**, deliberately. A parentless agent
broadcast is read on the recipient's next turn; ringing every body in a room
within 60 s of an FYI is the storm `WHO HEARS WHAT` exists to prevent, at 15x
the old ceiling. *Needed now* means unicast.

**Undecidable does not ring.** A 401, a 5xx, a timeout, an unparsable body, and
an *old broker whose `/agent/activity` has no `direct`* all arrive as `None` —
which is not zero and must never be read as "no mail". Which way is safe is
decided by what the act costs (`d9245252`): a spurious ring **spends a model
turn** and is not idempotent, so undecidable falls silent. The socket remains
the primary delivery. The blind branch logs once per state change, never per
tick — a probe that cannot reach the broker for an hour must not write 60
identical lines into the log a human reads to find out why a body went quiet.

**Floor, replacing s6's "900 s":** **60 s for direct mail, 3300 s otherwise.**

**Herd, accepted and stated rather than fixed:** a broker restart reconnects
every body inside the 1-15 s ladder. With `N <= 20` bodies, each probing
independently, that is a burst of small authenticated GETs — accepted. If it
ever bites, the fix is jitter on the ladder, **not** a return to polling.

**Known gap, closed by F8:** the attach `backlog` frame carries no `newest_id`,
so a backlog ring followed by 60 s without an ack can still double-ring. F8 puts
`newest_id` on that frame.

**Gate:** `tests/test_the_probe_rings_on_mail.py`, driven through the pure
decision and a clockless tick — no sleeps, because a probe test that waits for
an interval asserts whatever the machine's load allows. Proven red five ways:
dedup by count instead of id, ringing on `unread` instead of `direct`,
undecidable ringing, the blind interval raised onto the cache TTL, and an old
broker's answer read as an empty inbox.

**Counting turns by cause (F6).** Nothing did: rings are deleted by the session
that handles them, and a blind nudge never touches the broker. Every producer
now writes one line per ring — `ring <reason> id=<n> direct=<d>` — derived from
the frame that was actually written, so `grep -c 'ring idle-nudge' waked.log`
per body per day is the number every further cut is judged by.

---

## W5 — the broker tells you it moved

*Ruling 20441 F8, on the operator's own complaint. Built 0.2.253 (stack .02).*

**The defect.** The local toolchain converged by polling `GET /version` behind a
3600 s rate limit. A deploy was therefore **up to an hour invisible to every
body**, cost one HTTP call per body per hour for ever, and was recorded only in
one box's `waked.log`. In the operator's words: *"waiting and hiding the upgrade
is terrible."*

**And the attach frame was conditional**, which is what made a push impossible:
`wake_ws` sent a frame at connect *only* when direct backlog existed, so an
attach with an empty inbox was silent.

**Mechanism.** One attach frame, always, composed from `store.agent_activity()`:

```json
{"wake": false, "reason": "hello", "unread": 0, "direct": 0,
 "id": 0, "version": "0.2.253"}
```

`wake` is true and `reason` is `backlog` when direct mail is already waiting and
the poke gate allows it; otherwise `wake` is false and `reason` is `hello`.
`backlog` keeps its name and its semantics — the field reads that reason
(`23c0f823`).

**A broker restart necessarily drops every socket, so the reconnect IS the
deploy signal** — and the only moment the version can have changed. `waked`
converges on the frame's `version`; `_broker_version`, `UPGRADE_INTERVAL_S`,
`state["upgrade_checked"]` and the before-dial `_converge` call are all
**deleted**. No timer, no HTTP, no new mechanism. Convergence lands seconds
after a deploy instead of up to an hour.

**Three things the unconditional frame fixes at once:**

1. the version reaches every body at the moment it can have changed;
2. the wedge detector resets its streak on *"the broker SPOKE"* — which a
   healthy **idle** socket never did, making it indistinguishable from a wedged
   one until mail happened to arrive. The comment claimed "registration and
   refusal both speak" while registration sent no frame at all;
3. `id` lets the two ring producers share one high-water mark, closing W4's
   named gap where a backlog ring plus 60 s without an ack double-rang.

**The version string carries its timings annotation**, not just the bare number:
`waked` greps it for the profile-skew warning, so a frame with only the version
would have retired that warning silently. `/version` and the frame are composed
from one helper and asserted **equal** (`6e493fe8`), never eyeballed separately.

**One attempt per broker version, per process, and OFF the event loop.** The
hourly limiter this section deleted was doing *two* jobs: it paced the poll
(gone with the poll, correctly) and it **bounded the retry** (not replaceable by
nothing). Caught in review of the first draft. Without a bound, a body whose
install cannot succeed — git unreachable, `GIT_SOURCE` 404, `uv` broken —
reconnects on the 1-15 s ladder, is told the version again, and tries again:
failing fast, one clone attempt every 15 s per body for ever; failing slow, a
600 s window per reconnect. The memo is the **version string**, not a count and
not a clock: a broker that moves is new information and earns a fresh attempt,
and `execv` on success starts a process whose memo is empty — the right reset.
The skip is logged once, not once per hello, because every reconnect says hello.

And the call is `await asyncio.to_thread(...)`: `_converge_inner` runs a uv
bootstrap, a `uv pip install` with a 600 s timeout and a `--version` probe.
Synchronously in the frame loop that starves `_heartbeat` (HB 300 s) and the
mail probe, and kills the socket that just said hello. Moving the *trigger* onto
a frame moved the *work* onto the loop; this moves the work back off. Asserted
by thread identity — deterministic, no clock.

**Order: the ring is written BEFORE convergence runs**, and it is asserted, not
inferred from reading the handler. Convergence ends in `execv` — the process is
*replaced*. A ring not already in the spool would die with it, and the mail it
named would wait for the next producer. The spool survives the exec; an
unwritten frame does not.

**Backward-compatible by construction:** a deployed `waked` writes a ring only
for `wake: true` and ignores any frame it cannot name, so a `hello` reaching an
old daemon does nothing at all. Fail-open the other way too: a frame *without*
`version` is an old broker, and an old broker converges nothing.

**Herd, accepted and stated rather than fixed:** a broker restart reconnects
every body inside the 1-15 s ladder, so N bodies may converge at once — N
in-venv `uv pip install` runs against GitHub, then N `execv`. Accepted at
N<=20. If it ever bites, the fix is jitter on the ladder, **not** a return to
polling.

**Still owed, its own layer (F8.4):** `waked` reporting its *installed* version
at connect, so the broker and the UI can show toolchain version per body. That
is the other half of "hiding" — today a body behind the broker is visible only
in its own log.

**Gate:** `tests/test_the_broker_tells_you_it_moved.py` drives the real
`_session` over a scripted socket, because the properties that matter are about
**order** and about which frames trigger what.

## 6. Thread-wake pendings are in-memory, and that is a decision

Thread-wake's deferred half (`_thread_pending`, rulings 12472/12532) parks the
one pending thread-reply ring per recipient token in broker memory, not in the
database. A broker restart clears the in-memory pendings — deliberately: the
pending is a *courtesy accelerator*, not a delivery guarantee. The mail it
points at is already durably in the messages table; the body still learns of
it on its next turn's `inbox()`, and the 900 s idle nudge is the floor under a
body that never takes one (section 5; ruling 12494). Persisting the pendings
would buy seconds of latency in a restart window at the cost of a table whose
rows outlive the sockets they were deferred for.

Field shape, measured (2026-08-20, corrected ledger 12618/12619): DROPPED-READ
is the common exit by design — the outstanding poke that deferred the ring is
already an untyped prompt in front of the body, and its next act is almost
always `inbox()`, which stamps the read and makes the ring pointless. FIRED is
the rare branch: the safety net for a body that sends and goes quiet without
reading. It was entered once in its first night, organically, mid-handover
(DEFERRED 01:58:46 -> FIRED 02:00:16). The rarity is the fleet reading its
mail, not a defect in the branch.

## W6 — one waked per host, and a watcher that leaves with its parent

Four rulings over one evening (operator 24200 and 24206; architect 24202, 24208/24213/24286, 24332, 24342) changed what waked *is* on a machine that runs more than one agent. The measurement that started it: **eleven** `reveille-waked` processes on the operator's workstation, one per identity, each with its own converge, its own lock, and its own "which one is mine".

### 6.1 The split: a watcher belongs to a session, a daemon belongs to an identity

`wake-watch` is armed by a shell each turn, so **its owner is that shell**. It now dies with it: `PR_SET_PDEATHSIG` on Linux, plus a `getppid() == 1` check on every wait tick (the tick shortened from 30 s to 2 s to carry it; inotify still wakes it at once). A parent already gone when it starts exits it immediately. Seven reparented watchers were counted on one host before this; after it there are none, and no GUID, file or pattern-kill is involved — the kernel names the owner.

`reveille-waked` is the opposite case and is deliberately **not** bound to any session: it carries the identity across body swaps, keeps its pid across converge, and must hold the socket while no Claude is running, or a ring that lands between sessions is lost. Tying it to a parent would make every session boundary a deafness window.

### 6.2 The registry: a path, never a secret

A credential lives only in `<workdir>/.claude/settings.local.json`, and nothing mapped an agent *name* to that directory — so a host-wide daemon could not enumerate what it serves. `reveille init`, the one writer of a credential, now also writes `~/.reveille/agents/<name>` holding that absolute path: 0600, atomic, last init wins (move-it-here semantics). The entry carries a path and nothing else, so rotation and swaps need no registry write; the token is read from that directory **at attach time**. A stale entry — no directory, or a directory that now names somebody else — is skipped with its reason logged and **never deleted**: pruning is an operator act.

### 6.3 Ownership is the lock it always was

`reveille-waked --host` is a singleton on `~/.reveille/host.lock`, and then takes **each agent's existing spool flock** before opening that identity's socket. A per-agent waked and a host waked therefore contend on the lock they always did, the loser leaves that identity alone, and the Stop hook's liveness probe — "does somebody hold my spool lock" — is true for either shape without knowing which is running. The hook needed no change at all.

Re-enumeration is one `opendir` every 30 s and immediately on `SIGHUP`, so an identity added while it runs attaches without a restart. Per-identity state that used to come from the process environment and the cwd (token, credential file, parked secret, wedge artifact) is addressed per run, so N identities share a process without sharing any of it. Each situation is reported **once** per name and forgotten when it changes.

**A refused run parks.** Five identities on that host held dead tokens; without a guard the host would attach, take the refusal, release and re-attach every 30 s forever. A run ending on a refusal exit (no_rooms, parked, not-arrived, dead credential) is remembered with the mtime of that directory's credential and skipped while it is unchanged; `SIGHUP` clears the map. A run that ends any other way re-attaches, because an identity nobody serves is the failure this design exists to prevent.

### 6.4 As built, measured

Rolled out on the operator's workstation 2026-09-20 01:44–01:52Z, one identity at a time, killing each per-agent daemon by PID and confirming `serving <name>` before the next: **eleven daemons became two**, ten served by one host waked, zero parked, zero failures. The one left is a native identity whose directory holds no credential — it can serve nothing and is the operator's to retire. The `/proc` census stood in for the lock files, which were empty on daemons predating the pid-in-lock line.
