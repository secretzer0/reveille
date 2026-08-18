# DES-006: One front door — single-origin UI, credentials, attach-through-proxy

Status: ACCEPTED — operator directive 2026-07-29 (relayed via msgs 8494/8495).
Amends DES-005 §2 (two services, two origins) and DES-004 M3's Option-A
rejection, narrowly and on the record. Does NOT amend DES-002 G4.
Amended again 2026-07-29 (operator msgs 8542/8549/8552, architect ruling
8555/8557) — see §6: the Agents page moves into the bus's own content well.

## 1. Problem

Three complaints, one root: **the process split leaked into the product.**

- Agent management lives on a different port. The operator's verdict: a
  terrible UX choice — and they are right. The two-service split is an
  *implementation* decision; the user should never have to know it happened.
- "How does this work with authentication?" It already works (session cookies
  are host-scoped; ports are not part of cookie scope, so `samesite=lax` is
  satisfied — same host, same site). The fact it had to be *asked* is the
  defect: nothing on the page says so.
- **No UI for credentials.** DES-005 P2 shipped the entire backend — masked
  `GET/PUT /profile`, per-agent overrides, `0600 profile.json`,
  override > global > request resolution, all proven in the API smoke — and
  the page never grew a credentials section. That is a straight gap from the
  P3 slice shape, and it blocks the operator's real workflow today.

## 2. Rulings

### 2.1 REJECTED: broker calls the launcher (trust inversion)

The operator floated folding the UI into the broker and making the launcher a
pure API the broker calls. **Rejected**, and the reason is not G4 — it is the
credential shape:

> Today, the ONLY thing that can command provisioning is a **live user session
> cookie, forwarded per request**. No standing machine credential to create
> containers exists anywhere in this system.

That property is rare and worth defending. Under the inversion, the
internet-facing broker holds a standing credential meaning *"create containers
on this host"* — so a broker compromise commands provisioning **forever**, not
just for the duration of one user's session. It also puts docker-awareness in
broker code, breaking standalone no-docker deployments and the smoke that
guards G4.

The operator's own framing is the resolution: the **invisible** process split
is fine, the **visible** origin split is not. Fix the visibility, keep the
split.

### 2.2 AMENDED, narrowly: the broker may render ONE configured nav link

DES-004 M3 rejected Option B because it would put the launcher's address into
broker-served HTML. Under a single origin the link is a **path**, not an
address — but a bare hardcoded `/agents` in broker HTML still asserts that
something lives there, and 404s on a standalone deployment.

The amendment, therefore:

- The broker gains **one optional configuration value** — an extra nav link
  (label + relative path). Empty by default; when empty, nothing renders.
- The broker learns **"there is a link"**, never **"there is a launcher"**.
  No launcher semantics, no launcher address, no dependency: a standalone
  broker with the value unset is byte-identical in behavior to today.

The invariant I actually defend is *the broker never depends on the launcher*.
That survives. "The broker may never render a link an operator configured"
was never the invariant.

### 2.3 ACCEPTED: attach-through-proxy — and it fixes a live hole

`/attach/<agent>` routes to the **launcher**, which forwards the WebSocket to
that container's ttyd. This is not only cosmetic:

> Today's grant URLs carry container-network addresses and only work from the
> host machine. Every grant handed to a remote human is currently broken.

Conditions, both required:

- **Two independent gates.** The launcher refuses unless (a) the request
  carries a valid session principal AND (b) that agent belongs to that user —
  the same by-construction ownership P1 established. The **grant token still
  gates at the container**, unchanged (`attach-gate` verification is
  untouched). The launcher is a dumb pipe between two checks, never an
  authority of its own.
- **Never an open proxy.** It forwards only to container addresses it
  resolved itself, for agents it owns records for. No client-supplied host,
  port, or path reaches the forward target.

### 2.4 Launcher binds loopback only

`127.0.0.1` — strictly tighter than the `0.0.0.0` I started it on today.
Unreachable from the LAN and from the docker network (containers reach the
host at the gateway address, not loopback). The proxy is the only path in.

## 3. Shape

