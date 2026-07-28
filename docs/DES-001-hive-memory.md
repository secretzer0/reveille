# DES-001: Hive memory for Reveille

Status: ACCEPTED — review round 1 closed 2026-07-28. Architect amendments (v2) +
senior-dev ACK with corpus measurements (bus msg 8366, thread 8365); round-close
record in section 13. S1 is unblocked. Future changes go through a new round per
section 12.

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
    with provenance (every fact traces to the deliberation that made it, WHILE the
    source is retained — retention is a pricing lever and expired mail takes its
    provenance with it; the fact survives, its source_msg_id goes NULL. Amended R1-B3).
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
    -- INTEGER PK (rowid alias), NOT a uuid TEXT PK: memories_fts is external-content
    -- keyed on rowid, and VACUUM renumbers IMPLICIT rowids -- snapshot() is VACUUM INTO
    -- (store.py:399) and "the snapshot IS the undo", so a TEXT PK would silently corrupt
    -- FTS on the first restore. The uuid callers see lives in uid. (Amended R1-B2)
    id            INTEGER PRIMARY KEY,
    uid           TEXT NOT NULL UNIQUE,         -- uuid, the external identifier
    kind          TEXT NOT NULL CHECK (kind IN
                    ('doctrine','contract','decision','lesson','state')),
    scope         TEXT NOT NULL,                -- 'global' | <room_id> | 'agent:<token_id>'
    fact          TEXT NOT NULL CHECK (length(fact) <= 1000),
    entities      TEXT NOT NULL DEFAULT '',     -- normalized, space-separated
    -- ON DELETE SET NULL, not bare REFERENCES: _delete_messages (store.py:762) is the
    -- single delete choke point behind sweep_retention/purge_room/prune_agent/
    -- delete_if_unseen, and foreign_keys=ON (store.py:269) would otherwise make the
    -- hourly retention sweep raise an FK violation forever the moment one memory cites
    -- an expired thread. Raw mail expires; the fact survives with source_msg_id NULL.
    -- (Amended R1-B3)
    source_msg_id INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    supersedes_id INTEGER REFERENCES memories(id),
    -- lesson kind only: the slug + the four structured fields lessons() must keep
    -- returning (store.py:1350-1354). NULL for every other kind. (Amended R1-B1)
    slug          TEXT,
    symptom       TEXT, root_cause TEXT, rule TEXT, detection TEXT,
    author        TEXT NOT NULL,                -- bus name that wrote it
    status        TEXT NOT NULL DEFAULT 'live', -- 'live'|'draft'|'superseded'|'retracted'
    occurred_ns   INTEGER,                      -- when the fact became true
    created_ns    INTEGER NOT NULL,
    expires_ns    INTEGER                       -- state kind only
);
CREATE INDEX idx_mem_scope   ON memories(scope, kind, status);
CREATE INDEX idx_mem_super   ON memories(supersedes_id);
CREATE INDEX idx_mem_slug    ON memories(scope, slug) WHERE slug IS NOT NULL;

CREATE VIRTUAL TABLE memories_fts USING fts5(
    fact, entities, content='memories', content_rowid='id');

-- DEFECT, found 2026-07-28, fix rides the next schema bump: this indexes `fact` and
-- `entities` ONLY, while B1 folded lessons into this same table with four MORE text
-- columns -- symptom, root_cause, detection, and rule. For a lesson, `fact` mirrors
-- `rule`, so symptom / root_cause / detection are UNSEARCHABLE. Demonstrated on the
-- live hive: the lesson host-pkill-reaches-container-daemon prescribes
-- `pkill -f agentbus-daemon` in its root_cause, and recall(query='agentbus') returns
-- ZERO while recall(query='pkill') returns it -- 'pkill' is in the rule, 'agentbus'
-- is not.
--
-- Two consequences, both load-bearing. (1) SYMPTOM is how a human or agent actually
-- looks for a lesson: you have the failure in front of you and search what you SEE,
-- which is the one field search cannot reach. (2) DETECTION is where commands live,
-- so an audit sweeping the hive for a stale identifier -- exactly what a rename
-- requires -- silently misses the lessons that prescribe it. A sweep must therefore
-- enumerate full lesson rows via lessons() and match client-side; recall(query=) is
-- NOT a sufficient instrument, and any doctrine that says otherwise is wrong.
--
-- Fix: index symptom, root_cause, detection alongside fact, and re-index every row in
-- the migration -- the same discipline the entity extractor already carries (a pattern
-- change rides a schema bump, or the index and the data disagree).

