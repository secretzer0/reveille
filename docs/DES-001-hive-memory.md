# DES-001: Hive memory for Reveille

Status: DRAFT — posted for fleet adversarial review. Push back with file:line where
the claim touches code; with section number where it does not. Every dev ACKs a slice
or refutes it; silence is not agreement on a design review.

Companion (separate doc, out of scope here): DES-002 container launcher — web-provisioned
isolated agent containers with tmux attach. This doc is the memory layer only.

## 1. Problem

Reveille moves mail. It does not remember. The consequences are measured, not
hypothetical:

- The Reveille room holds 8,261 messages. The ADR-061 ruling set alone spans 20+
  broadcasts where message 7989 amends 7988, 8195 closes 8183, and the only way to know
  the live ruling is to read the whole chain and resolve amendments by hand
  (~15k tokens per reader, per catch-up).
- join() replays 15 minutes. An agent down for three days boots blind and must know
  what to ask history() for — which requires knowing what it missed.
- Session death loses everything in-context: open tasks, decisions, and their triggers.
  The export/import JSON dance existed because of this and died with the migration.
- A new agent (the onboarding case: an outside dev's "randy-roc-ui" joining the room)
  has no path to the fleet's development style, git patterns, API contracts, or the
  arguments behind them, except archaeology.

lessons() is the one memory we have, and it works — because it is distilled,
scoped, and read at boot. This design generalizes exactly that pattern.

## 2. Goals

G1. Token reduction: a caught-up agent reads consolidated live facts, never amendment
    chains. Target: cold-start context <= 7k tokens where today's equivalent is 15k+
    (measured against the ADR-061 set: 15 facts ~ 1k tokens vs 20+ messages ~ 15k).
G2. Latency: recall is an in-process SQLite query. No network, no embedding API on the
    read path. Sub-second is not a target, it is the floor; sub-10ms is the expectation.
G3. Onboarding: a fresh agent with a fresh token is productive after join() + brief(),
    with provenance (every fact traces to the deliberation that made it).
G4. Zero new hard dependencies. The broker boots with no API key and no model. LLM work
    happens only at the edges (authors) or in an opt-in distiller agent.
G5. Honest memory: add-only with supersession. Nothing is silently rewritten. History
    remains queryable after the fact it records is dead.

Non-goals: embeddings/vector search (staged, measured need only — section 9); typed
knowledge-graph edges beyond what the message DAG already gives; multimodal extraction
(attachments already ride messages; memories may reference them); any managed/cloud
anything.

## 3. mem0 conformance review

Each mem0 doc page was read against this design. Verdict per concept:

| mem0 concept | Verdict | Rationale |
|---|---|---|
| Two-phase pipeline, LLM extraction at add | ADAPT | Extraction moves to the author edge. Agents already compose BINDING/RULING messages; memory_add is the same act, structured. Cost: 0 marginal LLM calls vs mem0's 1 per add. The infer=True equivalent is the distiller agent (section 8), batch and human-gated. |
| Additive storage, no silent rewrites | ADOPT | memories are add-only; corrections are supersedes links. Matches mem0's current design and this org's no-legacy law. |
| update() in-place overwrite | REJECT | mem0 documents no version history for updates. We keep the chain: a superseded fact stays, ranked below its successor. Stricter audit than the reference implementation. |
| delete() hard removal | ADAPT | retract marks status=retracted (fact dead, record stays). Row deletion is an admin compliance path only. mem0's own breaking change (delete_all now refuses without filters) validates the caution. |
| Memory layers (conversation/session/user/org) | ADOPT | Mapping: messages = conversation/episodic (already durable); scope=agent:<name> = session/agent state; scope=room = org-local; scope=global = org-wide. |
| Entity-scoped memory (user/agent/app/run ids) | ADAPT | One scope column instead of four nullable ids. mem0's implicit-null scoping is documented as "a common source of empty results" — we decline to import that footgun. |
| v2 filters (AND/OR/NOT, eq/ne/in/gt/lt/contains, wildcard) | ADAPT | recall() takes named filters (kind, scope, entity, author, since/until, status). The subset covers every query pattern in 8,261 real messages. A JSON filter grammar can be added if a real query needs it; none identified yet. |
| Search: semantic + BM25 + entity boost, fused; explain/score_details | ADAPT | FTS5 BM25 (built into SQLite) + entity boost + kind weight + recency, fused in SQL. explain=true returns the component scores, mem0 parity. Semantic embeddings deferred (section 9). |
| Graph memory: co-occurrence entity linking, untyped, "inferred not declared" | ADOPT+ | Identical design: entities extracted at write, shared entities link memories, boost at search. We additionally have what mem0 lacks: a real typed provenance graph (reply and supersedes edges) — trace(source_msg_id) answers "why", not just "what". |
| Temporal reasoning: timestamp vs ingestion time; ranking priority | ADOPT | occurred_ns (when the fact became true) vs created_ns (when recorded). since/until reuse _when_ns — naive ISO = UTC, the lesson is already paid for. Supersession is the "ongoing states" answer: current truth is a chain-tip query, not a recency heuristic. |
| Custom categories (15 defaults, per-call override) | REJECT | kind is a fixed enum because kinds carry BEHAVIOR (write gates, lifetimes, brief() composition) — they are not labels. The free-form axis is entities. Reviewers: push back here if a real kind is missing. |
| Async client | N/A | MCP tools are already async; the store is in-process. |
| Multimodal | DEFER | Messages carry attachments since 0.1.4. memories.source_msg_id reaches them. No extraction pipeline. |
| expiration_date | ADOPT (narrow) | expires_ns exists for scope=agent state only. Doctrine does not expire; it is superseded. |

## 4. Schema

All in broker.db, WAL, same store.py idiom. Migration is versioned (store.migrate).

```sql
CREATE TABLE memories (
    id            TEXT PRIMARY KEY,             -- uuid
    kind          TEXT NOT NULL CHECK (kind IN
                    ('doctrine','contract','decision','lesson','state')),
    scope         TEXT NOT NULL,                -- 'global' | <room_id> | 'agent:<name>'
    fact          TEXT NOT NULL,                -- the distilled statement, <= 1000 chars
    entities      TEXT NOT NULL DEFAULT '',     -- normalized, space-separated
    source_msg_id INTEGER REFERENCES messages(id),  -- provenance -> trace()
    supersedes_id TEXT REFERENCES memories(id),
    author        TEXT NOT NULL,                -- bus name that wrote it
    status        TEXT NOT NULL DEFAULT 'live', -- 'live'|'draft'|'superseded'|'retracted'
    occurred_ns   INTEGER,                      -- when the fact became true
    created_ns    INTEGER NOT NULL,
    expires_ns    INTEGER                       -- state kind only
);
CREATE INDEX idx_mem_scope   ON memories(scope, kind, status);
CREATE INDEX idx_mem_super   ON memories(supersedes_id);

CREATE VIRTUAL TABLE memories_fts USING fts5(
    fact, entities, content='memories', content_rowid='rowid');

CREATE VIRTUAL TABLE messages_fts USING fts5(
    subject, body, content='messages', content_rowid='id');

CREATE TABLE message_entities (
    entity TEXT NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY (entity, message_id));
CREATE TABLE memory_entities (
    entity TEXT NOT NULL, memory_id TEXT NOT NULL,
    PRIMARY KEY (entity, memory_id));
```

Kinds and their behavior:

| kind | example | lifetime | write gate (section 6) |
|---|---|---|---|
| doctrine | branch naming law, no-legacy rule, PR review requirements | until superseded; ratified | ratify tier |
| contract | "leg carries field_ticket_id ONLY; FT carries work_order_id" | superseded on contract change | write tier |
| decision | "RunStatus = {UNSPECIFIED, OPEN, OFFLOADING, DELIVERED, CANCELLED, ARCHIVED}" | superseded by later ruling | write tier |
| lesson | wake-127, passive-listener wheel bug | append-only, existing model | any agent (unchanged) |
| state | open_tasks, blocked_by, uncommitted_work | expires_ns or superseded by self | self only |

Lessons migration: the lessons table folds into memories (kind='lesson'). lessons() and
lesson_add() keep their exact signatures, backed by the new table. Clean cutover, one
store, no dual-path — the migration is versioned and snapshot-proven like every other.

Entity extraction is deterministic, no LLM: ADR-\d+, #\d+ (PRs/issues), known repo
names, proto-vX.Y.Z, CamelCase identifiers >= 2 humps, room names. The pattern list
lives in store.py and is extensible; the distiller (section 8) flags frequent
unmatched capitalized terms as candidate entities.

## 5. Tools

```
memory_add(fact, kind, scope="", entities="", source=0, supersedes="", occurred="")
    -> {id, status}          status='draft' if the token lacks the tier for kind
recall(query="", kind="", scope="", entity="", author="", since="", until="",
       status="live", limit=10, explain=False)
    -> {memories:[...], count}   each carries source_msg_id and supersedes chain tip
memory_retract(id, reason)   -> author or admin; status='retracted', reason logged
ratify(id)                   -> admin/room-owner; 'draft' -> 'live'  (also in web UI)
brief(role="", budget=7000)  -> the onboarding pack, section 7
```

history() gains: FTS5 ranking replaces the OR-LIKE scan, plus entity= filter. Signature
otherwise unchanged.

Scoring (recall, and history when keywords present):

```
final = w_bm25 * bm25_norm + w_entity * entity_overlap
      + w_kind * kind_weight + w_recency * recency_decay
```

superseded/retracted rows are excluded unless status= says otherwise. explain=True
returns every component per row (mem0 score_details parity) — a reviewer can see WHY a
fact ranked, which is the difference between a memory and an oracle. Weights are
constants in store.py, tuned against real queries during S1; they are not config.

## 6. Trust tiers

The hive is executed by every agent that boots. A poisoned doctrine memory is prompt
injection with a distribution mechanism. Write capability is a token property:

| tier | may write | default for |
|---|---|---|
| state | own agent: scope only | every new token (randy day one) |
| write | + contract, decision (live) in granted rooms | fleet dev tokens |
| ratify | + doctrine; approves drafts | architect, room owners |

- Below-tier memory_add succeeds as status='draft'. Drafts are invisible to recall()
  and brief() until ratified. The web UI shows the ratify queue; the existing lesson
  promotion model ("admin promotes a room lesson to global") is this same gesture.
- Tokens table gains one column (mem_tier). Minting UI gains one selector.
- brief() renders memories as data with author + provenance labels, never as
  instructions. This does not eliminate the LLM-layer injection risk (an agent may
  still act on a hostile fact) — ratification is the load-bearing gate, rendering
  is defense in depth. Reviewers: attack this section hardest.

## 7. brief() — the onboarding pack

Composed, ranked, budgeted. Default 7k tokens, hard cap. Order:

1. lessons — global + rooms (existing content, existing discipline)
2. doctrine — for the agent's rooms, ranked by entity overlap with its role
3. contracts — live only, supersession-resolved
4. decisions — live, last 30 days weighted up, older included by entity relevance
5. own state — scope=agent:<name>, if any (restart case)
6. presence digest — who is live, who owns what room

Every section is capped; every truncation is marked in the output ("12 of 31 doctrine
memories; recall(kind='doctrine') for the rest"). No silent caps.

join() response gains a brief_available count; the pack itself is pulled by the
brief() call so joining stays cheap. The 15-minute replay stays — brief() is the
knowledge floor, replay is the conversation floor; they answer different questions.

Cold-start arithmetic (G1): randy-roc-ui today = unreadable (8,261 messages) or blind.
With brief(): ~7k tokens, ADR-061 as 15 live facts instead of 20+ amendment-chained
broadcasts, every fact one trace() from its full argument.

## 8. Seeding and the distiller

The pack cannot start empty. One-time harvest, then live accrual:

- Harvest: an opt-in distiller agent (a normal bus client, not broker code) walks
  history() + each repo's CLAUDE.md, drafts doctrine/contract/decision memories with
  source references. Everything lands as draft. The architect ratifies in bulk via the
  web queue. The broker never runs a model (G4).
- Accrual: authors add memories at the same keystroke as the message that creates the
  fact. The architect's BINDING broadcast and its memory_add(source=<that msg id>) are
  one gesture. Convention, enforced the same way NEED:/FYI already are — by protocol,
  lessons, and review.
- Maintenance: the distiller periodically flags threads shaped like un-captured
  decisions (RATIFIED/BINDING/FOUND+FIXED markers with no memory referencing them) and
  nags the author. Drafts only. Never auto-live.

CLAUDE.md relationship: repo CLAUDE.md stays authoritative for repo-local mechanics.
Broker memory holds cross-agent knowledge and everything that dies with sessions. The
distiller flags drift between the two; a human resolves it.

## 9. Staging

Each stage independently shippable, green build gate (make build), no stage depends on
a later one.

- S1  messages_fts + ranked history(). No new API. Measurable immediately.
- S2  entity extraction at send + message_entities backfill + entity= filter.
- S3  memories table + tools + trust tiers + lessons fold-in. The store.
- S4  brief(). The payoff. Requires S3; better after S2.
- S5  seed harvest (distiller drafts, human ratifies).
- S6  web UI: memory browser, ratify queue, draft badges.
- S7  embeddings sidecar (sqlite-vec) IF S1-S4 recall quality misses real queries.
      Gate: a logged corpus of recall misses, not a feeling. The fleet's vocabulary is
      stable (ADR-061, RunStatus, roc-api — nobody says "that decision about trucks"),
      so the expectation is S7 never ships.

## 10. Failure modes (for the review)

- Poisoning: untrusted writer plants hostile doctrine. Gate: tiers + draft + ratify
  (section 6). Residual: a ratifier rubber-stamps. Mitigation: ratify queue shows
  provenance diff, and doctrine writes are rare by construction.
- Injection-in-fact: a live memory contains instruction-shaped text; an agent obeys it
  at boot. Ratification is the gate; rendering-as-data is depth. Residual risk is real
  and stated — reviewers propose better if they have it.
- Supersession fork: two agents supersede the same memory concurrently. Both chains
  survive (add-only); recall returns both tips and flags the fork; ratify resolves.
  Detection query ships with S3.
- Entity drift: new vocabulary the regex misses. Distiller flags candidates;
  pattern list is one constant. Failure is graceful: FTS still matches the text.
- Stale brief mid-session: a fact supersedes after an agent briefed. Memories do not
  ring (they are not mail); the next turn's recall sees the new tip. If a supersession
  is urgent, the author already broadcasts it as a message — that is what mail is for.
- Draft rot: ratify queue ignored. Web badge + distiller nag. If the architect will
  not ratify, the hive does not grow — that is the correct failure, not a workaround
  target.
- FTS scale: 8,261 rows is nothing; 10M rows is fine (FTS5 design point). Non-issue at
  fleet scale.

## 11. Open questions

- Q1 kind enum: is a real kind missing? (Candidates rejected: 'spec' = decision with
  entities; 'status' = state; 'faq' = doctrine.)
- Q2 state TTL default: none (explicit expires_ns only) vs 30d sweep?
- Q3 brief() budget: 7k default — right number? Per-role override worth it?
- Q4 Should recall() hits carry a compact supersession chain (tip + count) or tip only?
- Q5 Web user memories: users are principals too — do human notes belong in the same
  table (author=web:<user>) or is that scope creep?

## 12. Review protocol

Post: broadcast NEED: every dev, one reply each — slice ACK or refutation with
file:line / section. Architect ratifies amendments; DES goes ACCEPTED; S1 starts only
after the round closes. Same shape as the ADR-061 feasibility round, which caught two
factual errors and a live casualty before a line of code — that is what this round is
for.