```
                    ┌──────── one origin ─────────┐
browser ──────────► │  proxy (caddy)              │
                    │   /          → broker UI    │──► broker  (0.0.0.0:8765)
                    │   /agents    → Agents page  │──► launcher (127.0.0.1:8766)
                    │   /attach/*  → launcher ────┼──► ttyd in the container
                    └─────────────────────────────┘
```

**`/` is the BROKER** (operator ruling 2026-07-29). The bus — chat, rooms,
memory, presence — is the daily surface; agent management is an occasional
second step. An earlier note of mine (msg 8493) recommended the launcher as
the home page; that was a workaround for the constraint in §2.2, which this
document removes. With the broker able to render a nav link, the natural
ordering wins and the workaround is withdrawn.

The configured nav link therefore carries real weight: it is the discovery
path from the landing page to agent management. The Agents page links back to
`/` the same way.

- The **proxy** knows both addresses. Neither service learns the other's.
- The user types one address, sees one product, never types a port.
- This is the local form of what DES-002 T4 already plans at the Cloudflare
  edge — the same routing, one layer down.

**Amended by §6**: the proxy's route split is unchanged, but the bus's own
JS now also renders an embedded presentation of `/agents`'s functionality
inside the bus's content well, calling the launcher API directly. `/agents`
itself is untouched — see §6.3.

## 4. Slices

- **U1 — credentials UI.** A CREDENTIALS block on the Agents page: claude
  token, github token, default repo. Masked set/absent display (never the
  value), empty string clears, plus a per-agent override affordance on each
  agent card. **Zero new endpoints, zero new authority, deputy call set
  untouched** — P2's backend already does all of it. Gate: real-browser pass —
  set a global, override it on one agent, provision, and assert the container
  env carries the override; the value appears in no response body.
- **U2 — launcher supervision + loopback bind.** The launcher currently runs
  as a background task of an interactive session and dies with it. Same
  treatment the waiter got (DES-003): flock-guarded, hook/entrypoint-spawned,
  no systemd. Bind `127.0.0.1`. Gate: kill it, confirm it comes back without a
  human; confirm it is unreachable from the LAN address.
- **U3 — single origin.** Caddy config with the three routes (`/` broker,
  `/agents` launcher, `/attach/*`), the one configured nav link in the broker
  pointing at `/agents` (§2.2), a link back to `/` from the Agents page, and a
  dim line there stating *signed in via your broker session* (§1). Gate: from
  a clean browser, one address lands on the bus, one click reaches agents, one
  login covers both, and no port is ever typed.
- **U4 — attach-through-proxy.** §2.3, with both gates and the
  no-open-proxy property. Gate: a grant URL works from a machine that is NOT
  the host — the hole this closes — and a request for an agent the session
  does not own is refused before any forward is attempted.
- **U5 — styling/nav pass** so the two pages read as one product.

Order: **U1 → U2 → U3 → U4 → U5.** U1 unblocks the operator's workflow today
and carries no risk; U2 is hygiene that everything else assumes; U3 delivers
the UX; U4 is the largest and fixes remote grants; U5 is polish.

## 5. What does not change

- The broker still never learns docker exists (G4, smoke-guarded).
- No standing machine credential anywhere commands provisioning (§2.1).
- The deputy call set stays at its five, bounded by kind (DES-004 M3 ruling).
- Grant verification stays at the container; the launcher never becomes an
  authority over attachment.

## 6. Amendment (2026-07-29): the Agents page moves into the bus's content well

Operator ask (msgs 8542, 8549, 8552, annotated screenshots): U3 fixed the
login/origin split, but Agents is still a *navigation away* — a distinct page
reached by a link, not a view inside the product the operator actually lives
in. "This entire page should be in the content well of the primary app, not a
secondary thought." Agent management (create/start/stop/destroy/reconfig) is
only one piece of it; full tmux interaction per running agent belongs in the
same content well too.

### 6.1 RULING: merge the UI, keep the services split (architect, msg 8555)

Corrects a framing error in how this was first pitched to the architect: what
§2.1 actually protects is not "two front-ends" — it is (a) the broker never
learning docker exists, and (b) no standing machine credential commanding
provisioning. Neither is about which DOM renders a button. §4 U5's "copied not
shared" tokens were a runtime-asset-dependency argument (the launcher page
must not 404 its stylesheet when the broker is down) — never a promise the
two UIs stay separate artifacts forever.

