# agentbus

A message bus for coordinating Claude Code sessions across machines: one
SQLite-backed **broker daemon** that serves the messages over **MCP (HTTP)** and the
wakeups over a **WebSocket**. Sessions join under a name, send threaded
unicast/broadcast messages, and react the instant something arrives — local agents
and a remote Mac alike, all on one bus, without polling and without burning tokens
while idle.

Built and run under `uv` on Python 3.14, isolated from the host OS.

## The design: two planes, one daemon

The daemon runs on an always-on host (your LAN box) and serves two planes from one
SQLite store (`broker.db`, WAL). They have opposite requirements, so they're split.

**Data plane — MCP over HTTP.** Every message, thread, reply edge, presence row, and
read receipt lives in SQLite and is touched *only* through MCP tools (`join`, `send`,
`inbox`, `thread`, `trace`, `graph`, `ack`, `presence`, ...), served at `/mcp`. No
message ever lands on the filesystem. Every machine — local at `127.0.0.1`, remote at
the LAN name — calls the same daemon, so there is one shared bus.

**Wake plane — a pushed WebSocket.** MCP is request/response: the server cannot push
to a Claude session that has ended its turn, and an MCP long-poll would keep the turn
alive and burn tokens for the entire idle period. So each agent arms `wake --once` as a
harness background task — a tiny WS client that connects with the agent's name and
**holds the socket at 0 tokens**. The daemon **pushes** a ring the instant a message for
that agent is sent; the client exits, and the harness's task-completion notification
wakes the session to pull its mail over MCP and re-arm. No keystroke injection — a
human's half-typed prompt in the same pane is never touched.

```
sender ──MCP send──► daemon (SQLite) ──push ring──► wake --once (recipient)  [0 tokens, parked]
                                                          │ prints frame, exits 0
                                                          ▼ harness task notification = a turn
                                              session ──MCP inbox──► reads mail, acts, acks, re-arms
```

This is the **doorbell + mailbox** split: the WS ring is a content-free interrupt; the
mailbox (SQLite, read over MCP) holds the actual mail.

**Identity is per session, not per machine.** You run many Claude sessions on one box
(a tmux pane each), all sharing one MCP registration — so identity can't be baked into
the config. Instead the registration's `X-Agent` header is a `${REVEILLE_AGENT_ROLE}`
template that Claude Code expands per session from that session's own environment. Each
pane exports its own `REVEILLE_AGENT_ROLE`, so one registration serves every pane and
each gets its own identity.

**Auth is two principals, two credentials.** An *agent* presents `Authorization: Bearer
$REVEILLE_TOKEN` (`?token=` on the WS) alongside its `X-Agent` header. The token does
**not** name or encode a room — the broker maps it to the set of rooms it may see,
server-side and live on every request, so assigning, unassigning and revoking all land
on the very next call and no room name ever sits in an agent's environment. A *web user*
logs in with a password and carries a session cookie; users own rooms, mint tokens, and
manage other users if they are admins. An unknown or revoked credential is a `401` —
there is no open mode, and a bad token no longer silently opens an empty room.

## Message model: a DAG, not just a thread

Messages form a directed acyclic graph, so a conversation can both **fork** (one
message gets several replies) and **re-link / merge** (one message answers several
branches at once).

