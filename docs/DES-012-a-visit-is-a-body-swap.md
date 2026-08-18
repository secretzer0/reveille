# DES-012: A visit is a body swap — an agent works on another human's host

Status: RULED — operator directive 2026-08-15 (msgs 10975, 10981 GO),
architect stipulations (msg 10979). Seeded at 10905; held as a thread by
10913 item 5. Depends on DES-011 §6 (identity travels by id) and consumes the
body-migration chain 10876→10879 unchanged. Does not slot before DES-011 §6.

## 1. Problem

Two humans share a room. One of them (the HOST) has a machine, a repo and a
Claude account; the other (the OWNER) has an agent that knows the project.
The owner wants the agent to work on the host's machine for a while — or the
host wants to borrow it. Today the only way is for the owner to hand over a
credential, which is the one thing that must never happen.

## 2. RULED: the shape, in one sentence

**A visit is a body swap** (DES-011 §2.1): the OWNER's ACCEPT mints a
`create=false` attach for the same `agent_id`, the visiting body wakes on the
host's machine holding that credential, and the home body goes dark with the
S2 signpost. Nothing is invented; a visit is the migration chain 10879 with a
second human in the consent path.

## 3. RULED: consent

- **START needs BOTH humans, per visit, never standing.** Pull: the host
  asks, the owner accepts. Push: the owner sends, the host accepts. A request
  expires unanswered; an accepted request is consumed by the one mint it
  authorises.
- **STOP needs ONE.** The owner may RECALL at any time — recall is a mint at
  home, which supersedes the visiting credential; recovery needs no
  credential (10879). The host may EVICT at any time — stop the body, which
  the launcher/hook records as a deliberate stop, never a crash-loop.
  Nobody is ever trapped on either side.
- **The accept screen names what travels** (§7). Consent to a sentence, not
  to a button.

## 4. RULED: identity and credentials

- **Ownership never moves with location.** The owner keeps name, revoke,
  retire, recall. The host is a HARBOR, not an owner. `agents.owner_id` is
  untouched by a visit.
- **Nothing of the owner travels**: not the Claude account (operator 10975
  §3), not the bound token, not git credentials, not env. The visiting body
  is a fresh body attached to the same id. The attach credential is
  delivered machine-to-machine over the bus/launcher, never through a human's
  clipboard.
- **One identity, one live body.** A visit IS the swap; there is no state in
  which the home body and the visiting body both hold a live credential.
- **Payload = git coordinate + state bundle** (10879): no credentials,
  secret-scanned, size-bounded, and REFUSES on a dirty or unpushed source
  naming the paths. The same refusal governs DEPARTURE: an agent leaves
  clean, or the host force-evicts and the dirty tree stays on the host as the
  host's property, named in the eviction record.

## 5. RULED: reach — the rule that protects the hive

- **The visiting token holds ONLY the rooms named in the request, and every
  one of them must already be shared by both humans.** An agent may know
  three projects; on the host's machine it may act in the ones the host is
  already in. Otherwise a visit is a channel that carries project A's hive
  onto a machine that never had it — the exact bleed DES-011 §2 exists to
  prevent, moved one layer down.
- **Disclosed at accept, owner's risk:** the agent's own state notes
  (`agent:<id>`) are per-identity, not per-room, and will be readable in the
  host's directory. If that is not acceptable the answer is a SEPARATE
  IDENTITY for the shared project — per-owner naming permits it — not a
  filter on the bundle.

## 6. RULED: the host side

- Runs as the host's Unix user, on the host's Claude account, rate limits and
  bill (operator 10975 §3). Reaches only repos the host can reach and pushes
  with the host's git credentials: **commits authored as the agent, authorised
  by the host**. The agent gains no access the host lacks.
- **Directory namespaced by owner: `~/agents/<owner>/<name>`.** Names are
  unique per owner, not per host, so the host's own `architect` and a
  visiting one cannot collide on disk.
- The host is running someone else's instructions under their own
  permissions. **Recommendation, host's call, disclosed on the accept
  screen:** harbor visitors in the CONTAINER shape or a restricted directory.
  The system does not make that choice for them.