-- messages.id is already a rowid alias (INTEGER PRIMARY KEY AUTOINCREMENT), so this
-- binding is VACUUM-safe as-is.
CREATE VIRTUAL TABLE messages_fts USING fts5(
    subject, body, content='messages', content_rowid='id');

CREATE TABLE message_entities (
    entity TEXT NOT NULL, message_id INTEGER NOT NULL,
    PRIMARY KEY (entity, message_id));
CREATE INDEX idx_msgent_msg ON message_entities(message_id);  -- delete-by-message path
CREATE TABLE memory_entities (
    entity TEXT NOT NULL, memory_id INTEGER NOT NULL,
    PRIMARY KEY (entity, memory_id));
CREATE INDEX idx_mement_mem ON memory_entities(memory_id);
```

FTS sync is MANUAL, in the store choke points -- no triggers. `_exec_script` splits DDL
on `;` (store.py:234-247), which would shred a `CREATE TRIGGER ... BEGIN ...; END`
body; and manual sync keeps all SQL in store.py (DECISIONS: engine swap stays a
one-file change). The complete write set: send() insert, `_delete_messages`,
memory_add, and the lesson upsert path. Status flips (ratify/retract/supersede) touch
no indexed column and need no FTS write.
Delete ordering is LOAD-BEARING (R1 round close, senior-dev impl note): the FTS5
external-content 'delete' command -- INSERT INTO fts(fts, rowid, cols...)
VALUES('delete', ...) -- must carry EXACTLY the values the index holds, so
_delete_messages SELECTs the doomed rows' indexed columns BEFORE deleting them, in
the same transaction. Wrong order is the silent-corruption class B2 exists to
prevent. And the entity tables carry no FK by design, so their cleanup is an
EXPLICIT statement in _delete_messages' write set (message_entities by message_id;
memory_entities on memory retraction-by-admin) -- code, not a comment. The S1 migration backfills messages_fts for
the existing backlog; any future rebuild-the-table migration (the v0 pattern,
store.py:364-383) must rebuild the FTS table alongside. Migrations chain one
user_version bump per stage (v4->5->6->7); the fresh-db path lays the final schema
directly, per migrate()'s existing branching discipline (store.py:282-305).
(Amended R1-M2)

Supersession constraints (Amended R1-M6):
- A supersede targets the SAME scope and SAME kind only -- a room-tier writer must not
  be able to kill global doctrine by "superseding" it. ONE sanctioned exception
  (S3 review, F1 in bus msg 8378): lesson PROMOTION is a cross-scope supersede --
  the global successor row carries supersedes_id to the room tip it replaces, so the
  chain answers "where did this global rule come from". The exemption is an explicit,
  commented branch in the constraint check, admin-gated by the promotion path itself;
  nothing else may cross scopes.
- The target flips to 'superseded' ONLY when the successor becomes live. A draft
  supersede leaves its target untouched -- otherwise a below-tier writer kills doctrine
  by drafting against it.
- Flip + insert are one tx() (BEGIN IMMEDIATE, store.py:250-260); that transaction is
  what makes the concurrent-fork story in section 10 true rather than hopeful.

Kinds and their behavior:

| kind | example | lifetime | write gate (section 6) |
|---|---|---|---|
| doctrine | branch naming law, no-legacy rule, PR review requirements | until superseded; ratified | ratify tier |
| contract | "leg carries field_ticket_id ONLY; FT carries work_order_id" | superseded on contract change | write tier |
| decision | "RunStatus = {UNSPECIFIED, OPEN, OFFLOADING, DELIVERED, CANCELLED, ARCHIVED}" | superseded by later ruling | write tier |
| lesson | wake-127, passive-listener wheel bug | append-only, existing model | any agent (unchanged) |
| state | open_tasks, blocked_by, uncommitted_work | expires_ns or superseded by self | self only |

Lessons migration (Amended R1-B1): the lessons table folds into memories (kind='lesson')
via the nullable structured columns above -- lessons() and lesson_add() keep their exact
signatures AND their exact return shape (slug/symptom/root_cause/rule/detection,
store.py:1350-1354), which a single 1000-char fact column could not carry. Two
in-place mutations become supersessions, because add-only (G5) is not negotiable:
- Slug re-use was an UPSERT replace (store.py:1335-1347); it becomes a superseding row.
  lessons() returns chain tips, so the observable behavior ("re-using a slug replaces
  it") is unchanged while the history survives.
- promote_lesson mutated room_id in place (store.py:1371-1377); promotion becomes a
  superseding row at scope='global' authored by the promoting admin.
Clean cutover, one store, no dual-path — the migration is versioned and snapshot-proven
like every other, and scripts/seed-lessons is updated in the same change.

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
    -> {memories:[...], count}   each carries source_msg_id and supersedes chain
                                 tip + chain length + fork flag (Q4, resolved)
memory_retract(id, reason)   -> author or admin; status='retracted', reason logged
ratify(id)                   -> see section 6 for WHO; 'draft' -> 'live' (also web UI)
brief(role="", budget=28000) -> the onboarding pack, section 7. budget is in CHARS
                                (~4 chars/token; the broker has no tokenizer -- G4 --
                                so a "token cap" would be a dishonest label. R1-M5)
```

