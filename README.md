# watch-loop — an agent message bus for Claude Code

Event-driven, real-time messaging between Claude Code sessions over the filesystem. Sessions sign up to a self-organizing global bus under a unique name, send each other unicast messages, and broadcast to a persistent shared log. A session in a `/watch-loop` reacts the instant a message arrives — no polling, no timers.

Linux only (kernel `inotify`). Zero dependencies: `python3` + the kernel. No pip, no `apt`, no `sudo`.

## How it works

**In-session constraint.** The model is the executor and only runs when invoked — it can't sit blocked. The only event-driven way to wake a sleeping session is a background process that *exits* (the harness wakes the session on task completion). So the loop is:

```
join bus ─► drain pending ─► run PROMPT over messages ─► ack ─┐
   ▲                                                          │ (rescan: caught anything that
   │                                                          │  arrived mid-prompt? loop)
   │  nothing pending                                         ▼
   └──── arm watcher (blocks on inotify) ◄── wake ◄── a message lands
```

The watcher (`fswatch.py`) blocks until a message arrives, then exits to wake the session; the session processes and re-arms it. It *behaves* like a daemon (idle-blocks, fires on change, coalesces, self-suppresses) but mechanically re-arms each cycle. The loop lives with the session — close the session and it stops.

**Self-suppression is structural, not PID-based** (inotify can't tell who wrote). Each agent watches only its own inbox + the shared broadcast dir, with an arrivals-only mask (`IN_CLOSE_WRITE | IN_MOVED_TO`). An agent never writes to its own inbox, and consumes by *moving* files to `processed/` (a move-out, ignored by the mask). So an agent's own activity can never trigger its loop — only real arrivals do.

**Nothing is lost or double-processed.** `pending` reads messages non-destructively; `ack` is what moves inbox files out and advances the broadcast cursor. A prompt that dies mid-evaluation loses nothing. Multiple arrivals are drained as one batch (no per-message storm), and a post-ack rescan catches anything that landed while the prompt ran.

**Broadcasts persist.** They append to a shared log; each agent has a cursor (last-read position), so agents that join later replay history (unless they join `--fresh`). The log is GC'd once every present agent has read past a message, or after 7 days.

## Install

```bash
make install        # deploys into ~/.claude  (override: make install CLAUDE=/path/to/.claude)
```

Copies `src/fswatch.py` + `src/bus.py` into `~/.claude/scripts/` and the four command files into `~/.claude/commands/`. Restart any running session (commands load at session start). `make uninstall` removes them.

## Usage

```
/watch-loop --name NAME [--fresh] -- PROMPT   join the bus, react to messages with PROMPT
/watch-send --to NAME | --all [--subject S] -- MESSAGE   unicast, or broadcast to everyone
/watch-list                                   who's on the bus (live / stale)
/watch-stop                                   leave the bus + stop this session's loop
```

Example — two sessions:

```
# session 1
/watch-loop --name reviewer -- review any diff I'm sent, reply to the sender with findings

# session 2
/watch-loop --name builder -- on each message run the build, then broadcast pass/fail
/watch-send --to reviewer -- here's the diff for PR 12: <...>
```

`reviewer` wakes the instant the message lands, runs its prompt, replies via `/watch-send --to builder`. No directories were configured by either side — they found each other by name on the global bus.

## Bus layout

Root is `$CLAUDE_AGENT_BUS` or `~/.claude/agent-bus`:

```
agents/<name>.json     presence: {name, tag, pid, joined}. join creates, leave deletes.
broadcast/<ts>-..json  append-only shared log, one file per broadcast (ts-ordered).
<name>/inbox/          the agent's mailbox; senders drop here (atomic move-in).
<name>/processed/      consumed messages, moved out of the inbox (unwatched).
<name>/cursor          last broadcast ts this agent has read.
.tmp/                  scratch for atomic writes (same fs -> rename is atomic).
```

## Behavior notes

- **Names are unique among live agents.** Joining a name a live agent holds fails; a stale name (its session gone) is reclaimed.
- **Liveness is best-effort** — an agent is "live" while its session's watcher process exists. An agent busy in a long prompt (watcher momentarily disarmed) can briefly read as stale. `bus.py prune` clears dead presence; broadcast GC tolerates ghosts via the 7-day cap.
- **Atomic drops.** Sends write to `.tmp/` then `rename` into the target inbox / broadcast dir, so a reader never sees a half-written message.
- **One loop per session.** Re-running `/watch-loop` supersedes the running watcher (tagged by `$CLAUDE_CODE_SESSION_ID`); `/watch-stop` kills only this session's watcher.

## Development

```
src/fswatch.py          low-level inotify blocker (stdlib + ctypes, no deps)
src/bus.py              the bus: join/leave/list/prune/send/pending/ack/whoami/paths
commands/               the four slash-command definitions
tests/                  test_fswatch.py + test_bus.py (assert-based, no framework)
Makefile                build / test / install / uninstall / lint
```

```bash
make build      # == make test; nothing to compile
make test       # run both suites
make install    # deploy to ~/.claude
```

### CLI reference

```
fswatch.py [--timeout S] [--all] [--arrivals] [--tag T] PATH [PATH ...]
  Blocks until a watched path changes; prints changes, exits 0. Exit 2 = timeout, 1 = error.
  --arrivals = wake only on new content / move-in (consuming by move-out won't re-trigger).

bus.py join   --name N [--tag T] [--fresh]        sign up (prints inbox + broadcast dirs)
bus.py send   --from N (--to N | --all) [--subject S] [--body B|-]
bus.py pending --name N [--json]                  non-destructive: unread inbox + broadcasts
bus.py ack    --name N [--consumed f1,f2] [--cursor TS]   move consumed out, advance cursor
bus.py whoami [--tag T]    list [--json]    prune    leave --name N    paths --name N
```
