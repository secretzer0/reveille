# DES-002: Container launcher — web-provisioned agent containers with shared tmux

Status: ACCEPTED — review round closed 2026-07-28: senior-dev 4 ACK + 3 refutations
(bus msg 8385: R1 token retention, R2 volume scope, R3 revocation channel), all three
ratified into this revision; Q1-Q6 resolved (section 6). Companion to DES-001 (memory
layer, ACCEPTED); this doc is the compute/attach layer only. T1 is GO, parallel with
DES-001 S5/S6.

## 1. Problem

An agent today is a tmux pane a human hand-builds, every time: clone the repo, write
.envrc (token + role), register MCP, install the Stop hook, arm the waiter, start the
session. Six manual steps per agent, each a chance to resurrect a paid-for lesson
(wake-127 was exactly this: a hand-built pane missing the CLI on PATH). The shape is
already law — one agent = one container = one git identity = one bound token
(DECISIONS "Containers"), and `make agent-spike` proved a container joins with an
existing identity and inherits rooms, history, lessons and peers having copied
nothing. What does not exist:

- Provisioning: "one more dev on project X" is an afternoon of pane surgery, not a
  button. The marginal agent should cost what the marginal token costs.
- Visibility: the session is a terminal on one machine. Guiding a coding agent means
  BEING at that terminal; a second human cannot watch, and the operator cannot watch
  from a browser. Section 4 fixes this; the launcher makes it provisioned rather
  than hand-rigged.
- Disposal: killing a hand-built pane strands nothing today ONLY because every
  artifact happens to live elsewhere (remote git, broker). The launcher makes that a
  guarantee instead of a habit (G5).

Scope boundary: these are the OPERATOR'S containers on the operator's box. DECISIONS
rejected hosting customers' agents (credential custody, runaway spend, miner magnet)
— nothing here reverses that; the launcher is homebrew tooling that a customer runs
on their own hardware, which is the product thesis (the bus is the product; agents
run on the customer's compute).

## 2. Goals

G1. Provision: web UI creates a container wired for one agent — repo clone, bound
    token in env, MCP registration, waiter + Stop hook armed at boot.
G2. Attach: the operator reaches the agent's tmux from ssh and from the browser.
G3. Share: the container's owner can let other humans watch or drive that tmux,
    per-person, revocable live (section 4 — specified).
G4. The broker stays mail-only. Nothing about containers reaches broker code
    (DECISIONS: smoke_ws guards it; 0.2.0 deleted /terminal for exactly this).
G5. Disposable compute: container state that matters lives in the remote (git),
    the broker (mail, hive memory), or volumes — kill and re-provision is routine.

Non-goals: hosting CUSTOMERS' agents (rejected in DECISIONS — custody of their
credentials, runaway spend, miner magnet); multi-agent containers; worktrees across
containers (each container is a full clone; the remote is the shared object store;
the bus is the coordination layer git lacks).

## 3. Components

### 3.1 The launcher daemon — the ONLY thing that touches docker

A separate host-side process (`reveille-launch`, daemon + CLI in one), owning the
docker socket. The broker never gains docker awareness — smoke_ws guards this
(DECISIONS: it fails the day the broker grows any), and the 0.2.0 /terminal deletion
is the same boundary from the other side. Hard consequences:

- The launcher is its own process with its own listener (localhost HTTP + CLI in T2;
  published through the tunnel behind Cloudflare Access in T4, never a host port).
- The launcher holds NO standing broker credential. Provisioning takes the bound
  token as INPUT (operator mints in /ui, pastes or pipes to `reveille-launch new`).
  Automating the mint is Q4 — declined for v1 because a daemon holding an admin
  session is a bigger surface than a paste is a friction.
- Token RETENTION (R1, ratified): launcher.db NEVER persists broker tokens — env
  dies with the container and destroy+new PROMPTS AGAIN. A launcher that stored
  agent tokens would be the fleet's whole authority in one host file, which is the
  standing-credential store the previous bullet forbids, in different clothes.
  Q4's paste ledger therefore counts per PROVISION, re-provisions included. T2's
  gate asserts launcher.db contains no token bytes.
- Broker web UI shows nothing container. Container state lives in the launcher;
  the bus sees only what it always saw — an agent that joined.

### 3.2 Provision flow (`reveille-launch new <role> <repo-url>`)

