# Migrate agents onto agentbus

Move the 10 exported agents (architect + 9 devs) onto the SQLite/MCP broker daemon:
connect to the bus, restore each agent's history from its export, import its
state/jobs/tasks into the distributed system, **bake agentbus into its CLAUDE.md** so
every future session uses it, and resume work.

State source of truth per agent: `~/.claude/agent-bus/export/<ROLE>.json`
(`architect`, `controller-api-dev`, `controller-ui-dev`, `minimal-mobile-dev`,
`roc-api-dev`, `roc-ui-dev`, `shared-dev`, `streaming-dev`, `vendor-api-dev`,
`vendor-ui-dev`). Each holds: `role, summary, repo, branch, open_tasks,
closed_tasks, needs, blocked_by, i_block, uncommitted_work, artifacts, next_steps`.

---

## Operator — one time, before any agent migrates

```bash
cd <claude-mcp repo>
make build                                   # verify green (unit + HTTP/WS smoke)

# 1. start the broker daemon on the always-on host (binds 0.0.0.0:8765):
AGENTBUS_TOKEN=<secret> make daemon          # leave it running (tmux / systemd)

# 2. register agentbus ONCE per machine (user scope). Identity is NOT baked in -- the
#    X-Agent header + bearer are ${VAR} templates Claude Code expands per session:
make register                                # daemon host  (Mac: make register URL=http://192.168.90.136:8765)

# 3. each session gets its identity per worktree via direnv. In each worktree's .envrc:
#       export AGENT_ROLE=roc-api-dev
#       export AGENTBUS_TOKEN=<secret>     # the shared bus secret (or set globally)
#    then: direnv allow
curl -s http://127.0.0.1:8765/health         # -> ok
```

Then, per agent, IN THIS ORDER:

1. **Re-export if the session is still live and newer than its export.** In-context
   task state is authoritative and a restart wipes it. Still running with current
   context? Run Appendix A first (overwrites its export). Already gone? The on-disk
   export is the source — skip this.
2. **Relaunch the session in its worktree:** `cd <worktree>` (direnv loads
   `AGENT_ROLE` + `AGENTBUS_TOKEN`) then `claude`. The restart is what loads the
   `agentbus` MCP tools, and direnv gives the session its identity.
3. **Paste the agent prompt below; tell it the broker URL** (`http://127.0.0.1:8765`
   on this box).

Joins are concurrent-safe. One daemon serves the whole fleet (local panes + the Mac),
each with its own identity from `$AGENT_ROLE`.

---

## Agent prompt — paste this; you will be told the broker URL

You are being migrated onto **agentbus**, a SQLite-backed broker daemon exposed over
MCP (HTTP) with a WebSocket for wakeups. Your prior state is exported on disk. Work the
phases in order. Do not skip the verify gates.

### PHASE 1 — verify you are connected (install)
1. Confirm the `agentbus` MCP tools are available (`join`, `send`, `inbox`, `ack`,
   `thread`, `trace`, `graph`, `presence`, `whoami`, `leave`). If they are missing,
   STOP — tell the operator the daemon is down or you were launched without
   `AGENT_ROLE`/`AGENTBUS_TOKEN` (relaunch in the worktree so direnv sets them).
2. Confirm the wake client runs: `uv run --project <claude-mcp repo> wake --help`.

### PHASE 2 — restore your history (from the export)
3. Your role is `$AGENT_ROLE` (also what `whoami` returns). Read
   `~/.claude/agent-bus/export/<role>.json`. If missing or invalid, STOP and report —
   do not invent state. This file is your prior working history.

### PHASE 3 — join the bus
4. Call `join` with `url="<broker url>"` (e.g. `http://127.0.0.1:8765`). Identity comes
   from your X-Agent header — no need to pass a name. Capture `wake_url` and `unread`.

### PHASE 4 — import your state/jobs/tasks into the distributed system
5. Rebuild your task list from the export:
   - each `open_tasks[]` -> a task (carry `status` in_progress/todo, `detail`,
     `blocked_by`, `blocks`); `next_steps[]` -> todos; `closed_tasks[]` -> done /
     reference (do NOT redo).
6. Restore working context: `uncommitted_work` (where you left off), `artifacts` (PRs,
   key files — reopen them), `repo` + `branch` (check out the branch if not on it).
7. Push your state into the bus so it is recorded in the broker, not just locally —
   post your status once (migration affects everyone, so broadcast is correct here):
   `send(to="*", subject="status", body="STATUS NEED:<needs> BLOCKED-BY:<blocked_by> I-BLOCK:<i_block> OPEN:[<open titles>] CLOSED:[<closed titles>]")`.
8. For each peer in `blocked_by`: `send(to="<peer>", body="...")` naming exactly what
   you need. If it continues a prior exchange, thread it with `reply_to`. `i_block`
   peers see your broadcast.

### PHASE 5 — adopt the MCP in your CLAUDE.md (durable)
This makes every FUTURE session use the bus, not just this one. Clean cutover — no shims.
9. In your repo's `CLAUDE.md`, DELETE any old coordination rules. Find them:
   ```
   grep -niE "dropbox|OVERSITEAI_DROPBOX|agent-bus|watch-loop|watch-standup|\.broadcast-read|\.read|bin/drop|poll .*dropbox|devchain" CLAUDE.md
   ```
   Remove the dropbox topology, `OVERSITEAI_DROPBOX`, poll/ls/find commands, the
   `.read`/`.broadcast-read` markers, `bin/drop`, any watch-* / devchain coordination
   rule. Leave all non-coordination rules (refactor, branch/release, repo topology) intact.
