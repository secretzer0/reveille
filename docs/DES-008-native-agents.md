# DES-008: A native agent is an agent whose home is the machine

Status: RULED — operator directive 2026-07-31 (msg 8829), architect ruling
(msg 8835). Companion to DES-002 (containers), DES-005 (web provisioning) and
DES-007 (identity).

## 1. Problem

Every agent on this bus today is a container the launcher created. That is the
product, and it is also a ceiling: a container agent's reach is its bind
mounts, so nothing on the fleet can touch the docker socket, the launcher's
database, a tenant's data root, or the deploy path. Three pieces of work are
stalled on exactly that gap right now — the DES-007 backfill's dry-run against
a copy of the live database, the attachments-table scan ruled a v1 gate item
(msg 8827), and every deploy step.

The operator's answer is a **native agent**: Claude Code running on the host
itself, joined to the bus like any other member. This document says what
installing one means, who it is, and what must never become easy.

## 2. The installer is packaging, not machinery

Everything a native agent needs already exists and already runs — the container
entrypoint and the `make register` target execute the *same two commands*:

- `claude mcp add --transport http --scope user reveille <url>/mcp` with
  `Authorization: Bearer <token>` and `X-Agent: <name>`
  (docker/entrypoint.sh:119-122, Makefile `register`).
- the user-scope Stop hook that refuses to end a turn with the waiter unarmed
  (scripts/install-hook).
- `wake-watch` on PATH.
- the credential in the agent directory's `.claude/settings.local.json` env
  block: THE DIRECTORY IS THE AGENT (operator ruling, 2026-08-13). Plain
  `claude` started in that directory carries the identity -- Claude Code
  injects the env block at session start -- and one machine holds as many
  agents as it has initialized directories.

**The launcher is `reveille-agent`, not `agent`** (architect, after senior-dev
raised it at msg 8970). This document said `agent` while the script lived in a
clone on our own box, where a generic name costs nothing. An installer puts it on
a PATH we do not own, and taking a name that plausible is a host decision — the
same principle that says installing a native agent is a host act, applied to the
namespace rather than the privileges. Anyone who wants the short form makes the
alias themselves; the README says so. (Retired 2026-08-13, operator ruling: no
launcher ships at all -- the per-directory env block replaced it, and the
history above is why the name was never `agent`.)

What is missing is only that today these arrive by cloning the repo and running
make. **The installer's entire job is to deliver those four without a clone.**
Anything beyond that is scope, and scope here is how a one-evening slice becomes
a subsystem.

## 3. RULED: the bound-token mint IS the provisioning event

DES-007 mints the `agents` row at provision. A native agent is never
provisioned — it has no container and the launcher never sees it. Rather than
invent a second identity path for it:

**Minting a bound token for a name inserts that name's `agents` row, owner =
the minting user.** The mint is the provisioning event. The install command the
UI hands back carries the token that mint produced.

Consequences, stated so they are not rediscovered:

- One identity path, container or not. A native agent is first-class in the
  hive from its first message, rather than an unowned name someone claims later
  under DES-007 §6.1.
- The launcher is not involved and does not need to be. Identity lives in the
  broker (DES-007 §2.1), which is precisely why this works.
- DES-007's one-live-name index applies unchanged: a native agent competes for
  its name with any container agent of the same name, and that is correct —
  they would otherwise be two histories under one label, which is the defect
  DES-007 exists to prevent.

## 4. INVARIANT: installing a native agent is a host act

A container agent's reach is its bind mounts. A native agent's reach is the
machine: docker socket, every tenant's data root, `launcher.db`, the deploy
path. **Nothing on the bus grants that — the shell does.** So the bus token is
not the dangerous credential here, and hardening it buys nothing.

What matters is the direction of the act:

> The web UI MINTS the token and SHOWS the command. It must never RUN it.
> Installing a native agent is a deliberate act performed on the host by
> somebody who already has a shell there.

The day a browser button provisions a native agent, the browser has become a
host-shell grant, and every tenancy boundary in DES-005 §6 is decoration. This
invariant is designed never to soften. It is the same shape as DES-006 §2.1's
refusal to let the broker call the launcher: the question is never whether the
operation is convenient, it is which side is trusting which.

## 5. Delivery

The token page shows the commands, pre-filled with the broker URL and the
freshly minted token, legible in one screen.

Not `curl | sh`. The broker is on the operator's LAN and three visible commands
are auditable where a piped script is not — and an installer that cannot be
read is one that nobody will read before granting it a shell's worth of reach.
If it outgrows one screen it becomes a console script in the package
(`uvx reveille-register`), still not a pipe.

## 6. Slices

1. **Mint-inserts-identity.** The bound-token mint writes the `agents` row
   (owner = minting user). Lands with or after DES-007 slice 3; it is the same
   table. Gate: minting a token for a fresh name produces exactly one live row
   owned by the minter, and a second live mint of that name fails at
   `idx_agents_live` rather than in application code.
2. **The install block on the token page.** The four commands, pre-filled.
   Gate: a machine with Claude Code and no clone of this repo reaches
   `join()` + armed waiter using only what that page displayed.
3. **`reveille-register` console script** — ONLY if slice 2's block outgrows one
   screen. Deliberately last, and deliberately conditional.

## 7. What this does not close

- The two browser-dependent items (msg 8801): the 0.2.52 roster grouping and
  U8's three terminal-tab gates. A native agent *could* drive a headless
  browser and close them, but nothing here has ever done that. Treat it as a
  separate question after the installer works, and do not let a plan depend on
  it.
- Anything on the v1 blocking list (msg 8795). This slice makes three of those
  items workable by giving the fleet host access; it removes none of them.

## 8. ANSWERED (operator + architect 11713, EPIC-001 item 7)

- **Q1. Room. NO — devops does not own a room.** It sits in Reveille2.0 as a
  MEMBER, not a landlord. The recommendation for a separate deploy room is
  withdrawn: the readership argument is real but a second room splits the one
  record the fleet reads at boot, and deploy chatter is exactly the context a
  design ruling needs. Filtering is the reader's job, not the topology's.
- **Q2. Name reservation. PER OWNER**, consistent with ruling 10969 and with
  what the index already did. A native agent's name is unique per owner, not
  fleet-wide; two owners may each hold an `architect`, and the room disambiguates
  with the `<owner>-<name>` alias DES-011 §2 mints at join. Fleet-wide
  reservation would make one human's naming choice a constraint on every other
  human's, which is the opposite of where the multi-human hive is going.