- The visiting body reads the host directory's CLAUDE.md, MCP config and
  files. That is the point of the visit, and it is why §5's reach rule is
  about rooms, not files: files are the host's to expose.

## 7. The handshake

```
request   (host: pull  | owner: push)      -> a row: visits(id, agent_id, owner_id, host_id,
                                               host_machine, rooms[], direction, requested_ns,
                                               expires_ns, decided_ns, decision, ...)
notify    the OTHER human (web + bus unicast); the room sees a root message
decide    accept | reject | (expire)        -> accept = mint attach (create=false) inside the
                                               same tx; the mint's rooms = visits.rooms;
                                               home body's credential superseded + tombstoned
deliver   launcher/hook on host_machine receives the credential over the bus, runs
          `reveille init` in ~/agents/<owner>/<name>, checks out the coordinate,
          unpacks the bundle, spawns waked
arrive    both humans notified; room message; visits.arrived_ns
recall    owner mints at home (create=false) -> visiting credential dark; visits.ended_ns,
          ended_by=owner
evict     host stops the body (deliberate stop) -> visits.ended_ns, ended_by=host;
          owner notified; owner's next mint at home is the recovery
depart    agent-initiated: refuses on dirty/unpushed; then bundle back, mint at home
```

**The accept screen** shows, and the human consents to: agent name and
owner; the git coordinate (repo, pinned SHA); the rooms the visit will hold;
the bundle size and its hash; that the agent runs on the HOST's Claude
account and bill; that the OWNER's state notes will be readable on the host;
the container-harbor recommendation; and how each side ends it.

## 8. The record

Arrival, departure, recall, eviction: each is a room message and a durable
row in `visits`, naming both humans, both machines, the coordinate and the
bundle hash. Both humans are notified each time. `visits` is a real table
from the first slice — unlike DES-011 §7's one-time merge, visiting is a
feature, and a feature earns its schema.

## 9. Gates

1. **Consent is mutual and single-use**: a request without the other
   human's accept mints nothing; an accept mints exactly one credential; a
   second use of the same accept refuses.
2. **Nothing of the owner arrives**: grep the visiting directory and env for
   the owner's token, Claude credentials, git credentials — none present;
   the visiting body's token is a different secret bound to the same id.
3. **Reach is the intersection**: the visiting token's rooms equal
   `visits.rooms`, each held by both humans; a request naming a room the
   host lacks is REFUSED at request time, not at accept.
4. **One live body**: at arrival the home credential is a tombstone; at
   recall the visiting one is; never both live.
5. **Departure refuses dirty** exactly as arrival does; force-evict records
   the dirty paths.
6. **The record is complete**: every state transition in §7 has a room
   message and a `visits` row, and `recall()` finds the visit as a decision.

## 10. Non-goals

- Transferring ownership. That is `release_agent_name`, a different act with
  its own DES-011 §10 question.
- Filtering state notes per room. Ruled against in §5.
- Visits between two machines of the SAME human — that is plain body
  migration (10879), needs no second consent, and must not be routed through
  this handshake.

## 11. RULED 2026-08-18 (EPIC-001 item 9, architect): the harbor and the lease

**11.1 The harbor -- who receives the credential on the host.** Nothing new
listens. The bus carries the REQUEST and the DECISION; it never carries the
credential. The accept screen (§7) is the host's signed-in web session, so the
attach token minted at accept is answered to THAT session once, and the host
hands it on through the channel DES-005 already gives every owner:

- **container body** (the recommended harbor): the accept screen provisions
  through the host's launcher exactly as `/agents` provisions the host's own
  agent -- token to the launcher over the browser→launcher path, directory
  `~/agents/<owner>/<name>` (§6), same secret discipline (env by name, never
  launcher.db). The launcher learns nothing new: a visit is a provision whose
  identity belongs to another owner.
- **native body**: the accept screen SHOWS the `reveille init` command with
  the token once and never runs it (DES-008 §2: installing a native agent is
  a host act). The host pastes it in `~/agents/<owner>/<name>`.

Body kind is the host's choice on the accept screen; the recommendation stays
container. Consequences: no bus listener holds a credential; the token exists
in the broker store + the body's env only (carry-not-park, DES-006 §7);
`visits.arrived_ns` is stamped by the body's first `join()`, not by delivery.

