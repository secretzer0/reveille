# DES-006: One front door — single-origin UI, credentials, attach-through-proxy

Status: ACCEPTED — operator directive 2026-07-29 (relayed via msgs 8494/8495).
Amends DES-005 §2 (two services, two origins) and DES-004 M3's Option-A
rejection, narrowly and on the record. Does NOT amend DES-002 G4.

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
                    ┌──────── one origin ────────┐
browser ──────────► │  proxy (caddy)             │
                    │   /          → Agents page │──► launcher (127.0.0.1:8766)
                    │   /bus       → broker UI   │──► broker  (0.0.0.0:8765)
                    │   /attach/*  → launcher ───┼──► ttyd in the container
                    └────────────────────────────┘
```

- The **proxy** knows both addresses. Neither service learns the other's.
- The user types one address, sees one product, never types a port.
- This is the local form of what DES-002 T4 already plans at the Cloudflare
  edge — the same routing, one layer down.

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
- **U3 — single origin.** Caddy config, the three routes, the one configured
  nav link in the broker (§2.2), and a dim line on the Agents page stating
  *signed in via your broker session* (§1). Gate: from a clean browser, one
  address reaches both UIs with one login and no port ever typed.
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
