# DES-002: Container launcher — web-provisioned agent containers with shared tmux

Status: DRAFT — sections 1-3 and 8 are skeleton, posted early because section 4
(tmux sharing) was specified first at the operator's request. The launcher core gets
its own adversarial round before any stage ships. Companion to DES-001 (memory layer,
ACCEPTED); this doc is the compute/attach layer only.

## 1. Problem (skeleton)

An agent today is a tmux pane a human hand-builds: clone, .envrc, MCP registration,
wake waiter, Stop hook. That is repeatable but not provisionable — an operator cannot
mint "one more dev on project X" from the web the way they mint a token. And the
session is invisible: guiding a coding agent means being the person at that terminal.
DECISIONS already fixes the shape: one agent = one container = one git identity =
one (bound) token. The launcher makes that shape a button.

## 2. Goals (skeleton)

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

## 3. Components (skeleton — each gets full treatment in its own round)

- provisioner: web-triggered; creates container from the agent image, injects
  identity env (bound REVEILLE_TOKEN, REVEILLE_AGENT_ROLE), clones the repo,
  registers MCP, arms the boot ritual.
- agent image: docker/ already builds one (docker/Dockerfile, entrypoint.sh,
  spike_join.py proved join-with-identity works — DECISIONS "no knowledge
  migration").
- tmux + ttyd sidecar: section 4.
- lifecycle: stop/kill/re-provision; per-tenant quotas ride the platform work
  (backlog: ZFS quota, cgroup caps).

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

- ttyd never execs `tmux attach` directly. It execs `attach-gate <grant-id>`, a
  small wrapper that (1) validates the grant against the launcher's grant table,
  (2) execs `tmux new-session -t visitors -s v-<grant>` for viewers WITH `-r`, or
  a writable attach for the driver, (3) drops the connection when the grant is
  revoked (revocation kills the wrapper's process group: a revoked viewer
  disappears mid-keystroke, which is the point).
- The mode rides the SERVER side of the WebSocket. A visitor cannot flip their own
  `-r` off: ttyd's writable flag stays off globally; write capability exists only
  through the gate's exec choice. Client-side anything is decoration.
- Auth to ttyd: per-grant URL token issued by the launcher UI, single audience,
  short TTL, renewed while the grant lives — layered under Cloudflare Access at
  the edge (tunnel plan, backlog item 1). Neither layer alone is the gate.

Audit: one log line per attach/detach/revoke — who, container, mode, timestamp.
tmux cannot attribute keystrokes inside the pane (known, accepted for pairing);
the attach log is the attribution boundary, so it must be honest and complete.

### 4.4 What a watcher sees — stated, not hidden

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

## 5. Staging (section 4 only; launcher core stages TBD with sections 1-3)

- T1  image: tmux.conf (window-size largest, aggressive-resize), ttyd sidecar,
      attach-gate wrapper, HISTFILE off. Attachable by owner, ssh + browser.
- T2  grant table + launcher UI (grant/revoke/flip person, viewer default,
      exclusive driver), per-grant URL tokens, audit log.
- T3  revocation kill-path + smoke: multi-client mirror, -r enforcement, revoke
      drops client, driver exclusivity race.
- T4  edge: publish through the tunnel behind Cloudflare Access (blocked on
      backlog item 1, the zone move).

## 6. Open questions

- Q1 Does a viewer grant include scrollback from before the grant, or attach at
  tail? (tmux gives history to any client; trimming it means a fresh grouped
  session per grant with history-limit 0 — cheap, worth deciding deliberately.)
- Q2 Idle grant TTL: none vs auto-expire after N hours unwatched?
- Q3 Is the driver's identity surfaced INSIDE the pane (status-line "driver: bob")
  so the agent's transcript shows who was driving when? Cheap, honest, mildly noisy.

## 7. Review protocol

Same as DES-001 §12: fleet round, slice ACK or refutation with file:line/section,
architect ratifies, ACCEPTED before T1 ships. Sections 1-3 must be fleshed to full
detail before that round opens; section 4 is ready for review now.
