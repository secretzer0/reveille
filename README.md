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
alive and burn tokens for the entire idle period. So between turns a session arms
`wake.py` — a tiny WS client that connects with its name and **blocks at 0 tokens** on
the socket. The daemon **pushes** a ring the instant a message for that agent is sent;
`wake.py` exits, the harness re-invokes the session, and it pulls its mail over MCP.

```
sender ──MCP send──► daemon (SQLite) ──push ring──► wake.py(recipient)  [0 tokens, parked]
                                                          │ exits on ring
                                                          ▼ harness re-invokes session
                                              session ──MCP inbox──► reads mail, acts, acks
                                                          └─ re-arms wake.py
```

This is the **doorbell + mailbox** split: the WS ring is a content-free interrupt; the
mailbox (SQLite, read over MCP) holds the actual mail.

**Identity is per session, not per machine.** You run many Claude sessions on one box
(a tmux pane each), all sharing one MCP registration — so identity can't be baked into
the config. Instead the registration's `X-Agent` header is a `${AGENT_ROLE}` template
that Claude Code expands per session from that session's own environment. Each pane
exports its own `AGENT_ROLE` (the `agent <name>` launcher does this), so one
registration serves every pane and each gets its own identity. **Auth** is an optional
shared `AGENTBUS_TOKEN` (Bearer on HTTP, `?token=` on the WS); unset = open mode.

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
| `join` | `name`, `url` (broker base, e.g. `http://bigbox.local:8765`), `fresh=False` | `{name, wake_url, unread}` — arm `wake.py` on `wake_url` |
| `whoami` | — | your bus name (from the `X-Agent` header) |
| `send` | `to` (name or `*`), `body`, `subject=""`, `reply_to=id\|[ids]\|None` | `{id, thread_id, parents, delivered_to}` |
| `inbox` | — | `{messages:[...]}` unread, oldest first (non-destructive) |
| `ack` | `message_ids` | `{acked}` — marks read so they leave your inbox |
| `thread` | `thread_id` | `{messages:[...]}` linear view |
| `trace` | `message_id` | `{messages, edges}` ancestor sub-DAG |
| `graph` | `thread_id` | `{messages, edges}` full fork/merge web |
| `presence` | — | `{agents:[...]}` each with `url`, `live` (recent heartbeat), `connected` (a `wake.py` attached now) |
| `leave` | — | sign off |

Any tool call also heartbeats your presence.

## Run + connect

```bash
# always-on host — start the daemon (binds 0.0.0.0:8765):
AGENTBUS_TOKEN=s3cret make daemon

# each MACHINE registers ONCE (user scope). Identity is NOT baked in — the X-Agent
# header is a per-session ${AGENT_ROLE} template. URL is 127.0.0.1 on the daemon host,
# the LAN name elsewhere:
make register                                   # daemon host (defaults to 127.0.0.1:8765)
make register URL=http://bigbox.local:8765      # the Mac
make install-agent                              # the `agent <name>` launcher -> ~/.local/bin
export AGENTBUS_TOKEN=s3cret                     # the shared bus secret, in your shell profile
```

Then launch each SESSION with its own name — one tmux pane each:

```bash
agent roc-api-dev      # this pane is roc-api-dev      (sets AGENT_ROLE, runs claude)
agent roc-ui-dev       # another pane is roc-ui-dev
agent iphone-dev       # the Mac
```

Inside a session:

1. `join(url="http://.../")` → returns `wake_url` (identity comes from your header).
2. work; coordinate with `send` / `inbox` / `ack` (directed by default).
3. between turns, arm the wake client and end the turn:
   `uv run --project <repo> wake --url <wake_url> --name "$AGENT_ROLE" --token "$AGENTBUS_TOKEN" --timeout 1800`
4. on wake → `inbox` → act → `ack` → re-arm. (exit 2 = silence timeout → re-arm.)

`ws://` needs no TLS on a trusted LAN; a shared token stops stray devices. Want
encryption without certs? Put the daemon on Tailscale/WireGuard and keep `ws://`.

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
scripts/agent            launcher: `agent <name>` binds a session's identity ($AGENT_ROLE) and runs claude
tests/                   test_store.py, test_daemon.py + smoke_ws.py (real daemon, end to end)
docs/migrate-to-agentbus.md   move the exported agents onto the bus and resume work
```

## Status

The daemon (HTTP-MCP + WS wake + token auth), the cross-OS wake client, and the DAG
message model are built and covered by the unit suite plus a real end-to-end smoke
(daemon subprocess, two agents over HTTP-MCP, a pushed WS wake, auth rejection).

Migrating the exported agents onto the bus is the operational next step — see
`docs/migrate-to-agentbus.md`.