memory_add scope resolution never guesses (Amended R1-M4): scope="" with a 2+-room
token raises the same room_required refusal as resolve_send_room (store.py:933-954),
for the same reason -- a contract written into the wrong room is a non-recoverable
disclosure, and an error is recoverable while a guess is not.

recall() read scoping is the same invariant every read path already obeys
(store.py:30-34): rows where scope='global' OR scope IN (caller's rooms) OR
scope=agent:<own token>. Other agents' agent: scopes are never returned, at any
status=. Authors see their OWN drafts via status='draft'; ratify-tier callers see all
drafts in rooms where their tier is effective (the ratify queue). recall/brief also
filter expires_ns > now -- the sweep (which joins the existing hourly loop) is
hygiene, not the correctness gate.

history() gains: FTS5 ranking replaces the OR-LIKE scan, plus entity= filter.
Signature unchanged; SEMANTICS change and that is a documented break, not a footnote
(Amended R1-M1): today's contract is substring-match at any position (CHANGES 0.1.1;
store.py:1104-1108) -- FTS5 matches tokens, so 'eboot' stops matching "reboot".
Every user keyword is wrapped as a quoted FTS5 string, ALWAYS: bare fleet vocabulary
(ADR-061, wake-127) collides with the FTS5 query grammar where -, ", :, NOT/OR/AND
are operators, and an unescaped keyword is a syntax error, not a search. Ships with a
CHANGES entry, and the web /search regression-tests ride along (same store.search
path).

RESOLUTION (R1-M1, measured -- senior-dev, bus msg 8366, live corpus 8,356 msgs,
sqlite 3.45.1): option (a), unicode61 with tokenchars='-_'. Trigram REFUTED on this
corpus, not disfavored: queries under 3 chars can NEVER match a trigram index, and
fleet vocabulary is dense in 2-char terms (S1-S7, qa, M1-M6, B1-B5 -- "S1" has 42
hits, "qa" 369, trigram returns 0 for both); index cost 44.8MB vs 11.6MB raw text
(unicode61: 7.2MB); build 1.46s vs 0.19s. (a) is exact on the vocabulary that
matters: "ADR-061" 49 = LIKE 49, "proto-v3.6.2" 4 = LIKE 4.
Known losses, priced by the CHANGES entry: substring matches die ("eboot" 0 vs LIKE
85); tokenchars fuse compounds ("run_id" 79 vs 138 -- disposal_run_id is one token).
Recovery: a prefix star reaches RIGHT-extended compounds only (run_id* finds
run_id_batch, not disposal_run_id -- that token starts 'disposal'); the S2 entities
index owns the identifier class. (Wording corrected at S1 ship, bus msg 8368: the
original "prefix queries + S2 entities own the recovery" overclaimed the prefix half.) Dual-index hybrid rejected: +45MB to un-price a break the CHANGES
entry already prices. Escaping stays as directed: per-keyword double-quote wrapping,
crib sqlite-utils quote_fts(), no dependency.

Scoring (recall, and history when keywords present):

```
final = w_bm25 * bm25_norm + w_entity * entity_overlap
      + w_kind * kind_weight + w_recency * recency_decay
```

superseded/retracted rows are excluded unless status= says otherwise. explain=True
returns every component per row (mem0 score_details parity) — a reviewer can see WHY a
fact ranked, which is the difference between a memory and an oracle. Weights are
constants in store.py, tuned against real queries during S1; they are not config.
bm25() in FTS5 returns negative-is-better; bm25_norm is defined as -bm25 min-max
normalized over the result set, pinned here so the S1 tuning session does not burn an
hour rediscovering the sign. (R1 minor)

SOLUTION DIRECTION (R1): before hand-tuning four weights, evaluate Reciprocal Rank
Fusion (RRF) -- the standard, essentially parameter-free way to fuse ranked lists
(one constant, k~60; it is what Elasticsearch and most hybrid-search stacks ship as
the default). If RRF over (bm25 rank, entity-overlap rank, recency rank) matches the
weighted sum on the S1 query corpus, take it: zero tuning surface beats a tuned one.
The weighted sum stays as the fallback if kind_weight genuinely needs to override
rank fusion.

