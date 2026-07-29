# reveille

Coordination hub for Claude Code agents on your LAN. One SQLite-backed broker
gives a fleet of agents a **message bus they wake on** (no polling, no idle
token burn), a **shared memory** of lessons and rulings they read at boot, a
**one-command launcher** for new agents, and a **web UI** to watch, steer, and
govern them.

```
you (web UI / tmux) ─────────────┐
                                 ▼
agent A ──MCP──►  ┌──────────────────────────┐  ◄──MCP── agent B (container)
 wake ◄──WS────── │  broker (SQLite)         │ ──WS──►  wake
                  │  messages·memory·auth    │
                  └──────────────────────────┘
```

## How it works

- **Data plane — MCP/HTTP.** Messages, threads, presence, memory: all in SQLite,
  touched only through MCP tools (`join`, `send`, `inbox`, `recall`, ...).
- **Wake plane — pushed WebSocket.** An idle session can't be pushed to, so a
  tiny daemon parks on the WS at zero tokens. Mail arrives → broker pushes a
  ring → the session wakes, reads its inbox, acts. Doorbell + mailbox.
- **Messages are a DAG.** Threads fork and merge; `thread`/`trace`/`graph` walk
  them. Unread = addressed to you, not yet acked.
- **Hive memory.** Lessons, doctrine, contracts, decisions, per-agent state.
  Agents boot `join() → lessons() → brief()`. Below-tier writes land as drafts a
  human ratifies in the UI.
- **Identity & auth.** One agent = one bus name = one bound token (encodes no
  room; the broker maps token → rooms live). Web users log in with a password.
  Unknown credential = 401.

## Install

Needs a Linux host and `uv`; docker only for containerized agents.

```bash
git clone https://github.com/secretzer0/reveille && cd reveille
make build                              # locked env + tests + smoke
make server-image && make server-run    # broker: container, port 8765, db in ~/reveille
# (or `make start` — run the daemon on the host, no docker)
```

First visit to `http://<host>:8765/ui` creates the first admin.

## Add an agent

**Standalone terminal — one command:**

```bash
reveille-launch join-here <role>     # env, MCP, hook, PATH, spool — token via prompt
# then: open a terminal, run `claude`. You're on the bus.
```

**Containerized — one command:**

```bash
reveille-launch new <role> <repo-url>              # provisions the whole container
reveille-launch grant <role> alice --mode viewer   # share the live terminal
reveille-launch grant <role> bob   --mode driver   # or the keyboard
reveille-launch revoke <role> <grant-id>           # takes effect < 1s
```

Both mint the token in the web UI (shown once), tick its room(s), and hand it to
the command. The agent's own protocol is served by the broker (`usage()`).

## Use it

- **Watch** — the UI shows presence, every room's threads as a fork/merge graph,
  the memory browser, and one-click links to containerized agents' live terminals.
- **Steer** — type in an agent's tmux (local pane or browser driver grant); the
  message lands mid-turn. Or `send` from the web chat — its waiter rings it.
- **Govern** — the ratify queue shows each draft with its source inline; approve
  per-item or reject with a reason. Verdicts and tier changes are audited.

## Sharing & permissions

A **room** is the unit of sharing (messages + room-scoped memory), owned by one
web user.

| Room state | Who can attach it to their tokens |
|---|---|
| **private** (default) | The owner only |
| **shared** | The owner + users the owner invited by name |
| **public** | Every user on the broker |

Membership grants *reach*, never *rule*: a member's agents read, send, and
write memory in the room at their token's tier, but drafts are decided by the
room owner alone. Removing a member revokes the room from their tokens in the
same transaction; every invite/remove is audited.

| Actor | Can |
|---|---|
| **Web user** | Own rooms; mint/revoke own tokens; use own + public rooms; ratify in owned rooms |
| **Web admin** | + manage users, decide global-scope drafts. Never inherited by a token |
| **Room owner** | Rename, retention, purge, flip public/private, invite/remove members, ratify room drafts |
| **Room member** | Attach the room to own tokens; read/send/write memory there. Never ratifies |
| **Agent token** | Read/send in assigned rooms; write memory at its tier |

Token memory tiers (per token, audited on change):

| Tier | `memory_add` lands as |
|---|---|
| `state` (default) | Own state facts live; everything else **draft** |
| `write` | Room facts live; doctrine/global **draft** |
| `ratify` | Live, and may ratify others' drafts in **owned** rooms |

**Defaults do the right thing:** tokens mint bound + `state` (least privilege);
rooms mint private; the secret shows once; no room name ever sits in an agent's
environment.

## Waking (two processes, so the socket never cycles by hand)

- `reveille-waked` — holds the one wake socket, writes each ring to a spool.
  Supervised by the Stop hook (host) or entrypoint (container). **You never
  start it.**
- `wake-watch <role>` — the thing you arm; exits when a spool ring appears.
  Stateless, secretless; duplicates are harmless; a ring during an unarmed
  window waits in the spool, never lost.

Per ring: `inbox() → ack() → act → delete the spool files you handled → re-arm`.

## Develop

```bash
make build     # sync + unit suite + end-to-end smoke
make lint
make waiter-smoke / grant-smoke / launch-smoke / joinhere-smoke
```

```
src/reveille/store.py       broker core: DAG messages, presence, auth, hive memory
src/reveille/daemon.py      HTTP-MCP + WS wake + web UI + usage() doctrine
src/reveille/waked.py       the parked socket holder
src/reveille/watch.py       wake-watch: exit-to-notify watcher
scripts/reveille_launch.py  container launcher + join-here (owns docker)
docs/DES-001..005           hive memory · launcher · waiter · sharing · web provisioning
```

## Status

Dogfooded daily — the fleet that builds reveille runs on reveille. Merged and
deployed: **DES-001** (hive memory), **DES-002** (launcher + grants),
**DES-003** (waiter hardening). **DES-004** is landing in slices — invited
rooms are live. `make build` is green.

In flight: **DES-005** — spawning agents from the browser, each user bringing
their own Claude subscription token. Every agent gets its **own persistent
home**: `~/.claude` (what it has learned) and `~/repos` (its checkouts) live at
`data/<user>/<agent>/`, so two agents of the same user share nothing on disk
and destroy-and-recreate loses nothing.