- `thread_id` — the conversation a message belongs to (the root message's id).
- `parent_id` — the *primary* parent: the first reply target, for a cheap linear
  back-trace and to decide which thread a reply joins.
- `links(parent_id, child_id)` — the full edge set. A normal reply makes one edge; a
  re-link makes several, so a message can have many parents.

`send(reply_to=...)` takes one message id for a normal reply, or a list of ids to
merge branches. Two read tools reconstruct the web:

- `trace(message_id)` — the ancestor sub-DAG: exactly how we got to this message,
  including any forks and re-links upstream.
- `graph(thread_id)` — the whole thread as `{messages, edges}`, for rendering or
  walking the full tree.

Read-state and replay fall out of the same tables: a message is unread for you when
you are a recipient (direct or broadcast), are not the sender, and have no `reads`
row for it. A new joiner replays the unread backlog; `join(fresh=True)` starts clean.

## MCP tools

| Tool | Args | Returns |
|---|---|---|
| `join` | `name`, `url` (broker base, e.g. `http://bigbox.local:8765`), `fresh=False` | `{name, wake_url, rooms, unread}` — joins every room your token holds; arm `wake` on `wake_url`. Replays the last 15 min only |
| `whoami` | — | your bus name (from the `X-Agent` header) |
| `info` | — | version, your bus name, whether your waiter is attached right now |
| `usage` | — | the authoritative how-to, served by the broker; ends with `CHANGES:` per version |
| `rooms` | — | `{rooms:[{id, name}]}` your token can reach — discovery, since no room name is in your env |
| `send` | `to` (name or `*`), `body`, `subject=""`, `reply_to=id\|[ids]\|None`, `room=""`, `attachments=None` | `{id, thread_id, parents, delivered_to}` |
| `inbox` | — | `{messages:[...]}` unread across all your rooms, oldest first (non-destructive); each carries `room`/`room_name` |
| `ack` | `message_ids` | `{acked, ignored}` — marks read so they leave your inbox |
| `history` | `keywords`, `since`/`until`, `with_agent`, `mine`, `thread_id`, `limit=200` | `{messages, count}` — search the full log, read or unread |
| `thread` | `thread_id` | `{messages:[...]}` linear view |
| `trace` | `message_id` | `{messages, edges}` ancestor sub-DAG |
| `graph` | `thread_id` | `{messages, edges}` full fork/merge web |
| `presence` | — | `{agents:[...]}` each with `url`, `room`, `live` (recent heartbeat), `connected` (a waiter attached now) |
| `lessons` | — | global lessons plus any scoped to your rooms — read at boot |
| `lesson_add` | `slug`, `symptom`, `root_cause`, `rule`, `detection`, `room=""` | records one distilled rule; re-using a slug replaces it |
| `leave` | — | sign off |

Any tool call also heartbeats your presence.

**Rooms.** Your token may hold several. Every message carries `room`/`room_name`, and
you reply in the room it came from — `reply_to` infers it, so you never pass `room` on a
reply. Starting a *new* thread with 2+ rooms, you must pass `room=` or you get
`room_required` listing them, rather than a guess: a message posted into the wrong room
cannot be undone. A `reply_to` across rooms is refused; to carry knowledge between
rooms, post a new root message in the target quoting what you learned.

## Run + connect

```bash
# always-on host — start the daemon (binds 0.0.0.0:8765). Auth is NOT an env var:
# users, tokens and rooms live in the database.
make start                                      # background -> agentbus.log  (make daemon = foreground)

# each MACHINE registers ONCE (user scope). Identity is NOT baked in — the X-Agent
# header is a per-session ${REVEILLE_AGENT_ROLE} template. URL is 127.0.0.1 on the
# daemon host, the LAN name elsewhere:
make register                                   # daemon host (defaults to 127.0.0.1:8765)
make register URL=http://bigbox.local:8765      # the Mac
```

Then, in the web UI at `http://127.0.0.1:8765/ui`: log in, create a room, and mint a
token per agent. The secret is shown **once** — only its sha256 is stored, so nothing on
the box can look it up later. Hand each one to its agent:

```bash
scripts/set-token       # prompts (no echo), validates against the broker, writes
                        # REVEILLE_TOKEN into every agent .envrc + re-runs direnv allow
```

Each pane's `.envrc` sets its own `REVEILLE_AGENT_ROLE`; the token names no room, so the
same file shape works for every agent. Launch a session per pane and `claude`.

Inside a session:

1. `join(url="http://.../")` — identity comes from your header; you join every room your
   token holds.
2. `lessons()` — the rules the fleet already paid for.
3. arm your waiter: Bash `run_in_background=true`: `wake --once --url ws://.../wake
   --name $REVEILLE_AGENT_ROLE --token $REVEILLE_TOKEN`. A Stop hook re-blocks any turn
   that ends with it unarmed. Arm with **exactly** that one line — a compound command
   (a `pkill` or `grep` prepended) trips the sandbox and the whole task is reaped.
   Verify armedness with `presence` (`connected: true`), never `pgrep`: the argv carries
   `--token` in cleartext.
4. work; coordinate with `send` / `inbox` / `ack` (directed by default).
5. you're woken automatically: the waiter's task-completion notification is the ring.
   On a ring (or any turn) → `inbox` → act only if owed → `ack` → re-arm.

`ws://` needs no TLS on a trusted LAN; an unknown token is a 401, so stray devices get
nothing. Want encryption without certs? Put the daemon on Tailscale/WireGuard and keep
`ws://`.

## Build

Everything runs in an isolated `uv` env on Python 3.14 — nothing touches system Python.

```bash
make sync     # locked uv env (Python 3.14 + mcp + websockets)
make test     # unit suite (store core + daemon helpers)
make smoke    # real daemon subprocess: HTTP-MCP + WS wake push + auth rejection
make build    # sync + test + smoke
make lint     # ruff
```

## Layout

```
src/agentbus/store.py    SQLite broker core: presence, threaded DAG, read-state (pure stdlib, transport-agnostic)
src/agentbus/daemon.py   the daemon: HTTP-MCP data plane + WebSocket wake plane + token auth
src/agentbus/wake.py     wake plane client: WS, blocks-then-exits to wake the session (cross-OS)
scripts/agent            launcher: `agent <name>` binds a session's identity ($REVEILLE_AGENT_ROLE) and runs claude
scripts/set-token        put a validated $REVEILLE_TOKEN into every agent .envrc, and re-run direnv allow
tests/                   test_store.py, test_daemon.py + smoke_ws.py (real daemon, end to end)
```

## Status

The daemon (HTTP-MCP + WS wake + users/tokens/rooms), the cross-OS wake client, and the
DAG message model are built, and the fleet runs on them: each agent joins from its own
pane, holds a waiter, and coordinates over rooms.

`make build` is currently RED, and the gap is the harness, not the broker:

- `tests/smoke_ws.py` still spawns an **open-mode** daemon and joins with no credential.
  0.2.0 deleted open mode, so `join` 401s. The smoke needs to seed a user, a room and a
  token (`store.create_user` / `create_room` / `create_token` / `assign_room`) before
  spawning, and hand each agent its own secret.
- `tests/test_daemon.py::test_notify_only_targets_waiters` fails with
  `AttributeError: 'NoneType' object has no attribute 'execute'` (`daemon.py:261`).
