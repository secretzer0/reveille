# DES-010: The VM pulls its own releases, and the swap loses nothing

Status: RULED — operator directive 2026-08-15 (msg 10926), architect rulings
(msgs 10877, 10913, 10928), devops proposal (msg 10927). Companion to DES-002
(the container stack) and DES-008 (native agents). Consumes the CI/CD ruling
(msg 10877) and the update-channel conditions (msg 10913 §6).

## 1. Problem

The operator wants a release that reaches main to reach the running server
without a human typing a deploy command, and wants zero downtime designed in
from the beginning rather than retrofitted.

The request as stated — "place a github agent on the VM" — names a mechanism
that must not be built. Everything below follows from keeping the outcome and
inverting the mechanism.

## 2. RULED: the VM pulls. GitHub never reaches in.

No self-hosted GitHub runner on the bus VM. The repo is PUBLIC, and a
self-hosted runner exists to execute what a pull request tells it to — on the
machine holding the live database, every room's history, and the credentials.
Scoping the runner to non-fork events narrows the hole; it does not close it,
and a control that depends on nobody misconfiguring it later is not a control
(see NOTES-rules-are-not-controls).

**A DEPLOYER runs on the VM** (systemd, beside the launcher). It watches for a
new release, verifies it, and performs the swap. The trust arrow points
outward only:

```
CI (hosted runner) --push--> ghcr.io (public)  <--pull-- deployer (VM)
                                                  ^
                                        no inbound port, no runner
                                        token, no write-scoped
                                        GitHub credential on the VM
```

What the VM needs from GitHub: nothing. It reads a public registry and a
public release marker. What GitHub needs from the VM: nothing.

## 3. RULED: the release cut is the human act

"CI DOES NOT DEPLOY" (10877 §6) is amended, not repealed. Its load-bearing
half was never that a human types the command — it was that **merged must not
silently mean running**. That half is kept by two things:

1. **A human cuts the release.** Not every push to main is a release. The
   deployer follows a MARKER — a cut release naming an attested image — never
   the branch. One deliberate click, and the machine takes only the mechanics.
2. **The deploy announces itself to the room**, before and after, with the
   version, the image sha, and the outcome. A deploy nobody can see is the
   failure 10877 §6 was written against, whoever performed it.

The pipeline still ends at the push. What changed is who performs the
mechanics, not who decides.

## 4. RULED: trust is the update channel, with the broker as one more consumer

No second trust design. The conditions ruled in msg 10913 §6 apply unchanged:

- CI-minted, attestation-signed releases; the deployer verifies **before**
  installing anything.
- **The verifier is never updated by the thing it verifies.** The trust anchor
  is pinned in the deployer's own installed package and changes only by a
  deliberate reinstall.
- Fetch **by sha**. A tag is a name; a sha is the artifact.
- A failed verification is a REFUSAL, never a warning, and it is loud on the
  bus. There is no unverified fallback path.
- **Canary first.** The bus VM is never the first consumer of a release. A
  cheap consumer proves the artifact before the thing everyone depends on eats
  it.
- Rollback by sha is part of the design, not a later nicety.

## 5. RULED: what "zero downtime" means here, stated before it is promised

**This design buys zero-downtime DEPLOYS. It does not buy high availability.**

One VM. If it reboots, the bus is down, and nothing here changes that. This is
written at the top because "zero downtime" reliably decays into "always up" in
everyone's memory about a month after a design lands — and then a single-host
outage reads as a defect in the deploy system instead of the topology we chose
on purpose.

The achievable goal, stated as two properties:

- **ZERO LOSS.** Nothing that matters lives only in a socket. Mail is
  db-backed, unread persists, `waked` retries forever, `join` replays. A
  broken connection is not a lost message, and that property — not the absence
  of restarts — is what makes a rolling swap acceptable at all.
- **SUB-SECOND CUTOVER.** No client meets a closed port.

## 6. The swap

Caddy is the seam, because it already terminates TLS and reloads its config
without dropping established connections.

1. **Start GREEN** alongside BLUE on an alternate port, same data root.
2. **Probe GREEN with an AUTHENTICATED call**, and check the version it
   reports is the one being deployed.
3. **Flip the Caddy upstream** (graceful reload).
4. **SIGTERM BLUE.** Its graceful shutdown closes wake sockets with the
   courtesy frame; every `waked` reconnects through the proxy and lands on
   green.
5. **Keep BLUE for a defined window**, then remove it.

**The health gate is an authenticated call. Never a TCP connect, never
`/health` alone.** A port that accepts a connection proves a port accepts a
connection. The fleet has now spent one full night on failures where every
transport signal read green while the thing behind it was wrong (the
architect split brain, msg 10896; the 500-not-401 on `/presence`, msg 10880);
a deploy gate that asks anything less is that class of defect with a promotion.

**The overlap window is short by construction.** Two brokers on one sqlite
file is tolerable for seconds under WAL and is NOT a steady state: waiter
registration is per-process, so two long-lived brokers are a wake-plane split
brain — the exact failure this fleet fixed tonight, rebuilt on purpose.

## 7. RULED: a schema-migrating release is stop-migrate-start

If a release migrates the schema, the swap for that hop is **stop, migrate,
start** — seconds of deliberate downtime, announced before it happens.

Migrating under a live writer is a gamble, and the odds do not improve by
classifying migrations as additive. One rule beats a judgement call made per
release by whoever is on shift: **schema moves, the swap stops.**

**Back the database up before every migration, without exception.** A
half-applied migration on the live database is a loss measured in history, and
the downtime this whole design exists to avoid is measured in seconds. That
trade is not close.

## 8. Rollback and the pause control

- GREEN fails its health gate: never flip, remove GREEN, say so on the bus.
  BLUE never knew.
- Trouble after the flip: flip the upstream BACK — possible only because BLUE
  is still there, which is why step 5 keeps it.
- **A pause control on the channel**, so a bad release is stopped without
  anyone editing a workflow under pressure. Recovery controls are never gated
  on the state they recover from (ruling 8866): pause is always available, not
  only while a deploy is in flight.

## 9. The launcher is the second unit, and it is easier

systemd, `Restart=always`, no long-lived client sockets. One rule: **it must
not restart mid-provision.** Refuse new provisions, let in-flight ones finish,
then restart. MERGED DOES NOT MEAN RUNNING remains true across two units that
deploy separately; this design does not merge them.

## 10. Gates

Each proven RED before it is trusted, on a commit that carries the defect:

1. **The health gate refuses a broker that opens a port and cannot resolve a
   principal.** Red against a broker started with a broken credential path.
2. **Nothing is lost across a swap.** Send to an agent, kill BLUE mid-flight,
   assert the unread arrives after reconnect.
3. **The deployer refuses an unattested or sha-mismatched artifact**, and
   refuses loudly rather than falling back.
4. **A schema-migrating release takes the stop-migrate-start path**, and a
   backup exists before the migration runs.
5. **Rollback restores service** from a green-but-bad release.

## 11. Non-goals

- High availability, multi-host, or failover. One VM (§5).
- Deploying the agent plane. Agent images follow their own tag discipline and
  reach containers by re-provision, not by this deployer.
- Auto-deciding what a release is. A human cuts it (§3).

## 12. Open

- **Where the marker lives** — a GitHub Release, a git tag, or a channel file
  in the registry. The marker's shape decides the deployer's shape, so this is
  settled before code is written, not during.
- **The canary's identity** on a single-VM topology: which consumer eats a
  release first when there is only one host that matters.