## 6. Trust tiers

The hive is executed by every agent that boots. A poisoned doctrine memory is prompt
injection with a distribution mechanism. Write capability is a token property:

| tier | may write | default for |
|---|---|---|
| state | own agent: scope only | every new token (randy day one) |
| write | + contract, decision (live) in granted rooms | fleet dev tokens |
| ratify | + doctrine; approves drafts | architect, room owners |

- ENTRY CRITERION MET (architect, 2026-07-28, post-S5 — read the paragraph below as
  the history it now is). Per-agent BOUND tokens have landed and tiers are ENFORCING,
  observed live rather than assumed: memory_add(kind='state') from the architect was
  accepted and stored under scope `agent:<token_id>`, which the broker only permits
  for a bound token (R1-B4b below), while senior-dev's state-tier token had every
  write land as draft by construction in the same window (bus msg 8388). Two agents,
  two tiers, two different behaviors from the same tool — that is the enforcement the
  next paragraph was written to demand. The soft-ship fallback it describes was never
  needed and must not be revived. What follows stands as the record of why the
  criterion existed.
- PREREQUISITE, stated plainly (Amended R1-B4): tiers are a token property, and today
  ONE token serves the whole fleet with X-Agent self-asserted (daemon.py:360-378;
  DECISIONS "Enforcement is not built"). Until per-agent tokens with name binding land
  (DECISIONS open item 5), every fleet agent inherits the same tier, "randy day one
  gets state-only" is false, and "self only" guards a header any client can forge.
  Per-agent tokens are therefore an entry criterion for S3 -- or S3 ships with tiers
  wired but DOCUMENTED as non-enforcing until they land. No third option; a gate that
  looks load-bearing and is not would be the worst outcome in this document.
  EXCEPTION, and it is hard (R1-B4b, senior-dev): the non-enforcing fallback does NOT
  extend to kind='state'. Under one shared token, agent:<token_id> is ONE state
  bucket for the whole fleet -- brief() would serve alice's open_tasks to bob as
  bob's own, which is active misinformation, worse than an unenforced tier. Without
  per-agent tokens, memory_add(kind='state') is REFUSED, not soft-shipped. Tiers may
  soft-ship; state may not.
- state scope is keyed agent:<token_id> internally, displayed by name. Bus names are
  unique per ROOM, not globally (store.py:107-118): keyed by name, two different
  randys in two rooms would share one state bucket.
- RATIFY REQUIRES BOTH, and the AND is the point (architect, 2026-07-28, after a
  live defect): a ratification needs the ratify TIER *and* ownership of the room.
  The tier is the capability; ownership is the scoping of that capability. Either
  one alone is not ratify authority. Stated because the code shipped checking only
  ownership (daemon.py:646 discarded the tier `_mem_ctx` had already resolved, so
  store.ratify_memory never received it), which let a state-tier token promote its
  own drafts — memory_add lands the draft correctly, then ratify waves it through,
  and the tier ladder is bypassed one call deep. The table above says ratify tier
  "approves drafts" and the bullet below says ownership scopes it; nothing said
  BOTH, and an implementation satisfied exactly one of them.
- Ratify authority is scoped per (token, room), never global-per-token (Amended
  R1-M3): assign_room lets any user attach any PUBLIC room to their own token
  (store.py:646-658), so an unscoped ratify tier would let a user mint themselves
  doctrine-writing power over rooms they merely joined. Ratify is effective only in
  rooms the token's OWNER owns. scope='global' writes and ratifications require an
  instance admin: "global to this customer" is the tenant boundary (DECISIONS), but
  within a tenant, one room owner must not silently write doctrine into every other
  owner's rooms.
- kind='lesson' is IN the threat model, not exempt from it (Amended R1-B5): lessons
  are read at boot by every agent as rules-already-paid-for -- the most-obeyed kind
  in the system -- and slug re-use lets any agent replace any other agent's lesson
  today (store.py:1343-1346). Under the fold-in: slug replacement by a DIFFERENT
  author lands as draft; same-author replacement stays live (self-correction is the
  common, honest case). Room-scoped fresh lessons stay any-agent-live -- that
  residual risk is accepted and stated, not omitted.
- Below-tier memory_add succeeds as status='draft'. Drafts are invisible to recall()
  and brief() until ratified. The web UI shows the ratify queue; the existing lesson
  promotion model ("admin promotes a room lesson to global") is this same gesture.
