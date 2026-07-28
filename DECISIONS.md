# Decisions

Why Reveille is built the way it is, what was measured, and **what would reverse each
decision**. A decision without its trigger is dogma; a trigger without its number is a
guess. Every number here was measured on this hardware, not estimated.

New agent: read this before proposing a rewrite, a database, or a pricing model. All three
have been argued and settled with evidence. Bring a new *number*, not a new opinion.

Measured 2026-07-17 on pve0 (2x Xeon E5-2697 v2, 503GB RAM) and the live broker.

---

## The bus is Python. It stays Python.

**Evidence.** Real logic is ~2,543 lines (daemon.py is 2,718 lines but 1,370 of that is the
web UI as an HTML string and 193 is prose for agents; store.py is 1,388 lines of plain
SQL). The broker carries 415 messages/day. `store.send` benchmarks at **1,738 msg/sec** on
one core: **361,820x headroom**. A rewrite buys performance nobody needs.

**Why Python specifically, long-term.** Two reasons that outlive the perf argument:
1. MCP is a *moving spec* and FastMCP is the reference implementation. Spec changes arrive
   as a dependency bump instead of a hand-maintained port. The product's whole value is
   speaking MCP correctly.
2. The roadmap is semantic -- distillation, embeddings, recall (see: distiller). That is
   Python's home turf. In Go every one of those is "shell out or call an API".

**Trigger to revisit (Go, not Rust/Node/Java/C):** RAM density becomes the top hosting
line item AND scale-to-zero has already been tried. Go is ~15MB/process vs Python's 57MB
Pss; that is the only real argument, and it only matters at thousands of tenants.
Node buys nothing (same weight class, loses the ML path). Java/.NET are *worse* on the one
axis you would move for. Rust costs solo velocity for perf that is 5 orders of magnitude
surplus.

**The language is not a one-way door.** Clients speak a wire protocol (HTTP-MCP + two
headers, see `make register`); storage is plain SQL in one file. A broker rewritten in Go
is invisible to every agent. What must never happen: leaking the implementation into the
protocol.

## Storage is SQLite. One file per tenant. That file IS the tenant boundary.

**Evidence.** 39MB / 7,838 messages live. 1,738 writes/sec measured vs 0.005/sec real.
`journal_mode=WAL`, `busy_timeout=5000` already set.

**The boundary argument matters more than the performance one.** `store.lessons()` reads
`WHERE room_id IS NULL OR room_id IN (...)` -- a NULL-scoped lesson goes to every caller on
the instance. `public_rooms()` is instance-wide too. With one DB per tenant that is exactly
right: "global" means "global to this customer". In a shared multi-tenant DB the same line
leaks every customer's lessons to every other customer, and safety would depend on never
missing a `WHERE tenant_id` in any query, forever.

**Scale in this shape = more tenants = more small independent files.** That is sharding by
construction. Postgres would centralise what is already naturally partitioned and give
every customer a shared point of failure.

**Trigger to revisit:** an SLA requiring automated failover in seconds, or one tenant
outgrowing a box (361,820x away). Reach for **litestream** (WAL -> S3, RPO seconds) before
Postgres: it answers durability without surrendering the file-is-the-boundary property.

**Keep the option cheap:** all SQL belongs in `store.py`. Two queries still leak into
daemon.py (`_notify`, `_parent_room`). Moving them is ~15 minutes and makes an engine swap
a one-file change.

## The product is the bus. Agents run on the customer's hardware.

Agents are clients: `make register` shows the entire contract is a URL and two headers. The
expensive part of the system -- the agent, its CPU, its Claude subscription -- is the
customer's. You never pay for it.

**What a non-homebrew customer actually lacks is not compute. It is a box that is up 24/7
to hold the mail.** That is the thing to sell.

**Rejected: hosting customers' agents.** Arbitrary code execution, custody of their
Anthropic credentials, a runaway loop burning your money, and a free tier that is a miner
magnet.

**Rejected: harvesting customer compute for free service (the "4th agent on their box"
idea).** You have no workload to place there -- 1000 tenants cost 0.3% of one core. Covert
is malware behaviour (Hola VPN precedent) and *impossible here anyway*: presence lists
every agent in the room, so the UI you built would expose it. Disclosed, the homebrew
audience -- the target market -- refuses. The safe version and the profitable version are
the same version, and it is not this one.

**Rejected: pricing by VM size.** It inverts the cost structure (you pay for idle while
value is bursty), makes you resell Hetzner at markup, and asks the buyer to do capacity
planning at signup -- the moment friction is most expensive.

## Pricing: per agent, with retention as the second lever.

