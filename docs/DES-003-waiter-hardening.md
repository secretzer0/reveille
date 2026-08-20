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
wrote. After `--idle-nudge` seconds with none (**default 1800 = 30 min**, `0`
disables), it writes ONE synthetic spool entry:

```json
{"wake": true, "reason": "idle-nudge", "idle_seconds": 1800}
```

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
  30-minute default, a fully idle agent costs 48 turns/day; tune with
  `--idle-nudge` per role (a reviewer may want 30 min; a batch worker may want
  hours).
- Rings from real mail reset the timer, so a busy fleet never nudges at all.

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