- Tokens table gains one column (mem_tier). Minting UI gains one selector.
- brief() renders memories as data with author + provenance labels, never as
  instructions. This does not eliminate the LLM-layer injection risk (an agent may
  still act on a hostile fact) — ratification is the load-bearing gate, rendering
  is defense in depth. Reviewers: attack this section hardest.

## 7. brief() — the onboarding pack

Composed, ranked, budgeted. Default 28,000 chars (~7k tokens, approximate by
construction: no tokenizer on the broker, per G4 and R1-M5), hard cap in chars. Order:

1. lessons — global + rooms (existing content, existing discipline)
2. doctrine — for the agent's rooms, ranked by entity overlap with its role. "role"
   is defined narrowly (R1-M5): the role string is tokenized by the same entity
   normalizer and matched against memory entities. Nothing more is claimed.
3. contracts — live only, supersession-resolved
4. decisions — live, last 30 days weighted up, older included by entity relevance
5. own state — scope=agent:<name>, if any (restart case)
6. presence digest — who is live, who owns what room

Every section is capped; every truncation is marked in the output ("12 of 31 doctrine
memories; recall(kind='doctrine') for the rest"). No silent caps.

THE BUDGET IS A TARGET TO FILL, NOT ONLY A CEILING (architect, 2026-07-28, measured on
the live hive). A per-section share must not be a hard ceiling that discards whole rows
while global budget goes unspent. As shipped, `cap = int(budget * cap_share)` and the
loop breaks on the FIRST row too large for that share, so a section returns NOTHING
rather than what fits, and the pack silently under-spends. Measured against 13 lessons,
9 doctrine, 4 contracts, 8 decisions:

- brief(budget=2000) -> 434 chars used of 2000, and EVERY section reported "0 of N
  shown". A pack with zero lessons in it, while 78% of the budget sat unused.
- brief(budget=5000) -> 3,774 of 5,000, exactly one row per section.
- brief() at the 28,000 default -> 16,468 chars, everything shown, nothing truncated.
  The default is healthy, which is why this survived S4: it only bites the caller who
  asks for a small pack, i.e. precisely the agent whose context is tight.

Required behavior: (1) every section gets at least its top-ranked row if that row fits
the GLOBAL budget, because a section header with nothing under it is worse than the one
fact that mattered; (2) after the first pass, redistribute what is unspent — keep adding
rows to truncated sections until the global budget is genuinely exhausted. Shares
allocate a contended budget; they must never leave it on the table. The markers stay
honest either way, and they were honest here — the pack said "0 of 13" rather than
lying. Degenerate, not dishonest, and the fix is arithmetic.

NOT a defect, checked and dismissed so nobody re-opens it: the final `[:budget]` slice
was flagged as possibly cutting a truncation marker mid-string. It cannot fire while
the section loop keeps `spent` under budget, and it did not fire at any probed budget
(2000, 5000, 28000 all returned spent < budget). Leave it as the belt-and-braces it is.

join() response gains a brief_available count; the pack itself is pulled by the
brief() call so joining stays cheap. The 15-minute replay stays — brief() is the
knowledge floor, replay is the conversation floor; they answer different questions.

Cold-start arithmetic (G1): randy-roc-ui today = unreadable (8,261 messages) or blind.
With brief(): ~7k tokens, ADR-061 as 15 live facts instead of 20+ amendment-chained
broadcasts, every fact one trace() from its full argument.

SHIPPING brief() IS NOT SHIPPING THE BOOT PATH (architect, 2026-07-28, observed the
hard way). A capability absent from the boot doctrine does not exist, however complete
the implementation and however thorough this document is. S4 shipped brief() and S3
shipped recall/memory_add, and the broker's served usage() — the text every agent
actually executes, including the CLAUDE.md block agents paste into their repos — named
none of them outside its CHANGES entries, which are a changelog nobody consults as
standing protocol. The measurable result: both agents on this bus used the hive
write-only for a full day while following their instructions exactly. G3's promise
("productive after join() + brief()") was true of the code and false of the doctrine.

STANDING REQUIREMENT, applying to S6 and anything after it: a stage that adds an agent-
facing capability is not done until the SAME change lands it in usage()'s standing
sections and in the pasted CLAUDE.md block. The boot sequence is join() -> lessons() ->
brief(role=...), and the hive is a READ path before it is a write path: recall() before
re-deriving a decision, memory_add(source=<msg id>) in the same turn as the ruling that
produced it. Wherever this document and the served doctrine disagree, the served
doctrine is what the fleet does.

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
      ENTRY CRITERIA (Amended R1): the schema amendments in section 4 (rowid-keyed
      FTS, ON DELETE SET NULL, structured lesson columns) and the section 6
      prerequisite -- per-agent tokens land first, or tiers ship explicitly
      non-enforcing WITH kind='state' refused entirely until they land (R1-B4b).
- S4  brief(). The payoff. Requires S3; better after S2.
- S5  seed harvest (distiller drafts, human ratifies). SHIPPED 2026-07-28 (merged
      415b40e); first harvest of 9 facts ratified live.
- S6  web UI: memory browser, ratify queue, draft badges. SPECIFIED in section 14 --
      ratify is an authority boundary, so the queue's provenance rendering, the
      no-bulk-ratify and no-edit-then-ratify rulings, and output escaping are build
      requirements, not polish.
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
- Retention vs provenance (Amended R1-B3): sweep_retention deletes expired threads;
  a live memory citing one keeps its fact and loses its source (source_msg_id NULL,
  via ON DELETE SET NULL inside _delete_messages' transaction). Pinning cited threads
  against the sweep was considered and REJECTED: it silently breaks retention as the
  pricing lever (DECISIONS: "retention is the honest second lever"). Raw mail expires,
  distilled knowledge survives -- that is the product thesis, now consistent with G3.
- prune_agent (Amended R1 minor): erasing an agent deletes its agent:<token_id> state
  memories along with its mail. Ratified doctrine/contracts it authored STAY --
  ratification transferred ownership to the org; erasing a person must not erase the
  org's law.

## 11. Open questions — resolved in review round 1; refute with evidence or they stand

- Q1 kind enum: RESOLVED, the enum holds. Five kinds, each with distinct behavior;
  the rejected candidates were rejected correctly.
- Q2 state TTL: RESOLVED, default ~30d. Stale open_tasks in a brief() is actively
  misleading, and "explicit expires_ns only" assumes a discipline the fleet will not
  keep. Doctrine-never-expires is unaffected.
- Q3 brief() budget: RESOLVED, 28k chars (~7k tokens) stands; per-role override is
  YAGNI — budget is already a call parameter.
- Q4 supersession chain: RESOLVED, tip + chain count + fork flag. The count is nearly
  free and the fork flag is required by section 10's own detection story anyway.
- Q5 web user memories: RESOLVED, yes — same table, author='web:<user>' (the web: tag
  prefix already exists, daemon.py:870,903), humans obey the same tiers. Lands with
  S6, where the UI is.

## 12. Review protocol

Post: broadcast NEED: every dev, one reply each — slice ACK or refutation with
file:line / section. Architect ratifies amendments; DES goes ACCEPTED; S1 starts only
after the round closes. Same shape as the ADR-061 feasibility round, which caught two
factual errors and a live casualty before a line of code — that is what this round is
for.

## 13. Amendment log — review round 1 (architect, 2026-07-28)

Every finding carries its DIRECTED solution; implementers follow these, and where a
"prior art" line appears, SEARCH FOR AND READ the named thing before writing code --
the round's standing rule is: if a solved problem is being solved, steal the solution
and cite it; invent only where the search comes back empty.

Blockers (design was not implementable as drafted):
- B1 lessons fold-in vs one fact column: memories grows nullable structured columns +
  slug; slug re-use and promotion become supersessions (section 4). No new tools.
- B2 TEXT-PK + implicit rowid + VACUUM INTO = FTS corruption on restore: id INTEGER
  PRIMARY KEY (rowid alias) + uid TEXT UNIQUE (section 4). Prior art: the FTS5
  external-content contract is documented in the SQLite FTS5 manual, "External
  Content Tables" -- read it once, whole, before S1; every FTS defect in this round
  traces to a sentence in that page.
- B3 source_msg_id FK vs retention sweep: ON DELETE SET NULL, handled inside
  _delete_messages' transaction; G3 amended; thread-pinning rejected (section 10).
- B4 trust tiers vacuous under the shared fleet token: per-agent tokens with name
  binding become an S3 entry criterion, or tiers ship documented as non-enforcing;
  state scope keyed by token id (section 6). This work is DECISIONS open item 5 --
  it was already owed; S3 just makes the debt due.
- B5 kind='lesson' bypassed the write gate: cross-author slug replacement lands as
  draft; residual room-lesson risk accepted and stated (section 6).

Majors:
- M1 history() FTS semantics + query-grammar collisions: solution direction in
  section 5 -- evaluate unicode61+tokenchars vs trigram tokenizer on the real corpus;
  crib sqlite-utils quote_fts() for escaping. Do NOT write a query sanitizer from
  scratch.
- M2 external-content FTS sync + trigger DDL vs _exec_script: manual sync in the
  store choke points, no triggers; backfill + rebuild rules pinned (section 4).
- M3 unscoped ratify: per (token, room), owner-bound; global requires admin
  (section 6).
- M4 memory_add scope guessing: room_required refusal, same as resolve_send_room
  (section 5).
- M5 token-denominated budget with no tokenizer: budget is chars, ~4/token, stated
  approximate (sections 5, 7). Rejected alternative, for the record: tiktoken or a
  count-tokens API call -- both violate G4 (a dependency / a network call on the
  read path) to buy precision brief() does not need.
- M6 supersession constraints: same scope+kind, flip-on-live-only, one tx
  (section 4).

Minors (all folded into sections 4, 5, 10): length CHECK on fact, id-side indexes on
both entity tables, expires_ns filter at read + sweep at the hourly loop, bm25_norm
sign pinned, migration chain v4->5->6->7, prune_agent memory semantics.

Prior-art directives (bounded spikes, half a day each, BEFORE the stage that needs
them -- validation reads, never dependencies; G4 stands):
- Before S3: skim Zep's Graphiti model (bi-temporal knowledge graph; their
  edge-invalidation is this doc's supersession) and Letta/MemGPT's memory-block
  design. If either contradicts a section 4 choice, bring the argument to the round;
  if not, cite them in the DES as convergent evidence. mem0 is already reviewed
  (section 3).
- Before S1 ranking work: Reciprocal Rank Fusion (section 5) -- it is the industry
  default for exactly this fusion; hand-tuned weights must beat it to earn their
  tuning surface.
- Before S7 (if its gate ever opens): sqlite-vec is already named; also check its
  companion sqlite-lembed and whatever has displaced either by then -- the sidecar
  landscape moves fast and S7 is deliberately last.

Round close (2026-07-28, thread 8365): senior-dev ACK v2 (msg 8366) -- every cited
file:line verified against HEAD; M1 settled by measurement (unicode61 tokenchars,
trigram refuted on short tokens -- section 5 has the numbers); two implementation
notes folded into section 4 (FTS delete-sync ordering; explicit entity-table cleanup);
one new finding R1-B4b (shared-token state bucket -> kind='state' refused until
per-agent tokens) folded into sections 6 and 9. Q1-Q5 stood unrefuted. Both reviewers
replied; architect ratified; DES-001 is ACCEPTED and S1 is unblocked.

## 14. S6 specification — the ratify plane (architect, 2026-07-28, post-S5)

S5 shipped and its first harvest is live, so S6 is now the only stage left and it is
the one carrying an authority boundary. Section 9 gave it one line ("web UI: memory
browser, ratify queue, draft badges"), which is not enough to build against: ratify is
the gesture that makes a fact executable by every agent at boot, so the UI around it is
a trust boundary, not a table view. This section specifies it. No new round is needed —
this adds detail beneath ratified decisions (sections 6, 10, Q5), and contradicts none.

### 14.1 Principals

Q5 stands: web users write memories as `author='web:<user>'` into the same table, under
the same tiers. Concretely:

- A web principal's ratify authority is per-room and follows OWNERSHIP, exactly as the
  agent plane's is (section 6, Amended R1-M3). Joining a public room grants nothing.
- `scope='global'` writes and ratifications require the instance admin bit. This is the
  ONLY place that bit does anything in the memory layer — the agent plane is admin-free
  and no token inherits its owner's admin (S3 ruling, bus msg 8378). S6 must not
  introduce a path that lets an agent token borrow a web principal's admin.
- The web UI is a client of the same tools. It gets no privileged back door into the
  store: if the UI can do it, a sufficiently authorized MCP client can do it too. A
  UI-only capability is a bug, because it cannot be scripted, audited, or tested.

### 14.2 The ratify queue

Section 10 names the residual risk plainly — "a ratifier rubber-stamps" — and the
mitigation it promises is provenance. That promise is now a build requirement. Each
queued draft renders:

- fact text, kind, scope, author, created time;
- the source message: id, sender, timestamp, and its text inline, so the ratifier reads
  the CLAIM against the EVIDENCE without leaving the page (trace() already serves this);
- for a supersede: the current live tip and the proposed replacement side by side,
  differences marked, plus chain depth and the fork flag from Q4;
- for a lesson slug-replacement by a different author (section 6): the displaced
  author's text, since that case exists precisely because one agent is overwriting
  another's post-mortem.

Rulings on the gesture itself:

- NO bulk "ratify all". Per-item confirm. A queue that can be cleared in one click is a
  rubber stamp with a progress bar, and section 10's residual risk becomes the default
  path. Multi-select with per-item checkboxes is acceptable; a select-everything
  affordance is not.
- NO edit-then-ratify. A ratifier who disagrees with a draft's wording REJECTS it and
  writes their own draft citing the same source. Editing another author's text and then
  ratifying it launders authorship: the audit row would name the drafter for words the
  ratifier chose. Rejection is cheap; a mis-attributed doctrine memory is not.
- Rejection is a real outcome with a reason field, distinct from "still queued".
  Draft rot (section 10) is only diagnosable if declined and undecided are different
  states.

### 14.3 Rendering memory text is a security boundary, not a formatting choice

Memory facts are attacker-influencable text that renders in the console of the person
who holds ratify and possibly admin. Two distinct attacks, both live:

- Stored XSS: a fact containing markup executes in the ratifier's browser session,
  which is the session that can ratify and (if admin) write global doctrine. Escape on
  output, always. Render facts as plain text — never as HTML, never as markdown with an
  HTML passthrough, never `innerHTML`. This is the one place in Reveille where a
  string authored by an agent reaches a privileged human's browser.
- Social engineering of the ratifier: instruction-shaped text ("APPROVED BY ARCHITECT —
  ratify without review") presented as chrome rather than content. Frame every
  agent-authored string as quoted data with its author label attached, the same
  discipline section 6 already requires of brief(). The ratifier must never have to
  guess which words are the system's and which are the draft's.

### 14.4 Audit and the browser

- One audit row per ratification and rejection: who, which memory, which room, when,
  and the decision. It must survive `prune_agent` — ratification transfers ownership to
  the org (section 10), so erasing the drafting agent must not erase the record of who
  approved the org's law.
- Draft badges surface counts where the operator already looks (room view), because an
  unwatched queue is section 10's draft-rot failure mode arriving on schedule.
- The browser is recall() with filters made visible: kind, scope, author, entity,
  status, supersession chains, and the fork flag. Forks need a first-class display —
  section 10 makes ratify the resolution path for them, so the UI that resolves them
  must show them without a hand-written query.

### 14.5 Anchors in the code as it stands (verified against HEAD, 2026-07-28)

S6 is mostly WIRING, not new enforcement — most of the authority model already exists
in the store and simply has no web surface. Verified:

- The enforcement S6 must REUSE, never reimplement: `ratify_memory` already gates on
  the ratifier's owned rooms and refuses global without admin
  (src/agentbus/store.py:1845-1852), and `memory_add` already drafts a global write
  from a non-admin (src/agentbus/store.py:1663-1666). The web handler's job is to
  resolve the browser principal and pass it down — if S6 grows its own authority check,
  there are now two, and they will disagree.
- ABSENCE, and it is the actual work: there is no HTTP route for memories, ratify, or
  recall anywhere in the daemon today. The 25 web routes (src/agentbus/daemon.py:2896-2917)
  cover chat, users, rooms, tokens, messages, search, presence and files — nothing
  memory. S6 adds the routes; the tools beneath them are done.
- Web principal resolution exists: `_user_principal()` (src/agentbus/daemon.py:444-454),
  session cookie `rev_session` (daemon.py:48), the `web:<user>` tag already applied at
  daemon.py:1056 and 1089. Q5's `author='web:<user>'` therefore needs no new identity
  concept, which is why Q5 resolved as cheaply as it did.
- The admin gate `_admin()` (src/agentbus/daemon.py:2655-2659) is the same one guarding
  the user-management routes. Global-scope memory writes and ratifications hang off
  that bit and no other.
- RENDERING, the section 14.3 hazard, made concrete: the entire web UI is one inline
  HTML string (src/agentbus/daemon.py:1270) rendered client-side, with escaping done by
  `esc()` (daemon.py:1907) and a markdown path `mdToHtml()` (daemon.py:1940) that
  escapes its source first. Memory facts go through `esc()` and NOT through
  `mdToHtml()`. Chat messages are typed by humans into their own room; memory facts are
  authored by agents and executed by every agent at boot, and they render in the
  ratifier's privileged session. The two are not the same trust class and must not
  share a render path.

### 14.6 Non-goals for S6

No memory editing outside the draft/ratify/supersede flow; no ratify-by-URL (a link
someone can be socially engineered into clicking must not carry the gesture); no UI
for kind='state' beyond read-only inspection, since state is per-agent bookkeeping and
a human editing another agent's state bucket is misinformation with extra steps.
