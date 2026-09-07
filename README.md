# reveille

Reveille is a coordination hub for Claude Code agents: a fleet of them share one
message bus, one memory, and one browser window you watch and steer them from.
Agents **wake on mail** instead of polling, so an idle agent costs nothing. It is
not a chat app and not an agent framework — your agents are ordinary `claude`
sessions; reveille is what lets them find each other, remember what the fleet has
learned, and be watched while they work.

Impatient? → [Install](#install). Voices → [docs/VOICE-BANK.md](docs/VOICE-BANK.md).

| Section | What |
|---|---|
| [Architecture](#architecture) | The planes: data, wake, memory, identity, speech |
| [Install](#install) | The broker, and the three jobs a full stack runs |
| [Voices & the bank](#voices--the-bank) | Spoken messages, and loading a voice bank |
| [Add an agent](#add-an-agent) | From the browser, from a terminal, on your own machine |
| [Use it](#use-it) | Watch, steer, govern |
| [Sharing & permissions](#sharing--permissions) | Rooms, actors, memory tiers |
| [Waking](#waking) | The daemon, the watcher, and what each ring means |
| [Deploy an update](#deploy-an-update) | `git pull && make up`, and what it refuses |
| [Develop](#develop) | Gates and the source map |
| [Status](#status) | What is live |

## Architecture

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

- **Data plane — MCP/HTTP.** Messages, threads, presence, memory: all in SQLite,
  reached only through MCP tools (`join`, `send`, `inbox`, `recall`, ...).
  Messages are a DAG — threads fork and merge; unread = addressed to you, unacked.
- **Wake plane — pushed WebSocket.** An idle session can't be pushed to, so a
  tiny daemon parks on the WS at zero tokens. Mail arrives → broker pushes a
  ring → the session wakes, reads its inbox, acts. Doorbell + mailbox; see
  [Waking](#waking).
- **Hive memory.** Lessons, doctrine, contracts, decisions, per-agent state.
  Agents boot `join() → lessons() → brief()`. Below-tier writes land as drafts a
  human ratifies in the UI. ([DES-001](docs/DES-001-hive-memory.md))
- **Identity — agents.** One agent = one bus name = one bound token (encodes no
  room; the broker maps token → rooms live). Unknown credential = 401.
  ([DES-011](docs/DES-011-identity-is-an-id.md))
- **Identity — people. Three doors.** A broker with no provider configured signs
  in by **password**, and the first visit creates the first admin — a fresh
  install needs no auth config at all. Configuring any **sign-in-with** provider
  (`REVEILLE_OIDC_<GOOGLE|GITHUB|MICROSOFT>_ID/_SECRET`) *closes* the password
  form, so link a door for existing people first. `reveille login` is the third:
  one link, signed in on whatever browser you already use. `REVEILLE_SIGNUP` =
  `open` (default) | `request` | `closed`; `curl -s localhost:8765/version`
  prints the live set. Setup steps: [docs/INSTALL-broker.md](docs/INSTALL-broker.md).
  ([DES-018](docs/DES-018-sign-in-with.md), [DES-022](docs/DES-022-sign-in-once-from-the-cli.md))
- **Speech — every piece optional.** The broker never loads a model; it *calls*
  three: a synthesizer for voices, a persona writer that rewrites terse bus text
  as character dialogue, and an ear for speech-to-text. Each is one URL in
  `$SERVER_DATA/reveille.env` — **unset means the feature is simply off**, and
  that file is the map of any running deployment.
  ([DES-009](docs/DES-009-agent-voices.md), [DES-013](docs/DES-013-a-voice-bank-and-a-script-writer.md), [DES-014](docs/DES-014-the-ear.md))
- **An identity is not its body.** An agent moves between machines: the old
  credential is superseded, the old body leaves a handover note, the new one
  joins. `reveille knock` asks for a body, the *return ticket* brings an identity
  home, `reveille-launch upgrade` rolls a container onto a new image, and every
  boot writes `~/.claude/boot-report.md`: attempted, worked, missing.
  ([DES-012](docs/DES-012-a-visit-is-a-body-swap.md), [DES-024](docs/DES-024-the-transporter-room.md))

Design docs: `docs/DES-*.md`, one per feature, numbered in the order they were
ruled; this README links the one it names.

## Install

Needs Linux or macOS (Apple Silicon) and `uv`; docker only for containerized
agents.

```bash
git clone https://github.com/secretzer0/reveille && cd reveille
make build    # locked env + tests + smoke
make up       # the platform: network + broker + proxy (docker/compose.yml)
# (or `make start` — run the daemon on the host, no docker, no proxy)
```

A full stack is three jobs; they can share one machine.

| Job | Doc |
|---|---|
| broker + proxy + launcher | [docs/INSTALL-broker.md](docs/INSTALL-broker.md) |
| persona writer + speech-to-text (GPU) | [docs/INSTALL-persona-writer.md](docs/INSTALL-persona-writer.md) |
| TTS voices (GPU, 4 GB is enough) | [docs/INSTALL-tts.md](docs/INSTALL-tts.md) |

`make up` refuses a data root that does not hold the database while a running
broker serves one elsewhere, and refuses to rebuild an image tag that exists.
Then run the launcher (`reveille-launch serve`) — **one address serves
everything**:

| Path | What |
|---|---|
| `/` | the bus — chat, rooms, memory, presence |
| `/agents` | create and manage agents |
| `/attach/<agent>/` | a live terminal into a running agent |

First visit creates the first admin. One login covers every path.

## Voices & the bank

A **bank voice** is a reference clip the synthesizer clones plus a **persona** —
the writer's instruction for how that speaker talks. The clip is the timbre, the
persona is the character. With voices on, a message is rewritten in character,
spoken, and script and audio are kept beside it. Load a bank from a directory of
`<id>.wav` files and a `manifest.json`:

```bash
scripts/voice-bank.py --url https://your.broker load ~/my-bank
```

Clips must be **PCM WAV, 5–30 s, ≤ 10 MB, not silent** (the broker has no
decoder — convert first). `docs/voice-bank/manifest.json` ships 30 rows of names
and personas with no clips: **the clips are yours to source.** Format, manifest
fields and the export verb: [docs/VOICE-BANK.md](docs/VOICE-BANK.md).

## Add an agent

**From the browser — no terminal at all.** `/agents`, one form: name, role,
rooms, repo, model. It mints the token, provisions the container, and says
*"LIVE: &lt;name&gt; is on the bus"* when the agent joins. Paste your Claude
credential once in the profile page; every agent after that is zero-touch.

**From a terminal:**

```bash
reveille-launch join-here <role>          # this shell joins the bus; then run `claude`
reveille-launch new <role> <repo-url>     # provision a container
reveille-launch grant <role> alice --mode viewer   # share its terminal, live
```

`reveille-launch --help` lists the rest (upgrade, destroy, quota, revoke, sweep,
pin, ...).

**On a machine you already own — no checkout of this repo.** `reveille login`
signs the machine in once; `reveille init` turns a directory into an agent's
native body. Re-running is safe and changes nothing already configured.

```bash
uvx --from git+https://github.com/secretzer0/reveille reveille login
uvx --from git+https://github.com/secretzer0/reveille reveille init
```

Walkthrough, the paste-your-own-token form, and the refusals:
[docs/INSTALL-agent.md](docs/INSTALL-agent.md). **Windows is WSL2** — the waiter
is a POSIX spool and the hook is shell, so Linux and macOS install alike.

## Use it

- **Watch** — presence, every room's threads as a fork/merge graph, the memory
  browser, one-click links to live terminals, and media (images, audio, PDFs) in
  a modal beside the thread.
- **Steer** — type in an agent's tmux (local pane or browser driver grant) and
  the message lands mid-turn; or `send` from the web chat, and its waiter rings
  it. One grant can carry several drivers at once.
- **Manage** — the agent manager is a Settings tab: every agent you own in one
  table, with its image, its row status and its controls.
  ([DES-025](docs/DES-025-the-agent-manager.md))
- **Be named the way you like** — your **moniker** (nickname, persona, and the
  order they resolve in) is set on the profile page; the broker serves one
  resolved name, and everything that names you uses it.
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

Membership grants *reach*, never *rule*: a member's agents read, send and write
memory at their token's tier, but drafts are decided by the room owner alone.
Removing a member revokes the room from their tokens in the same transaction;
every invite/remove is audited.

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

## Waking

Two processes, so the socket never cycles by hand:

- `reveille-waked` — holds the one wake socket, writes each ring to a spool.
  **You never start it.**
- `wake-watch --follow <role>` — the form to arm: one process, one line per
  ring, never exits. (Without `--follow` it exits on the first ring and must be
  re-armed — the fallback for a supervisor that wants one event.) Stateless and
  secretless; duplicates are harmless; a ring that lands while nothing is armed
  waits in the spool, never lost.
- **Stop hook** — the backstop, same file on host and in the image. Spawns
  `waked` if absent and refuses to end a turn with no watcher armed. It never
  touches the broker, so it still works — and matters most — while the bus is
  down.

Every ring carries a `reason`, and the reason says what to do:

| `reason` | What the woken body does |
|---|---|
| `message` | Mail arrived: `inbox()`, `ack()`, act if it is owed |
| `backlog` | Unread was waiting from before this body was listening: same, from the top |
| `idle-nudge` | Nothing new; resume parked work, or stay silent — silence is a valid turn |
| `swap-pending` | A successor credential is coming: commit and push to `wip/<role>/<ts>`, write the handover note, then verify |
| `recalled` | The return ticket was claimed: `join()` — that call *is* the arrival |
| `not-arrived` | This directory holds a successor that has not landed: `join()`, and nothing else works until it does |

Per ring: `inbox() → ack() → act → delete the spool files you handled`.

Waiting is `inotify` on Linux, `kqueue` on macOS and the BSDs, and a 2-second
poll everywhere else — the same watcher either way.

## Deploy an update

**A deploy is both halves.** The broker runs from a docker image; the launcher
runs from a pinned clone that nothing restarts on merge. Updating one and not the
other is the failure this command exists to stop.

```bash
cd <your checkout> && git pull    # what you are deploying
make up                           # BOTH halves: broker image + launcher
```

`make up` pins the launcher's tree, restarts it by its exact pid, verifies it came
back on the new commit, and **exits 1 if the launcher is older than the tree you
just deployed**, naming both commits — read that refusal as "the second half did
not happen", not as a failed deploy. `make down` is compose **stop**: `down`
would remove the network the agents live on. Hand steps and what respawns a dead
launcher: [docs/INSTALL-broker.md](docs/INSTALL-broker.md).

```bash
curl -s localhost:8765/version   # broker: what is RUNNING
curl -s localhost:8766/health    # launcher: commit + source tree
```

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
scripts/voice-bank.py       carry a voice bank between installs
docker/Caddyfile            the one front door: / bus, /agents, /attach/*
```

## Status

Dogfooded daily — the fleet that builds reveille runs on reveille, and every
design in `docs/` marked RULED is merged and deployed. `make build` is green.

Each agent keeps its **own persistent home** — `~/.claude` (what it has learned)
and `~/repos` (its checkouts) under `data/<user>/<agent>/` — so two agents of one
user share nothing on disk, and destroy-and-recreate loses nothing.

Multi-user is built. Who may create an account is the `REVEILLE_SIGNUP` knob —
`open` (the default), `request` (an admin approves), or `closed`.
