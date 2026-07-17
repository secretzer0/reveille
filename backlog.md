# Backlog

Ideas captured but not yet scheduled.
Decisions and their reversal triggers: see DECISIONS.md. Read it before proposing a
rewrite, Postgres, or a pricing model -- all three are settled with measurements.

## Next, in the order it will bite (the platform path)

1. **Ingress** -- pve0 is behind residential NAT; nothing is reachable from outside.
   Cloudflare Tunnel: no port forward, no static IP, keeps the home IP off the internet.
2. **Wildcard TLS via DNS-01** (`deploy/Caddyfile` assumes it). Per-subdomain certs hit
   Let's Encrypt's ~50/week cap and wedge at ~customer 50 -- and the failure lands on new
   signups, the one path that must never break.
3. **litestream** -> rustfs (fast tier) AND an offsite tier (~$5/mo). rustfs alone shares a
   failure domain with pve0: fire, theft, surge, LAN ransomware.
4. **ZFS quota per tenant** -- `zfs create -o quota=2G Pool0/reveille/<tenant>`. The app cap
   (REVEILLE_QUOTA_BYTES) is legibility, not a guarantee; ENOSPC on the DB is worse than a
   refused upload.
5. **Per-agent tokens, with the token bound to its agent name.** Today ONE token serves the
   whole fleet and X-Agent is self-asserted: no attribution, and revoking one agent kills
   all of them. Security now, metering later (agents == tokens == one count(*)).
6. **The distiller** -- the free 4th agent. Runs on the CUSTOMER's box with THEIR
   credentials, compacts room history into scoped lessons, writes them back locally. No
   data leaves their machine. It is the token-savings thesis made visible, and it is what
   makes a retention limit humane: raw mail expires, distilled knowledge survives.
   Overlaps heavily with `recap` below.
7. **Move the last 2 raw queries out of daemon.py** (`_notify`, `_parent_room`)
   so `store.py` is the only file with SQL. ~15 min; makes any future engine swap one file.

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