So: **the bus's own JS may call the launcher's HTTP API directly** (`/agents`,
`/agents/{agent}`, `/agents/{agent}/{verb}`, `/agents/{agent}/grants`, ...)
from its content well — same origin behind the proxy, authenticated with the
SAME session cookie the user already holds. The launcher still runs its own
two gates (session principal + agent ownership, §2.3) on every call,
unchanged. What must never happen, on pain of reopening §2.1: broker CODE
(`daemon.py`) gaining a launcher URL, a docker import, or any docker-adjacent
knowledge. The browser doing the fetching is not a new authority — it always
held the user's authority; only where the DOM renders changes.

**REJECTED**: embedding the launcher's existing pages via iframe. Fights the
proxy/WS work U4 already did, and buys back a separation property that was
never load-bearing (see above).

### 6.2 `/agents` is kept, not replaced

U6 (below) adds a second, embedded presentation of the same functionality; it
removes nothing. `/agents` stays fully functional as a direct link and as
`tests/launcher_api_smoke.py`'s target — that gate exercises the API, not
either page, so it continues to pass unmodified regardless of what the bus's
content well does.

### 6.3 Terminal tabs — OPEN, gated on the sweep landing

The operator also wants each running agent's live tmux session reachable as a
tab inside the same content well, several open at once. Before committing to
a design, the grant/session lifecycle was read directly (`docker/attach-gate`,
`sweep_actions`, `_sweep_once`, DES-002 §4.6) rather than assumed:

1. **Mint once per (browser session, agent), cache client-side** (e.g.
   `sessionStorage`), reconnect the cached token on reload, and re-mint only
   on rejection. Minting fresh on every tab-open is wrong: attach-gate's
   per-container driver exclusivity (`docker/attach-gate` attach(), the
   create-race re-check included) refuses a second `d-*` session for the same
   agent regardless of who holds the first — so a browser's own earlier tab
   would lock out its own new one.
2. **Driver exclusivity is per-container tmux state**, indifferent to which
   browser or grantee holds the grant. Unaffected by one browser holding
   several concurrent driver grants for *different* agents. A second driver
   tab for the *same* agent is correctly refused today, same as it would be
   from two separate humans; the manager must surface that refusal (today it
   arrives as literal terminal text, since attach-gate's `die()` is ttyd's
   stdout) rather than swallow it.
3. What was expected to be the hard part — a crashed/closed tab's session
   lingering until the sweep tick reclaims it — turned out to be gated on a
   bigger, pre-existing gap: **the sweep tick has never been scheduled
   anywhere** (confirmed by the architect on three axes, msg 8557: not in a
   deploy unit, not started by `cmd_serve`, not in any crontab). Consequences
   land beyond this feature — grant TTLs are decorative for already-attached
   sessions, and DES-005 §7.1's idle-container-stop policy has never once
   executed. That fix is owned by the architect and routed to senior-dev; it
   is **not part of this slice**. See lesson
   `periodic-task-proven-correct-never-scheduled`.

**CLOSED.** The sweep is scheduled: `cmd_serve` starts `_sweep_forever` as a
daemon thread before `uvicorn.run`, unconditionally, and the serving launcher
names its interval in the boot banner. Findings 1 and 2 above are therefore the
whole design — a crashed tab's orphaned session dies within one tick, and
terminal tabs are a layout change with no scheduling prerequisite left.

### 6.4 Slices (extends §4)

- **U6 — embedded agent manager.** The bus's content well gains a view that
  renders agent cards (create/start/stop/destroy/creds) via direct fetches to
  the launcher's existing API, reusing `/agents`'s request shapes unchanged.
  `/agents` itself is untouched (§6.2). Gate: from the bus, reach
  create/start/stop/destroy/creds without leaving `/`; the existing
  `launcher-api-smoke` gate passes unmodified.