10. REPLACE the existing `## Coordination` section with the one from **Appendix B**
    (insert it if there is none). Note: after the reversed devchain trial, the residue
    is usually a **Devchain** coordination section + scattered "Devchain chat/board"
    lines — replace the section, and reword the stray lines (e.g. the architect
    REQUEST/ACK/GO flow) to route through agentbus instead of Devchain.
11. Verify nothing old remains:
    ```
    grep -niE "dropbox|OVERSITEAI_DROPBOX|\.broadcast-read|bin/drop|devchain" CLAUDE.md || echo CLEAN
    ```
    It must print `CLEAN`. Also remove the stale `OVERSITEAI_DROPBOX` export from the
    worktree's `.envrc` if present.

### PHASE 6 — resume work
12. Continue the in_progress task from `uncommitted_work`, on the right branch. Real
    coding — pick up where the export says, not from scratch.

### PHASE 7 — stay reachable
13. You are reachable automatically: launching via `scripts/agent <role>` (tmux) arms a
    wake sidecar that holds your wake socket at 0 tokens and pokes your pane on each
    message. Do not run wake yourself.
14. On a poke ("agentbus ring...") or any turn: `inbox` -> handle each (reply unicast,
    `reply_to` to thread; a `DIRECTIVE:LEAVE` body = you were kicked -> `leave`) -> `ack`
    the ids. No tmux -> no poke; just `inbox` each turn (messages queue durably).

### Done
On agentbus: connected, history restored, state imported, CLAUDE.md adopts the MCP,
resuming work, reachable. Coordinate by directed messages; broadcast only when most
peers are affected or after a long silence.

---

## Appendix A — re-export current state (still-live agent, before relaunch only)

Run ONLY if you are the still-running session and your in-context state is newer than
your export. Export FIRST, verify, THEN let the operator relaunch you.

1. `ROLE` = your role. Build a JSON snapshot of your FULL working state in exactly this
   shape (fill every field; `[]` / `""` when empty):
   ```json
   {
     "role": "<your role>",
     "summary": "one line: what you are currently working on",
     "repo": "<repo>", "branch": "<working branch>",
     "open_tasks": [
       {"title": "", "status": "in_progress|todo", "detail": "", "blocked_by": [], "blocks": []}
     ],
     "closed_tasks": [{"title": "", "detail": ""}],
     "needs": [], "blocked_by": [], "i_block": [],
     "uncommitted_work": "WIP not yet committed and EXACTLY where you left off",
     "artifacts": ["PR numbers, key files, paths"],
     "next_steps": []
   }
   ```
2. Write atomically: Write to `~/.claude/agent-bus/.tmp/<ROLE>.json`, then
   `mv ~/.claude/agent-bus/.tmp/<ROLE>.json ~/.claude/agent-bus/export/<ROLE>.json`.
3. Verify the gate (do not relaunch until it passes):
   ```
   python3 -c "import json,os; p=os.path.expanduser('~/.claude/agent-bus/export/'+os.environ.get('ROLE','')+'.json'); json.load(open(p)); print('EXPORT OK:', p)"
   ```

---

## Appendix B — the coordination section to paste into CLAUDE.md

```markdown
## Coordination (agentbus)

Cross-session/cross-machine coordination runs over the **agentbus** MCP server — a
SQLite-backed broker daemon. Use its MCP tools; never use files, a dropbox, polling,
.read/.broadcast-read markers, or any other channel.

Your identity is `$AGENT_ROLE` (set per worktree via direnv, sent as the X-Agent
header). The broker is at `http://127.0.0.1:8765` (the LAN name on remote machines).

On session start:
1. `join(url="http://127.0.0.1:8765")` -> your `wake_url` + unread count.
2. drain: `inbox` -> handle each -> `ack` the ids.
3. stay reachable: the sidecar from `scripts/agent <role>` pokes your pane on each message
   (0-token idle); don't run wake yourself. On a poke or any turn: `inbox` -> handle ->
   `ack`. No tmux -> `inbox` each turn.

Message style — caveman ultra (save tokens): write every bus message terse --
drop articles/filler/pleasantries, fragments OK, short synonyms. Keep ids, paths,
SHAs, error text, and the STATUS line VERBATIM (exact technical substance never
drops). e.g. "NEED architect ratify ADR-045. blocks #25/#20." not a paragraph.

Messaging — directed by default:
- Need from / blocking / unblocking a specific peer -> `send(to="<peer>", body=...)`.
  Unicast wakes one agent.
- Broadcast (`to="*"`) ONLY when most peers are affected (shared contract change,
  release) or after a long silence (heartbeat). A broadcast wakes everyone = N turns.
- Reply in-thread with `reply_to=<id>`; merge branches with `reply_to=[ids]`.
- `trace(message_id)` / `graph(thread_id)` to track how a decision was reached;
  `presence` to see who is live.

`DIRECTIVE:LEAVE` in a message = you were kicked -> `leave` and stop the loop.
```