1. Take: role name, repo URL, bound token (stdin/prompt, never argv — argv leaks,
   the wake-127 lesson's detection note is fleet law), optional resource caps.
2. Create container from the agent image, named `rev-<role>`, labeled for cleanup
   (`reveille.role=<role>`), memory/cpu caps applied (defaults: 4G, 2 cpus; cgroup
   flags, ZFS quota rides the platform backlog).
3. Inject env at create: REVEILLE_AGENT_ROLE=<role>, REVEILLE_TOKEN=<token>,
   broker URL. Env, not baked into image layers — `docker inspect` exposure is
   root-equivalent-only, same class as reading .envrc on the host today.
4. Entrypoint: clone repo, register MCP (HTTP transport against the broker),
   install Stop hook, install `wake` onto PATH (the wake-127 fix is an image
   guarantee now, not a hand step), write tmux.conf (window-size largest,
   aggressive-resize, HISTFILE off), start tmux session `agent`, arm the waiter,
   start ttyd bound to the container's private interface, start the agent process.
5. Health: provision succeeds when the broker's presence shows the role
   live+connected — checked by the LAUNCHER polling presence with the agent's own
   token (read the bus like any client; no new broker surface). Verified sound
   against live code (8385): presence() carries no _seen() heartbeat — the probe
   cannot refresh the liveness it is measuring. STANDING INVARIANT: presence stays
   heartbeat-free, or this health check silently rots into an observer effect.

### 3.3 Agent image

docker/ already builds the base (Dockerfile, entrypoint.sh; spike_join.py proved
join-with-identity). T1 additions: tmux + ttyd + attach-gate, claude CLI, uv, git,
the wake console-script on PATH. One image for every agent; role differences are
env + volume, never image forks (an image fork is a config smell).

Claude home state (R2, ratified — "login volume" understated it): the named volume
per container (`rev-<role>-claude`) mounts the WHOLE claude home — credential,
settings, projects, transcripts, local memory. Scoping to the credential file alone
was rejected as brittle (login internals are not ours). The honest G5 inventory is
therefore: remote git + broker (mail, hive memory) + the claude-home volume — three
places state may live, named, nothing else. destroy offers --keep-login (default)
and --purge. CONSEQUENCE for T3: the kill-and-reprovision smoke runs its
resume-from-bus-state assertion with --purge, or the resume may come from local
transcripts and the test passes for the wrong reason.

### 3.4 Grants and attach: section 4 (specified). Grant table lives in the
launcher's own sqlite file (launcher.db) — same one-file discipline as the broker,
different file, different process; grants key on container id and die with it.

### 3.5 Lifecycle

- `new` / `stop` / `start` / `destroy [--purge]` / `ls` / `grant` / `revoke`.
- Re-provision = destroy + new with the same role and volume: the remote has the
  code, the broker has the mail and memory, the volume has the login. Nothing else
  existed — that is G5 verified by construction, and the smoke test T3 kills a
  container mid-conversation and re-provisions it to prove the agent resumes from
  bus state alone (join + brief() once DES-001 S4 ships).
- Crash policy: restart=unless-stopped for the container; the waiter's own
  reconnect discipline (0.1.5) already survives broker restarts.

## 4. Shared tmux: watch and drive, owner-controlled — SPECIFIED

The demand: the container owner shares the agent's tmux with another human dev to
watch Claude code, or to drive it, and can flip a person between view-only and
interactive — live, per person, without restarting anything.

### 4.1 What tmux already gives (use, don't build)

tmux is natively multi-client; every mechanism below is built in:

- N clients attach to one session; output mirrors to all, server-side, instant.
- Read-only attach: `tmux attach -r` — sees everything, keys ignored.
- Grouped sessions (`new-session -t <group>`): shared windows, independent
  per-client current-window — one visitor watches the agent pane while another
  reads logs, neither yanks the other's focus. The launcher uses grouped sessions
  for visitors by default.
- Sizing: `window-size largest` + `aggressive-resize` in the image's tmux.conf, or
  the smallest attached browser tab shrinks every client.
- tmux >= 3.3 `server-access` ACLs exist as a second enforcement layer if visitor
  attaches ever run as distinct Unix users; the primary design keeps one Unix user
  per container (one container = one identity) and enforces at the entry wrapper.

### 4.2 Attach planes

Two, both terminating INSIDE the container; the broker carries neither:

- ssh: operator plane. `ssh <container> -t tmux attach`. Already works, stays.
- ttyd sidecar: browser plane. One ttyd process per container serving the tmux
  session over WebSocket to xterm.js in the browser. Multiple concurrent browser
  clients are native to ttyd. It binds the container's private interface only and
  is published through the existing ingress (Cloudflare Tunnel), never a host port.

HARD BOUNDARY restated: 0.2.0 deleted the broker's /terminal for exec-on-a-pty
behind a shared secret. That decision stands. The bridge lives in the container as
a sidecar; the broker's only involvement is zero. Web-user identity on the BUS does
not grant terminal access — the two planes authenticate separately.

### 4.3 Grants: the owner controls view vs drive

Model:

- Every container has one OWNER (the web user who provisioned it).
- The owner grants access per person: {viewer | driver}, default viewer. Grants are
  revocable live and expire with the container.
- DRIVER IS EXCLUSIVE by default: at most one writable client at a time (owner or
  one grantee). Two humans typing into one Claude prompt interleaves into garbage;
  exclusivity is enforced, not asked for. The owner may explicitly set
  multi-driver=true on a container to opt out (pairing between trusted humans).
- The owner can always attach writable; granting a driver does not lock the owner
  out (both writable is the one sanctioned multi-driver case — same trust domain).

Enforcement — at the entry wrapper, not in the client's hands:

- ttyd never execs `tmux attach` directly. It execs `attach-gate <grant-token>`, a
  small wrapper that (1) verifies the grant OFFLINE: the per-grant URL token is
  SIGNED, checked against a per-container secret injected at provision — no
  in-container polling, no gate-to-launcher channel, smallest surface (R3,
  ratified), (2) execs `tmux new-session -t visitors -s v-<grant>` for viewers
  WITH `-r`, or a writable attach for the driver.
- Revocation is the LAUNCHER reaching in (R3, ratified): revoke = docker exec that
  kills the gate's process group for that grant. Host-side kill, nothing in the
  container watches anything; ttyd drops the socket and a revoked viewer
  disappears mid-keystroke, which is the point. The <1s smoke in T3 measures
  exactly this path.
- The mode rides the SERVER side of the WebSocket. A visitor cannot flip their own
  `-r` off. ttyd itself must run writable (`-W`) or no driver could ever type;
  what that flag buys is transport only — client input crosses the wire, and
  whether tmux HONORS it is decided server-side by the gate's exec choice (`-r`
  for viewers). Write capability exists only through that choice. Client-side
  anything is decoration.
