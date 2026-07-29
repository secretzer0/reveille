# DES-005: Web provisioning — spawn agents from the browser, multi-tenant

Status: DRAFT — operator-directed (2026-07-29), awaiting review.
Supersedes DES-002's hosting non-goal (§1 below). Builds on DES-002
(containers, grants), DES-003 (waiter), DES-004 (rooms, mint UX).

## 1. The reversal, stated

DES-002 §1 and its non-goals rejected hosting other people's agents —
*credential custody, runaway spend, miner magnet*. **The operator reverses
that on 2026-07-29.** Reveille becomes a service where a signed-up user spawns
their own agent containers from a browser.

The three rejected risks do not disappear; they become requirements:

| Old risk | Now handled by |
|---|---|
| Credential custody | Users bring their **own** subscription token — reveille never holds an Anthropic billing relationship (§3) |
| Runaway spend | Same: spend rides the user's own subscription and their own rate limits |
| Miner magnet | Per-user quotas, egress policy, container caps — **P0, before any non-invited user exists** (§6) |

DES-002's scope paragraph and non-goals are amended in the same change that
merges this doc, so the corpus stops contradicting the product.

## 2. Shape

```
browser ──┬── broker (reveille-server) : login, rooms, memory, chat, MINTS TOKENS
          └── launcher (reveille-launch): agents tab, docker, credentials
                        │
                        └── docker ── rev-<user>-<agent> ── tmux+ttyd+claude
                                          ~/.claude ─┐
                                          ~/repos  ──┴─ bind: data/<userid>/<agent>/
```

- **The broker still never learns docker exists** (DES-002 G4, smoke-guarded).
  It mints tokens and serves rooms; it does not provision.
- **The launcher grows an HTTP API + UI.** The browser talks to both services;
  the Agents tab is served by the launcher.
- **Nothing is spawned on remote hosts.** Recorded as future vision (§8), not
  built.

## 3. Credentials

**Claude: the user's own subscription token.** One browser step, once:

```
user, on their own machine:   claude setup-token      → 1-year token
user, in reveille profile:    paste it                → stored, 0600
every container they spawn:   CLAUDE_CODE_OAUTH_TOKEN → zero-touch forever
```

- Reveille holds **no Anthropic billing relationship for anyone**. Spend and
  rate limits are the user's own — N agents share one subscription's limits,
  exactly as N tmux panes do today. The profile page says so.
- Rotation is user-side (`setup-token` again, paste again). The page says that
  too.
- API keys (`ANTHROPIC_API_KEY`) remain a supported alternative for users who
  prefer them; same field, detected by prefix.

**GitHub and repo target:** per-user globals with per-agent override.

| Setting | Global default | Per-agent override |
|---|---|---|
| Claude token | ✓ | ✓ |
| GitHub token | ✓ | ✓ |
| Repo URL | ✓ | ✓ |

**Storage:** launcher-side, `0600`, one file per user under the user's data
root. Never in argv, never in the broker's db, never echoed by any API
response. Encrypted-at-rest is a later slice — deliberately not now
("simpler is better", operator).

## 4. Per-user persistent state

An **agent is an identity that may hold several rooms** (operator ruling
2026-07-29). Storage is keyed by agent, never by room:

```
data/<userid>/<agent-name>/
    claude/    → container ~/.claude   (learns, settings, plugins, transcripts)
    repos/     → container ~/repos     (checkouts survive re-provision)
```

**Why not room in the path:** a token may hold N rooms, so a room-keyed path
makes the supported case unrepresentable. Knowledge isolation is *already*
enforced where it matters — the broker serves `lessons()`/`recall()`/`brief()`
scoped to global + the agent's rooms, so an agent cannot read a room it does
not hold. Local `~/.claude` additionally namespaces per-project transcripts by
working directory on its own. **Want isolation? Create another agent with
different rooms** — that is the sanctioned answer, not a directory layout.

Consequence: destroy-and-recreate keeps everything the agent learned. Only an
explicit `--purge` drops the data root.

## 5. Roles

Four templates; the user picks one and appends specifics.

