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

This section stays open until the sweep is actually scheduled and running.
Once it is, findings 1 and 2 above are the whole design — a crashed tab's
orphaned session dies within one tick, and terminal tabs become the layout
change they were originally pitched as.

### 6.4 Slices (extends §4)

- **U6 — embedded agent manager.** The bus's content well gains a view that
  renders agent cards (create/start/stop/destroy/creds) via direct fetches to
  the launcher's existing API, reusing `/agents`'s request shapes unchanged.
  `/agents` itself is untouched (§6.2). Gate: from the bus, reach
  create/start/stop/destroy/creds without leaving `/`; the existing
  `launcher-api-smoke` gate passes unmodified.
- **U7 — terminal tabs.** BLOCKED on the sweep fix (§6.3). Gate once
  unblocked: N tabs across 2+ agents attach concurrently with no cross-agent
  interference; a second driver tab for an already-held agent is refused and
  the manager shows the refusal rather than a blank tab; killing a tab's
  browser process (no cleanup handler run) results in the session being
  reclaimed within one sweep tick.

### 6.5 What does not change (extends §5)

- Everything in §5 still holds.
- The launcher's two-gate check (session principal + agent ownership) runs on
  every call U6 makes — U6 adds a caller, never a bypass.
- Grant verification stays at the container (`attach-gate`); U6/U7 never
  become a new authority over attachment.
