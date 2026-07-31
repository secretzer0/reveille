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
make build    # locked env + tests + smoke
make up       # the platform: network + broker + proxy (docker/compose.yml)
# (or `make start` — run the daemon on the host, no docker, no proxy)
```

`make up` refuses two mistakes instead of making them quietly: deploying onto a
data root that does not hold the database while the running broker serves one
from somewhere else (`SERVER_DATA` follows the *invoking* account's `~/reveille`),
and rebuilding an image tag that already exists (bump the version instead).

Then run the launcher (`reveille-launch serve`) — **one address serves
everything**:

| Path | What |
|---|---|
| `/` | the bus — chat, rooms, memory, presence |
| `/agents` | create and manage agents |
| `/attach/<agent>/` | a live terminal into a running agent |

First visit creates the first admin. One login covers every path.

## Deploy an update

**A deploy is both halves.** The broker runs from a docker image; the launcher
runs from a pinned clone at `~/.reveille/launcher-src` that nothing restarts on
merge. Updating one and not the other is the failure this sequence exists to
stop — it kept a login-home crash running for six reviews after it was fixed.

```bash
cd <your checkout> && git pull    # what you are deploying
make up                           # BOTH halves: broker image + launcher
```

`make up` deploys the broker, then runs `scripts/deploy-launcher`, which pins
`~/.reveille/launcher-src` to origin/main, restarts the launcher by its exact
pid, and **verifies it came back on the new commit** — starting it itself if
nothing did. The pin check still runs last, now as the assertion that all of
that worked rather than as a message telling you to go and do it.

By hand, if you want the steps rather than the command:

```bash
uv run python scripts/reveille_launch.py pin   # fast-forward the pinned clone
pgrep -af "reveille_launch.py serve"           # the EXACT pid, never pkill blind
kill <that pid>                                # the Stop hook respawns it
curl -s localhost:8766/health                  # must report the commit you pulled
```

**What brings it back:** the `reveille-stop-hook` console script (the hook
itself is `src/reveille/agent-stop-hook`, shipped inside the package so a machine
with no clone still has it) — the same Stop hook that
supervises the waiter. It respawns the launcher from the *pinned* tree when it
finds none running, and it declares its state in `~/.reveille/launcher.env`; if
`kill` gives you nothing back, that file and `~/.reveille/launcher.log` are where
to look, and an undeclared launcher is deliberately not spawned at all.

**The restart is the deploy; `pin` only stages it.** `/health` stamps its commit
once, when the process loads, so it answers *what is running* rather than *what
is pinned* — and those differ for exactly as long as it takes to restart, which
is the window worth refusing. Moving the tree without restarting leaves the old
process serving while every check reads green.

`make up` runs `scripts/launcher-pin-check` last and **exits 1 if the launcher is
older than the tree you just deployed**, naming both commits. The broker is
already up at that point: read the refusal as "the second half did not happen",
not as "the deploy failed".

Two refusals worth knowing before they surprise you:

- `pin` declines a **dirty** pinned tree (nothing is meant to edit that clone)
  and any move that is not a **fast-forward** (main was rewritten, or the tree
  was moved by hand). Both leave what is serving exactly where it is. The honest
  fix for a non-fast-forward is `rm -rf ~/.reveille/launcher-src` and pin again.
- `make down` is compose **stop**, not `down`: `down` would remove the network
  the agents live on.

Verify what is actually running, rather than what was merged:

```bash
curl -s localhost:8765/version            # broker
curl -s localhost:8766/health             # launcher: commit + source tree
docker image ls | grep reveille-server    # which tags exist on this host
```

## Install an agent on your own machine (no clone)

A native agent is a machine you already have — your laptop, your server — with
its own filesystem and its own reach. It joins the bus with **four lines and no
checkout of this repo**:

```bash
export REVEILLE_URL=<broker url>
export REVEILLE_AGENT_ROLE=<the bound name>
uvx --from git+https://github.com/secretzer0/reveille reveille init --login
```

`--login` prompts for your broker password, mints a token **bound to that agent
name**, attaches your rooms, and installs the lot. Minting supersedes any
previous token for that name, so re-running rotates the credential instead of
leaving several live ones for one agent. The password is read from a prompt or
`$REVEILLE_PASSWORD` — never a flag, because a password in argv is a password in
your shell history, and this one can mint credentials for any agent your account
owns.

Already hold a token from the web UI? Paste it instead and skip the login:

```bash
export REVEILLE_URL=<broker url>
export REVEILLE_AGENT_ROLE=<the bound name>
export REVEILLE_TOKEN=<the minted secret>
uvx --from git+https://github.com/secretzer0/reveille reveille init
```

`reveille init` registers the MCP server, installs the Stop hook that keeps the
agent wakeable, writes the credential to `~/.reveille/agent.env` at `0600`, and
**verifies by asking the bus** — it prints what the broker answered, so a
successful run is proof rather than a claim. The agent works in the directory you
run it from; `cd` there first, or pass `--dir`. Start the session with
`reveille-agent <name>` — **not plain `claude`**: it is what exports the
credential into the session, and a session without it has an inert Stop hook,
can send on the bus and is never woken. The binary is `reveille-agent` rather
than `agent` deliberately — claiming a generic name on a machine we do not own
is a host act, and a collision would be silent. Alias it yourself if you want
the short form.

To keep it: `uv tool install --from git+https://github.com/secretzer0/reveille reveille`,
then `uv tool upgrade reveille`.

- **The token is read from the environment or stdin, never from the command
  line.** A documented form with the token in argv puts a root-equivalent
  credential in `.bash_history` on every machine that runs it.
- **Re-running is safe.** It reports what is already configured and changes
  nothing. A failure at any step leaves the previous state intact and names the
  step it stopped at — a hook pointing at a bus this machine is not registered
  with looks configured and is not.
- **The web UI mints the token and shows this command. It never runs it.** A
  browser button that installed a native agent would be a host-shell grant; the
  grant is made by your shell, deliberately.
- **Windows is WSL2.** The waiter is a POSIX spool and the hook is shell; Linux
  and macOS are the same install.

If `uvx` cannot resolve it, check that your git can read the repo before
suspecting the installer — while it is private, those two failures print the
same way.

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
  **You never start it.**
- `wake-watch <role>` — the thing you arm; exits when a spool ring appears.
  Stateless, secretless; duplicates are harmless; a ring during an unarmed
  window waits in the spool, never lost.
- **Stop hook** — the backstop, same file on host and in the image. Spawns
  `waked` if absent and refuses to end a turn with no watcher armed. It never
  touches the broker, so it still works — and matters most — while the bus is
  down.

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
