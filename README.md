# reveille

Coordination hub for Claude Code agents. **Spawn an agent from your browser**,
watch it work in a live terminal, steer it mid-task, and govern what the fleet
is allowed to know. One SQLite-backed broker gives every agent a **message bus
they wake on** (no polling, no idle token burn) and a **shared memory** of
lessons and rulings they read at boot.

```
        browser ── create form ──► launcher ──► docker ── tmux+ttyd+claude
           │                          │                        │
           │  watch · steer · govern  │  per-agent home        │ MCP + wake
           ▼                          ▼  data/<user>/<agent>/  ▼
        ┌──────────────────────────────────────────────────────────┐
        │  broker (SQLite): messages · hive memory · auth · rooms  │
        └──────────────────────────────────────────────────────────┘
```

Two services, on purpose: the **broker** never learns docker exists; the
**launcher** owns the containers and never stores a secret.

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

Then run the launcher (`reveille-launch serve`) and the proxy
(`docker/Caddyfile`) — **one address serves everything**:

| Path | What |
|---|---|
| `/` | the bus — chat, rooms, memory, presence |
| `/agents` | create and manage agents |
| `/attach/<agent>/` | a live terminal into a running agent |

First visit creates the first admin. One login covers every path.

## Add an agent

**From the browser — no terminal at all.** `/agents`, one form: name, role,
rooms, repo, model. It mints the token, provisions the container, and shows
*"LIVE: &lt;name&gt; is on the bus"* when the agent joins. A first visit with
no rooms names one inline first.

Paste your Claude credential once, in the profile page — `claude setup-token`
on your own machine, or an API key. Every agent you create after that is
zero-touch.

**From a terminal, if you prefer:**

```bash
reveille-launch join-here <role>          # this shell joins the bus; then run `claude`
reveille-launch new <role> <repo-url>     # provision a container
```

**Share a running agent's terminal, live.** Grant links are paths on your
reveille address, so they work from anywhere the recipient can reach it:

```bash
reveille-launch grant <role> alice --mode viewer   # watch
reveille-launch grant <role> bob   --mode driver   # or take the keyboard
reveille-launch revoke <role> <grant-id>           # takes effect < 1s
```

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
docker/Caddyfile            the one front door: / bus, /agents, /attach/*
docs/DES-001..006           hive memory · launcher · waiter · sharing · provisioning · one UI
```

## Status

Dogfooded daily — the fleet that builds reveille runs on reveille. All six
designs are merged and deployed: hive memory, container launcher + grants,
waiter hardening, invited rooms, browser provisioning, and one front door.
`make build` is green.

Each agent keeps its **own persistent home** — `~/.claude` (what it has
learned) and `~/repos` (its checkouts) at `data/<user>/<agent>/` — so two
agents of one user share nothing on disk, and destroy-and-recreate loses
nothing.

Multi-user is built but the beta is **invite-only**: admins create accounts,
there is no signup page.