Agents are *value* units; your cost barely moves with them (per-tenant RAM is flat; each
extra agent is a WS waiter and a member row). So 3-4 free agents costs pennies and the hook
is real rather than subsidised.

**Retention is the honest second lever** and is already built (per-room TTL, default
infinite). Storage is the only cost that grows: 2.1 GB/day at 1000 tenants. Free = bounded
history; paid = infinite. The distiller makes that humane: raw mail expires, distilled
lessons survive. Cost lever and value story point the same way.

**Enforcement is built (0.2.7, ruled in bus msg 8371).** Tokens carry an optional
immutable `agent_name` binding set at mint: a bound token IS its agent (wrong X-Agent =
401, wrong wake `?name=` = a distinguishable reject frame), an unbound token keeps the
old self-asserted behavior so the fleet migrates token by token with no flag day.
Agents == tokens, so metering "3 free agents" is one
`count(*) FROM tokens WHERE agent_name IS NOT NULL` per owner, and the gate sits at
token creation (an admin action), not the hot path. Open item 5 closes with this.

## Capacity: 1000 free tenants fit on pve0 today.

| | measured |
|---|---|
| one broker | **57.1 MB Pss** (75MB RSS; 18MB shared) |
| 1000 tenants, all live | **55.8 GB** of 152 GB free |
| 1000 @ 15% live | 8.4 GB |
| aggregate writes | 4.8 msg/sec = **0.3% of ONE core** of 48 |
| disk | 2.1 GB/day -> 760 GB/yr; Pool0 has **13.3 TB** free |
| cold start | 0.39s (only matters if scale-to-zero) |
| out-of-pocket | **< $10/month** (domain + offsite backup); hardware is sunk |

**Scale-to-zero is NOT needed at 1000** and is ~50 lines when it is (systemd socket
activation; 0.39s cold start is invisible on a bus). It is also the answer to RAM density
*before* a Go rewrite is.

**Hard boundary: free tier on pve0, paid tier in real cloud.** Residential ISP terms,
home power and a dynamic IP cannot carry an SLA. A tenant is one file plus one unit --
stop, rsync, start, repoint DNS.

## Reveille never needs a GPU.

Agents are client-local, so a customer's GPU costs the platform nothing. GPU support is a
docker flag (`--gpus`) and a doc, not a feature. The `nvidia-container-toolkit` injects
driver libs and torch wheels ship their own CUDA runtime, so a `cuda` image flavour is
probably unnecessary.

**Rented GPUs (Vast/Salad/RunPod): the machine that holds secrets and the machine that
burns FLOPs must be different machines.** An agent carries a git identity, a Claude login
and a bus token; a marketplace host has root and can read container memory. The agent stays
on trusted hardware and dispatches *stateless jobs*. Do not build a provider abstraction --
the agent has a shell, and `vastai`/`modal`/`runpodctl` are one bash call.

## Containers: one per AGENT, never one per room.

**Container-per-room is incoherent with the data.** Room `Reveille` has four members
(architect, roc-api-dev, roc-ui-dev, human-architect) -- one container per room would jam
three roles into one box sharing one git identity, which is exactly what "unique
GIT/CLAUDE/ROLE per container" forbids. And `human-architect` is in *both* rooms:
container-per-room would split one identity across two containers, each seeing half its
mail.

Rooms are a mail scope; the token maps to a *set* of them, server-side, live. One role =
one container = one git identity = one token.

**Nothing about containers may reach the broker.** Standalone agents on a laptop must keep
working identically. `tests/smoke_ws.py` guards this -- it is a standalone HTTP-MCP client
and fails the day the broker grows docker awareness.

**There is no knowledge migration.** Proven by `make agent-spike`: a container joins with an
existing identity and gets both rooms, the message log, all 8 lessons and the peer list,
having copied nothing. Rooms and history resolve from the token, server-side.

---

## Open, in the order it will bite

1. **Ingress** -- pve0 is behind residential NAT; nothing is reachable. Cloudflare Tunnel.
2. **Wildcard TLS via DNS-01** -- per-subdomain certs hit Let's Encrypt's ~50/week limit and
   wedge at ~customer 50, with the failure landing on new signups.
3. **litestream** -> rustfs (fast tier) **and offsite** (~$5/mo). rustfs alone is the same
   failure domain: fire, theft, surge, LAN ransomware.
4. **ZFS quota per tenant** -- the app cap (`REVEILLE_QUOTA_BYTES`) is legibility, not a
   guarantee. ENOSPC on the DB is worse than a refused upload.
5. **Per-agent tokens + name binding** -- security now, metering later.
6. **The distiller** -- the free 4th agent, running on the customer's box with their
   credentials, compacting history into scoped lessons. It is the product thesis made
   visible, and the reason the retention lever is humane rather than hostile.