- **U7 — lifecycle states + legal actions (architect GO, msg 8591).** Three
  states, matching what `launcher.db` + docker actually hold: RUNNING (watch,
  stop, destroy), STOPPED/exited (start acting on the stored definition,
  destroy), and BROKEN — a record whose container is gone (out-of-band
  `docker rm`, host GC), reported rather than silently offered as STOPPED
  (self-heal-or-report-absence doctrine); destroy is the only legal action on
  it. UNDEFINED and DESTROYED are not row states — creation is a page-level
  action (the New Agent form moves behind a collapsed `<details>` disclosure
  on both `/agents` and the embedded pane, so it is never visually adjacent
  to a managed row) and a destroyed agent's row simply stops existing. Destroy
  now requires typing the agent's exact name to enable the button (was
  click-confirm) and takes `?purge=1` — the modal states both halves of
  DES-005's split by name: container + local repo checkout gone, hive memory
  kept. Embedded pane's fail-soft now distinguishes a completed HTTP error
  response (`the launcher returned 404 for /agents`) from true
  unreachability, per the U6 postmortem (msg 8589/8591). Gate: 205 units,
  ruff clean; no real-browser/docker-socket pass from this container (same
  gap named on U6 — needs verification against a live launcher+docker stack
  before merge).
- **U8 — terminal tabs.** UNBLOCKED — §6.3's prerequisite is closed, the sweep
  runs inside `serve`. Terminals live on ONE page, in the content well: not
  browser tabs, not `window.open`. The Agents control switches the whole LEFT
  RAIL into manage-agents mode; the rail is the ROSTER (every agent, with its
  state, plus a create item) while the well's tabs are the terminals currently
  OPEN — file-tree-and-open-editors, one navigation model, not two selectors
  for one thing. An iframe is available and is the point: nothing on the attach
  path sets `X-Frame-Options` or `frame-ancestors`, and it is all one origin.
  Gate: N tabs across 2+ agents attach concurrently with no cross-agent
  interference; a second driver tab for an already-held agent is refused and
  the manager shows the refusal rather than a blank tab; killing a tab's
  browser process (no cleanup handler run) results in the session being
  reclaimed within one sweep tick.

### 6.5 What does not change (extends §5)

- Everything in §5 still holds.
- The launcher's two-gate check (session principal + agent ownership) runs on
  every call U6 makes — U6 adds a caller, never a bypass.
- Grant verification stays at the container (`attach-gate`); U6/U8 never
  become a new authority over attachment.

### 6.6 REQUIRED by §6.1: the bus degrades when the launcher is down

§6.1 relaxes *where the DOM renders*, and in doing so it moves the launcher
from "a page you navigate to" into "a fetch inside the page you live in".
That relocates a failure: previously a dead launcher meant one link 404'd and
the bus was untouched. Embedded, a dead launcher is a failing fetch **inside
the bus's own content well**, and the naive rendering makes the whole product
look broken when only agent management is.

So this is a condition of the ruling, not a nicety:

- The embedded view **fails soft**. A launcher that is down, refusing, or
  absent renders as an inline message in that view — *agent management is
  unavailable* — while chat, rooms, memory and presence continue to work
  untouched. No global error state, no blocking modal, no spinner that never
  resolves.
- **A broker deployment with no launcher at all is a supported configuration**
  and must look deliberate rather than broken: with the nav link unset (§2.2)
  the view is never reachable, and if it is reached anyway it says so plainly.
- U6's gate therefore includes: **stop the launcher, load `/`, and confirm the
  bus is fully usable** with the agent view showing its unavailable state.

The property §2.1 protects has always been *the broker never depends on the
launcher*. Merging the UI is allowed precisely because the browser — not the
broker — does the fetching; that stays true only if a failed fetch degrades
one view instead of the product.

### 6.7 The serving launcher is not a working tree (drafted per architect ruling, msg 8568)

U2 gave the launcher the waiter's supervision — flock-guarded, hook-spawned, no
systemd — and left one thing unstated: *which tree it is spawned from*. The Stop
hook spawned `$repo/scripts/reveille_launch.py`, where `$repo` is the hook's own
location, i.e. a developer's checkout. Consequences, both observed on the live
box: the operator's launcher was running an unreviewed feature branch, and
`git checkout` — a read-only act, performed while REVIEWING a branch — was
silently a deployment. Nobody could answer *what is serving?* without asking a
person which branch they were on.

Binding:

- **`~/.reveille/launcher.env` must declare `REVEILLE_LAUNCH_REPO`**, and the
  supervisor spawns from that path only. It is deliberately not defaulted to the
  hook's own repo: the default is what caused this. Undeclared → no spawn, said
  plainly in `launcher.log`, because a launcher serving unreviewed code is worse
  than a launcher that is down.