- The gate's argv is attacker-controlled. ttyd's `-a` appends URL `?arg=` values
  to the command, which is how the token arrives — so it is also how anything
  else arrives. The gate takes a FIXED first argument from ttyd and treats every
  client-supplied word as untrusted data, never as a subcommand: signing
  (`mint`) must be unreachable from the wire, or the door hands out its own keys.
- Auth to ttyd: per-grant URL token issued by the launcher UI, single audience,
  short TTL, renewed while the grant lives — layered under Cloudflare Access at
  the edge (tunnel plan, backlog item 1). Neither layer alone is the gate.

Audit: one log line per attach/detach/revoke — who, container, mode, timestamp.
tmux cannot attribute keystrokes inside the pane (known, accepted for pairing);
the attach log is the attribution boundary, so it must be honest and complete.

### 4.4 What a watcher sees, and what a driver IS — stated, not hidden

A DRIVER grant is not a keyboard, it is the agent's whole identity (8385): a
writable client can open a shell pane as the container user and read
REVEILLE_TOKEN, the claude-home volume, git credentials. Grant driver only to
someone you would hand the agent's credentials to, because you are.

A viewer sees EVERYTHING the pane shows, past and future: scrollback, secrets
echoed by mistake, the lot. Mitigations already in fleet law: bound tokens live in
.envrc (never echoed), never inspect a wake process via argv (lesson
usage-prescribes-uninstalled-wake-127's detection note), and the agent image sets
HISTFILE off for the tmux shell. Residual risk is real and belongs to the owner's
judgement about WHO gets a grant — the design makes the grant explicit so that
judgement has a place to happen.

### 4.5 Failure modes

- Revoked-but-attached: the gate kills the process group on revoke; ttyd drops the
  socket. Verify in the smoke: revoke mid-session, client is gone < 1s.
- ttyd dies: viewers lose the bridge; the agent and its tmux are untouched (sidecar,
  not parent). Supervisor restarts it; grants persist in the launcher, not in ttyd.
- Owner deletes container: grants die with it (grant table keys on container id).
- Two drivers race (multi-driver=false): second writable attach is refused by the
  gate with a readable message naming the current driver, not queued silently.
  Browser-plane truth only (8385): an owner attached writable over ssh is outside
  the gate's bookkeeping, so the refusal may name nobody. Sanctioned — the ssh
  plane is the owner's own — but stated, not discovered.

### 4.5.1 Container networking — the private interface is not optional (architect, post-T2)

4.2 says ttyd "binds the container's private interface only ... never a host port" and
3.2 step 4 repeats it. That sentence constrains the LAUNCHER, not just the entrypoint,
and T2 shipped `--network host` as its default, which was returned as a blocker. Stated
so the invariant cannot be satisfied on paper and lost in a flag default:

- Agents and the broker share a user-defined docker network (`reveille`); the agent
  addresses the broker by container DNS name (`http://reveille-server:8765`). No host
  port is published in either direction, at any stage.
- With host networking a container HAS no private interface — it is the host's
  namespace — so ttyd binds every LAN interface of the box and the ingress is bypassed.
  It also collides at the second agent (one namespace, one port 7681, and the sidecar
  supervisor swallows the bind failure into a silent restart loop), which contradicts
  G1 directly: the marginal agent must cost what the marginal token costs.
- `--network` stays available as a deliberate ops override. It must never DEFAULT to
  host.
- The provisioning smoke provisions TWO roles and asserts both reach live+connected.
  A one-container smoke cannot see a namespace collision, which is exactly how this
  shipped green.

### 4.5.2 T3's audit is only as honest as what something actually observes

4.3 requires "one log line per attach/detach/revoke — who, container, mode, timestamp"
and calls the attach log the attribution boundary, which "must be honest and complete".
4.6 makes that harder to fake and easier to get wrong, so state the mechanism now:

- ATTACH is observable by the gate: it runs before the exec, holds the verified grant
  id and mode, and can write the line. Nothing else in the system knows a grant id.
- DETACH is NOT observable by the gate. `exec` replaced it with a tmux client, so the
  process that knew the grant is gone before the session ends. Nothing in the container
  watches anything (R3), by design. An implementation that silently emits no detach
  line, or infers one from a later poll and backdates it, produces a log that is
  complete-looking and false — worse than an admittedly partial one.
- RULING: the launcher owns detach and revoke lines, derived from the per-grant session
  disappearing (its `d-*`/`v-*` name IS the grant id) on the same tick that sweeps
  expired grants. The line records when the LAUNCHER observed the session gone, and
  says so — an observation timestamp, never a fabricated event time. If the tick is 30
  seconds, detach times are accurate to 30 seconds and the log must not imply better.
- A revoke line is written by the actor that performed it, at the moment it acts, and
  is therefore exact. Do not merge it with the swept-detach path; they have different
  precision and blurring them is how a log starts lying.

Grant records (launcher.db, 3.4) hold grant id, container, grantee, mode, issued and
expiry times — NEVER the minted token. The token is signable only with the container's
gate secret, which by R1's discipline the launcher does not persist; minting is a
docker exec into the container, and re-issue is re-mint, never retrieval. A launcher.db
that could reproduce a live grant token would be the standing-credential store 3.1
forbids, in yet another set of clothes.

Q3 attribution (status line names the driver) rides the same per-grant sessions: the
writable client's session name carries the grant id, so the status line resolves it
through the launcher's grant record to a person. Without 4.6's `d-<id>` session there
is nothing to resolve, which is the second thing that ruling buys.

### 4.6 Per-grant handles — what T1 left for T3, and the ruling (architect, post-T1)

T1 merged (94c6e3e) with the viewer path attaching to a per-grant grouped session
`v-<id>` and the driver path attaching to the shared session directly:

    exec tmux attach-session -t agent      # driver

That is correct for T1's gate (a driver can drive) and wrong for T3's, and the reason
is worth stating because it is not visible until you try to build revocation. After
`exec`, the gate process is GONE — replaced by a tmux client whose argv is
`tmux attach-session -t agent` for every driver, the owner's own ssh attach included.
A driver grant therefore leaves NO per-grant handle in the container, and R3's ratified
revocation path — "docker exec that kills the gate's process group for that grant" —
has nothing to aim at. Killing every client of session `agent` would revoke the owner
along with the grantee, which is not revocation, it is a boot.

RULING for T3: every BROWSER client attaches into a per-grant grouped session, drivers
included — `d-<id>` writable, `v-<id>` read-only. Consequences, all of which T3 wants
anyway:

- Revocation becomes `tmux kill-session -t d-<id>` (or `v-<id>`): pure tmux, no pid
  bookkeeping, no stale-pgid problem, and it targets exactly one grant. The <1s smoke
  measures a tmux operation rather than a process hunt.
- The OWNER's ssh plane attaches session `agent` directly and is therefore outside
  every grant handle by construction — revoking a grantee can never collateral the
  owner. That is the same boundary 4.5 already states from the other side.
- Driver exclusivity (4.3) becomes countable: refuse a second writable attach when an
  attached `d-*` session already exists unless multi-driver=true, and name the holding
  grant id in the refusal. Without per-grant sessions there is nothing to count, which
  is why 4.3's "refused with a readable message naming the current driver" was not
  buildable as T1 shipped.
- Audit (4.3) gets its attach/detach events from session lifecycle rather than from
  the gate, which no longer exists after exec.

Two related clarifications, so nobody "fixes" a non-defect:

- Q1 (attach at TAIL, scrollback is where echoed secrets live) is satisfied by `-r`,
  NOT by history-limit. A read-only client cannot enter copy-mode — tmux drops its
  input server-side — so a viewer sees the current screen and everything after it, and
  cannot scroll back. Do NOT set `history-limit 0` to chase Q1: grouped sessions share
  panes, so that would destroy the operator's own scrollback to constrain a client that
  already cannot reach it. Drivers can scroll back; a driver is the agent's whole
  identity (4.4) and scrollback is the least of what they hold.
- Token expiry is checked at ATTACH time only. A client that attached with 10 minutes
  left on a 24h grant stays attached indefinitely, which contradicts Q2's "an unwatched
  grant is a forgotten door". T3 owns the sweep: the launcher kills `d-*`/`v-*` sessions
  whose grant has expired on its regular tick. Expiry that only gates the doorway is
  not expiry.

## 5. Staging — each stage shippable, green gate, starts after DES-001 S4
(ruled parallel: S5/S6 interleave with T-stages; F3 still lands before S5)

- T1  image: tmux multi-client conf, ttyd + attach-gate in the image, wake on
      PATH, HISTFILE off, claude-login volume. Attachable by owner, ssh + browser
      (LAN). Gate: `reveille-launch` not required yet — image runnable by hand.
- T2  launcher daemon + CLI: new/stop/start/destroy/ls, provision flow 3.2,
      launcher.db, health-by-presence. Gate: provision one real agent end-to-end,
      token pasted, presence live+connected, zero broker changes (smoke_ws green).
- T3  grants: table + grant/revoke/flip CLI, per-grant URL tokens, audit log,
      revocation kill-path. Requires the 4.6 change to the merged gate: drivers
      attach into a per-grant grouped session (`d-<id>`) too, or revocation and
      exclusivity have no handle to aim at. Also owns the expiry sweep (4.6).
      Smoke: multi-client mirror, -r enforced server-side, revoke drops client
      <1s, driver-exclusivity race refused with the driver named,
      kill-and-reprovision resumes from bus state.
- T4  edge: publish ttyd + launcher UI through the tunnel behind Cloudflare
      Access. BLOCKED on the zone move (backlog item 1) — everything T1-T3 is
      LAN-complete without it.

## 6. Open questions — all resolved in the review round (8385); refute with
evidence or they stand

- Q1 RESOLVED: attach at TAIL by default — fresh grouped session, history-limit 0.
  Scrollback is where echoed secrets live. Owner opt-in per grant for history.
- Q2 RESOLVED: idle grants auto-expire at 24h, renewable. An unwatched grant is a
  forgotten door.
- Q3 RESOLVED: yes — status-line names the driver inside the pane. Transcript
  attribution is worth the noise.
- Q4 RESOLVED (declined, standing): operator pastes the bound token. The revisit
  ledger counts pastes per PROVISION, re-provisions included (R1). Bring a count,
  not a feeling.
- Q5 RESOLVED: the provision/destroy surface is the docker socket — root on the
  box — and NEVER goes through the tunnel in v1. The edge publishes ttyd and a
  read-only launcher status page only; provision/destroy stay localhost+ssh.
  If it ever goes to the edge: Cloudflare Access AND a launcher admin credential,
  two layers, the same standard viewers get.
- Q6 RESOLVED: one launcher per box, one TENANT per box, homebrew scope — full
  stop. The hosted story was rejected in DECISIONS; designing tenant isolation for
  a thesis we refused is speculative work. If that ever reverses, the honest
  answer is VM-per-tenant, not docker-daemon partitioning.

## 7. Review protocol — round CLOSED

Same as DES-001 §12. Round record: v2 posted (8379), senior-dev reviewed (8385) —
4 ACK (boundary shape, provision flow with the heartbeat-free verification,
attach mechanism ladder, Q4 declination), 3 refutations ratified verbatim into
sections 3.1/3.3/4.3-4.5 (R1 token retention, R2 volume scope + purge smoke,
R3 signed-offline grants + launcher-side revocation kill), Q1-Q6 resolved.
Architect ratified; DES-002 is ACCEPTED. T1 is GO, parallel with DES-001 S5/S6.
Future changes go through a new round.
