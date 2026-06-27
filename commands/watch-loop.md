---
description: "Run a prompt every time watched files/dirs change (real-time, inotify-driven). Event-driven sibling of /loop."
argument-hint: "PATH [PATH ...] -- PROMPT"
---

Start a **filesystem-triggered loop**. Unlike `/loop` (timer-based), this fires the moment a watched path changes — a real-time dropbox for agent-to-agent comms.

**If `$ARGUMENTS` is empty, do NOT start anything — print this usage and stop:**

```
/watch-loop PATH [PATH ...] -- PROMPT

Runs PROMPT every time any watched file/dir changes (real-time, inotify).

Examples:
  /watch-loop ~/agent-inbox -- read new files, act on them, then move each to processed/
  /watch-loop ./shared/state.json -- reload state and report what changed
  /watch-loop ./dirA ./dirB ~/notes.md -- summarize what changed across all three
  /watch-loop --timeout 60 ./inbox -- process new drops; also wake every 60s to housekeep
  /watch-loop --all ./src -- rebuild on every save (wakes mid-write too)

Paths before `--` are watched together. Everything after `--` is the prompt.
A dir catches move-in/delete/close-write inside it; a file catches writes to it.
Stop with /watch-stop (Esc alone won't — the bg watcher keeps running).
```

Otherwise, parse `$ARGUMENTS`: everything before `--` is the watch PATH list; everything after `--` is the PROMPT to run on each change. If there is no `--`, treat the first token as the path and ask the user for the prompt.

Then run this loop:

1. **Replace any existing watcher for this session** — one watch-loop per session; a new `/watch-loop` supersedes the old one. Run this as its OWN foreground command (not combined with the arm step):
   ```
   pkill -f "[f]swatch.py --tag $CLAUDE_CODE_SESSION_ID" || true
   ```
   It must be separate: if you put the `python3 ...fswatch.py...` arm in the same command line, pkill matches that un-bracketed `fswatch.py` in its own shell and self-kills (exit 144). Run pkill alone first.

2. **Arm the watcher** in the background (this is what makes it event-driven, not polled):
   ```
   python3 ~/.claude/scripts/fswatch.py --tag "$CLAUDE_CODE_SESSION_ID" <PATHS>
   ```
   Launch it with `run_in_background: true`. The `--tag "$CLAUDE_CODE_SESSION_ID"` stamps this session's id into the process so `/watch-stop` can kill THIS window's watcher only (other windows run their own, untouched) — always include it. It blocks on inotify and exits — printing the changed entries — the instant any watched path changes. End your turn; the harness re-invokes you when it exits.

3. **On wake**, do NOT trust the printed list alone. **Rescan the watched dirs** (`ls`/Read) — the directory listing is the source of truth. Events that land between the watcher exiting and you relaunching it are not captured, so the listing closes that race. ponytail: rescan-on-wake, not perfect event capture.

4. **Run the PROMPT** against what actually changed/arrived. For a dropbox/mailbox, after processing a file, move it aside (e.g. into a `processed/` or `.done/` subdir) so the next rescan doesn't reprocess it.

5. **Re-arm** — re-run step 2 (the arm command) as a fresh background call, then end your turn. No pkill on re-arm — the watcher already exited when it woke you; step 1's pkill is only for superseding a watcher at `/watch-loop` invocation time. Repeat until stopped.

**Stopping:** run `/watch-stop`, or the user says "stop". Then kill THIS session's watcher — `pkill -f "[f]swatch.py --tag $CLAUDE_CODE_SESSION_ID"` (or `TaskStop` the bg task) — and do not re-arm. NOTE: Esc alone does NOT stop the loop; it interrupts the turn but the background watcher keeps running and will re-wake you when a file changes. The watcher process must be killed.

Notes:
- Watching a **directory** catches move-in/delete/close-after-write of files inside it (non-recursive). Watching a **file** catches writes to that file.
- Default fires only on *complete* messages: file closed after write (`IN_CLOSE_WRITE`) or atomically moved in (`IN_MOVED_TO`) — not bare create, so a `> dir/file` writer wakes you at close (full content), not on the empty file. For atomic drops, write the temp **outside** the watched dir then `mv` it in (a temp inside the watched dir would wake you on its own close).
- Add `--all` before the paths to also wake on in-progress writes (`IN_MODIFY`); add `--timeout SECONDS` to also wake periodically even with no change (exit code 2 = timed out).