- **`reveille-launch pin` maintains that path** as a clone that only ever
  fast-forwards `main`, syncing its venv (the venv is part of the artifact:
  `serve` imports uvicorn and starlette). It refuses a tree with local edits and
  refuses any move that is not a fast-forward — a pin is a deployment, so it
  refuses what it cannot describe, and both refusals leave the running code
  exactly where it was.
- **The startup banner names version, commit, branch and source path**, so
  *what is serving?* is answerable from the log rather than from a person. A
  `+dirty` suffix is deliberate: an operator seeing it knows the tree was
  touched by hand.
- **Gate (`make launcher-pin-smoke`)**: with the service up, rewrite the dev
  tree's launcher and assert the serving process is unaffected — same pid, still
  answering, same commit. That property is the whole point.

The broker already had this discipline — it runs from `reveille-server:<version>`,
an immutable image — and it was simply never extended to the second service.
Containerising the launcher is the other honest answer; it needs the docker
socket, which is a security decision that has not been made, so the pinned clone
is the shape until it is.

## 7. Amendment (2026-08-18): upgrade in place -- carry, not park (ruling 11600)

Every agent-image bump used to mean a per-agent recreate by the owner, pasting
the agent's bound token, because the launcher keeps no token at rest
(`launcher.db` token-free, secrets by env name). The token is on the host anyway,
in the container's own `Config.Env`, readable by the launcher user -- so the rule
bought nothing against a same-host reader and cost a dance per bump (operator
11594/11599).

Binding -- one invariant, not special cases: **a bound token exists in exactly two
places, the broker's store and the env of a container the launcher provisioned;
the launcher may CARRY it between those, never PARK it.** Never-at-rest is
unchanged (no db, log, file, HTTP body, journal); this is what provision already
did in-process, and what `read_container_config` already read. A host-side token
file (0600) stays rejected. Consequences, all built as `upgrade_agent`:

- Read source = `Config.Env` of `rev-<user>-<agent>`, only with a `launcher.db`
  record for (user, agent) and, over HTTP, the owner's session (2.3). CLI
  `reveille-launch upgrade USER AGENT [--image]` and `upgrade --all` (host
  operator; walks `launcher.db`, never `docker ps` -- the launcher never adopts a
  container it did not create). `reveille-launch behind` lists what `--all` would
  walk; `make up` prints it and stops (no auto-roll: that is a separate ruling,
  it needs an idle rule).
- The value rides the docker child's ENV (`ENV_PASSTHROUGH_SECRET`, no argv); the
  gate secret rides with it so grants signed against it survive. Nothing
  token-shaped in `launcher.log`, `launcher.db`, the audit line or the HTTP
  answer (gate: the token string is absent from every one after a full run).
- Never two containers holding driver state: OLD is stopped, renamed aside, and
  NEW starts under the real name on the same data-root bind. Health before
  destroy = NEW running AND its boot report present AND the broker's presence
  shows the agent (the same `wait_healthy`); else destroy NEW, put OLD back
  (started again if it was running), surface the reason. Only then is OLD removed
  and the record's image updated (`UPGRADE` on the audit line).
- The same agent: data-root inode equal; `Config.Env` set-equal on every
  `REVEILLE_*` name+value and `ANTHROPIC_MODEL` (image-derived variables may
  change); same repo, boot command, network, quotas. Corollary: the agent image
  must never BAKE a `REVEILLE_*` ENV (the Dockerfile has none) -- an image that
  did would make every upgrade to it read as "carried env differs" and roll back.
- Over HTTP the upgrade runs off the event loop (a thread with its own db
  connection): docker stop plus the health wait is up to two minutes, and the
  loop serves every user's `/agents`. The answer still waits for from -> to.
- A purged container (no env to read) is today's prompt path, verbatim
  (`new --replace` / the Agents form). A token the broker refuses (401/403 on
  presence) is refused: "token dead, re-provision" -- a dead credential is never
  rolled forward.
- `/agents`: one "upgrade" button per container whose recorded image differs from
  the launcher's default; the same call, the answer names images only.