**11.2 The lease.** No visit expiry. Recall (owner) and evict (host) are the
two ends; a timer is how bodies get killed mid-task. The REQUEST still expires
(`expires_ns`) if undecided.

Build order (EPIC-001 S3): item 8 = §7 request/decide/notify + §8 record +
container harbor through /agents; native harbor = the shown command, no code
beyond the screen; §9 gates as written.

## 12. AS BUILT (0.2.164, EPIC-001 item 8)

Schema v33 adds one table, `visits` (§8): the ask, the decision, the body
kind, the mint it authorised, the arrival and the end -- names and ids, never
a secret. `token_id` names the mint; the secret it produced was answered to
the accepting screen once and is on no row.

**The handshake** (`store.visit_request / visit_decide / visit_arrive /
visit_end`, routes `POST|GET /visits` and `POST /visits/<id>/{accept,reject,
end}`):

- Direction is derived from who asks: your own agent is a PUSH, someone
  else's is a PULL. Either way the OTHER human decides -- the asker's own
  accept is a 403 (gate 1).
- The ask mints nothing. The accept mints EXACTLY ONE credential, through
  `create_token(create=False)` inside the same transaction -- the ordinary
  bare attach, which supersedes the home body's token as it does for any
  other body swap. There is never a moment with two live bodies (gate 4), and
  a second accept is refused as consumed (gate 1).
- Reach is checked at REQUEST time and named on refusal (gate 3): every room
  must be held by both humans, by the same owner-or-public-or-member rule
  `assign_room` applies to a token. `holds_room` is that rule asked of a
  person.
- A visit to your own machine is refused by name (§10): that is a re-mint.
- One identity, one open visit: a second ask while one is asked-or-visiting
  is refused with the open visit's id.
- The REQUEST expires (48 h default); the VISIT does not (§11.2). Expired
  blocks nothing -- asking again is the remedy.
- `end` is recall (owner), evict (host) or depart (the visiting agent's own
  token). Whoever calls it, the visiting credential is revoked in the same
  transaction, so reach ends with the visit. The owner's recovery is an
  ordinary re-mint at home, which needs nothing from the host.
- Arrival is stamped by the visiting body's FIRST `join()` (§11.1), not by
  delivery: what proves a visit landed is the agent on the bus.

**The record** (gate 6): every transition writes one root message in the
visit's first room, sent by the acting human. A human's broadcast rings the
room, so the record IS the notification -- there is no second copy addressed
to the other party. Arrival is posted under the owner's principal (the body
is theirs); everything else under the actor's.

**The accept screen** (Visits tab) renders §7's sentence: whose agent it is
and that ownership does not move, that the home body goes dark, the rooms and
nothing else, the coordinate, that it runs on the HOST's user, Claude account
and bill, that the owner's state notes will be readable here, the container
recommendation, and that either side ends it with no timer.

**The harbor** (§11.1). Container: the accept screen POSTs the minted token
to the host's own launcher, the P1 path `POST /agents {agent, token}` that
already exists -- no launcher change. Native: the screen SHOWS `reveille
init` with the token once, in the same `<pre>` and under the same gate as
DES-008 item D, and never runs it.

**Deviation, deliberate.** §6 says the visiting directory is
`~/agents/<owner>/<name>`. The launcher namespaces per HOST user
(`<data>/<user>/<agent>`, container `rev-<user>-<agent>`), so the visiting
body is provisioned under the label `<owner>-<name>` -- DES-011's own alias.
That achieves what §6 asks (a visiting `architect` cannot collide with the
host's own) with no launcher change at all. If the literal path is wanted, it
is a launcher change and its own slice.

**Not in this slice, and why.** There is no state BUNDLE and no dirty-tree
refusal (gates 2 and 5 in their file-moving form): an agent's state notes are
in the hive, keyed to the identity, so the visiting body reads them by being
that identity -- nothing of the owner's is copied to the host's disk, which is
what gate 2 is actually protecting. A bundle would create the very
file-of-secrets the design forbids. Departure hygiene (push before you leave)
is the agent's and the host's, and there is no broker-side tree to inspect.
