# watch-loop

Event-driven loop for Claude Code. The built-in `/loop` fires on a timer; `/watch-loop` fires the instant a watched file or directory changes — a real-time dropbox for agent-to-agent comms.

Linux only (uses the kernel's `inotify`). Zero dependencies: `python3` + the kernel. No pip, no `apt`, no `sudo`.

## How it works

```
[bg] fswatch.py PATHS  ->  blocks on inotify  ->  file changes  ->  exits, prints what changed
        ^                                                                |
        +------------------ re-arm ----------  run your PROMPT  <-- harness wakes the session
```

`fswatch.py` blocks until any watched path changes, prints the changed entries, and exits. Run it as a background task; when it exits the Claude Code session is re-invoked, runs your prompt against what changed, and re-arms the watcher. Repeat.

## Install

```bash
make install        # deploys into ~/.claude  (override: make install CLAUDE=/path/to/.claude)
```

Copies `src/fswatch.py` into `~/.claude/scripts/` and the two command files into `~/.claude/commands/`. Restart any running session (commands load at session start); new sessions pick them up automatically. `make uninstall` removes them.

## Usage

```
/watch-loop PATH [PATH ...] -- PROMPT
```

Paths before `--` are watched together; everything after `--` is the prompt run on each change. `/watch-loop` with no args prints usage.

```
/watch-loop ~/agent-inbox -- read new files, act on them, then move each to processed/
/watch-loop ./shared/state.json -- reload state and report what changed
/watch-loop ./dirA ./dirB ~/notes.md -- summarize what changed across all three
/watch-loop --timeout 60 ./inbox -- process new drops; also wake every 60s to housekeep
/watch-loop --all ./src -- rebuild on every save (wakes mid-write too)
```

Stop with `/watch-stop`. Esc alone won't — it interrupts the turn but the background watcher keeps running and re-wakes you.

## Behavior notes

- **One per session.** Calling `/watch-loop` again supersedes the running watcher in that session.
- **Per-session scope.** Each watcher is tagged with `$CLAUDE_CODE_SESSION_ID`, so `/watch-stop` (and the auto-replace) only touch this window's watcher. Other windows run their own, untouched.
- **Dir vs file.** Watching a directory catches create / move-in / delete / close-after-write of files inside it (non-recursive). Watching a file catches writes to that file.
- **Complete messages only.** By default the watcher fires on file-closed-after-write (`IN_CLOSE_WRITE`) and atomic move-in (`IN_MOVED_TO`), not on bare create — so a `> dir/file` writer wakes the loop at *close* (full content), never on the empty file. For atomic drops, write the temp file **outside** the watched dir, then `mv` it in (a temp created inside the watched dir would itself wake the loop on its own close). `--all` adds mid-write (`IN_MODIFY`) wakes.
- **Rescan on wake.** The loop rescans the watched dirs on each wake rather than trusting the event list alone — events between the watcher exiting and re-arming aren't captured, and the directory listing is the source of truth.

## fswatch.py directly

```
fswatch.py [--timeout SECONDS] [--all] [--tag VALUE] PATH [PATH ...]
fswatch.py --selftest
```

Exit 0 = something changed (paths printed). Exit 2 = `--timeout` elapsed with no change. Exit 1 = bad usage / watch error. `--tag` is an ignored argv marker used for per-session `pkill` targeting. `--selftest` runs a built-in assert-based check.

## Development

```
claude-mcp/
  src/fswatch.py          the watcher (stdlib + ctypes, no deps)
  commands/               the /watch-loop and /watch-stop slash-command definitions
  tests/test_fswatch.py   assert-based checks, no framework
  Makefile                build / test / install / uninstall / lint
```

```bash
make build      # == make test; nothing to compile (single zero-dep script)
make test       # run the suite
make lint       # ruff, if installed
make install    # deploy to ~/.claude
```

The command files hardcode the install path `~/.claude/scripts/fswatch.py`. Edit sources here, `make test`, `make install`, restart sessions.