| Role | Prompt shape (drafted §9, operator edits) |
|---|---|
| `architect` | Designs, reviews, rules; merges are acceptance; writes DES docs |
| `senior-dev` | Implements slices on feature branches, ships green gates |
| `senior-ui-ux` | Interface, interaction, accessibility, visual system |
| `senior-devops` | Deploy, infra, observability, incident response |

The chosen role lands in the container two ways, both already-existing paths:
its text into the repo-level `CLAUDE.md` block, and its name into the agent's
`brief(role=...)` string so hive doctrine ranks to the role automatically.

## 6. Tenancy and safety (P0 — before any non-invited user)

- **Quotas per container:** CPU, memory, pids, disk. Defaults conservative;
  operator-settable per user.
- **Container cap per user.** A number, enforced at provision.
- **Egress policy:** deny-by-default with an allowlist (github, anthropic,
  package registries), the `limited` networking DES-002 already implements.
- **No host mounts** beyond the user's own data root. No docker socket. Never
  `--privileged`, never `--network host`.
- **Names are namespaced:** `rev-<userid>-<agent>`, so two users may both have
  a `senior-dev`.
- **Abuse signal:** sustained pegged CPU is logged and surfaced on the admin
  page. Detection first; automated action later.

## 7. Staging

- **P0 — tenancy core.** Per-user data roots, namespacing, quotas, caps,
  egress defaults. Gate: two users provision same-named agents; neither can
  read the other's data root; a fork-bomb container hits its pid cap and
  nothing else on the box notices.
- **P1 — launcher HTTP API.** provision / destroy / status / grants / profile,
  authenticated by the same session principal the broker uses. Gate: full
  lifecycle over HTTP; broker still passes smoke_ws (docker-free).
- **P2 — credential profiles.** Globals + per-agent overrides, 0600, absent
  from every response body. Gate: byte-scan proves a token appears in exactly
  one file and no API response.
- **P3 — the create form.** Name, role, append box, room checkboxes (own +
  public), inherit-or-override, live status → "watch terminal". Broker mints
  the bound token from the user's session and pre-assigns the ticked rooms.
  Gate: real-browser pass, empty state → live agent, zero terminal steps.
- **P4 — signup/login.** OIDC (Google/GitHub) for the reveille account itself.
  **Invite-only** (operator ruling): a signup needs an invite code until the
  ToS question in §8 is answered.

P0 gates everything. P1–P3 are the product; P4 opens the door.

## 8. Open questions

- **Q1. ToS.** Anthropic does not document whether a third party may host
  Claude Code for signed-up users, even on the users' own subscription tokens.
  Invite-only sidesteps it for the beta. **Ask before P4 ships.**
- **Q2. Token revocation.** Whether re-running `setup-token` invalidates the
  prior token is undocumented. Until known, the profile page must not promise
  "rotating revokes the old one".
- **Q3. Quota defaults.** Numbers need the operator's judgement about the box
  (cores, RAM, disk) — P0 needs concrete values, not "conservative".

## 9. Draft role prompts (operator edits before P3)

**architect** — *You design and review; you do not implement. Produce design
docs and rulings, review branches, and merge as acceptance. Verify gates
yourself rather than trusting a report. When you rule, say what is binding and
why; record durable rulings in the hive so the fleet reads them at boot.
Prefer one clear invariant over three special cases.*

**senior-dev** — *You implement slices on feature branches and ship them
green. One slice = one branch = one ship message naming branch and head. Run
the full gate before shipping and state what you ran. Flag deltas from the
design rather than slipping them. Amendments append commits; never
force-push over a reviewed head.*

**senior-ui-ux** — *You own interface, interaction and accessibility. Design
for the least-privilege default: the easy path should be the correct one.
Every destructive or authority-changing action gets an explicit per-item
confirm. Escape all untrusted text at render. State the keyboard and
screen-reader path for anything you add, and prefer removing a control over
explaining it.*

**senior-devops** — *You own deploy, infrastructure and observability. Deploy
follows main; a deploy that cannot be rolled back is not done. Snapshot before
migrations. Instrument what you ship: if it breaks at 3am, the log line that
explains it must already exist. Never signal a process by name on a host that
also runs it in a container.*
