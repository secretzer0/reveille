# Backlog

Ideas captured but not yet scheduled.

## Threaded-history retrieval — make the DAG useful to Claude while coding

The thread/DAG history is durable external memory + decision provenance, but only
pays off if an agent can pull the *right slice* cheaply (dumping the DB into context
burns tokens). Add targeted retrieval so history is useful at the keystroke:

- **`search(text)`** — full-text over message bodies via SQLite **FTS5** (built into
  stdlib `sqlite3`, no new dep). Ranked hits, not a scan. "pull prior discussion
  about retries" before touching the retry code.
- **message `refs` + `find(ref)`** — tag a message with what it touches (file paths,
  PR numbers, symbols); `find("auth.py")` returns every message about it. One table +
  one index. Highest-leverage piece: wires the conversation graph to the code graph.
- **`recap(thread)`** — a digest message an agent writes that becomes the thread's
  new head, so long threads stay token-cheap to catch up on.

Coding-time value this unlocks:
- About to edit a file -> `find(file)` surfaces the prior decision/warning on it.
- "Why is this like this?" -> `trace(decision_id)` shows the fork/merge of how it was
  decided (provenance, already built).
- Woke after context compaction -> `inbox` + `thread` reconstitute the task from
  durable storage; the bus is the memory the context isn't.
- New agent mid-epic -> `graph(thread_id)` is the whole design conversation.

Loop it ties into: **wake -> `inbox` -> before touching a file, `find`/`search` ->
act -> `send` a decision/result back with `refs`** so the next agent (or future you)
inherits it. The bus accretes a queryable decision record mirroring the codebase.

`trace`/`graph` already give provenance; `search` + `refs`/`find` give the lookup.
Build `search` + `refs`/`find` first (small, stdlib-only); `recap` after.
