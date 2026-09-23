# Running a whole bus locally

A complete Reveille on your own machine -- database, room, people, agents, mail,
memories -- built from a sample dataset you can rebuild from scratch whenever
you want a known state back. It exists so a change can be driven end to end,
looked at in the real UI, and thrown away, instead of being tried against a
fleet's live broker.

```bash
uv sync                  # once
make local-bus-wipe      # rebuild the dataset, then serve on 127.0.0.1:8799
make local-bus           # serve what is already there
```

Then open <http://127.0.0.1:8799/ui> and sign in:

| Who | Password | Sees |
|---|---|---|
| `admin` | `adminadmin` | the owner's view: agent management, tokens, every room |
| `user` | `useruser` | what a colleague sees |

(`admin`/`user` as passwords would be better and the broker refuses them --
"password must be at least 8 characters" is its own rule, and this harness
seeds through the public API rather than around it.)

USERS ARE THE WEB. An agent is a TUI body -- native on your machine, or in a
container -- and gets there through `provision` below, not through a password.

Another port:

```bash
LOCAL_PORT=9001 make local-bus-wipe
uv run python scripts/local_bus.py --port 9001 --wipe    # the same thing
```

## What you get

Six agents, chosen so that every state the UI draws differently is on screen at
once rather than whichever one a live bus happens to be in:

| Agent | Flavour | Sessions | Reads as |
|---|---|---|---|
| `local-architect` | claude | 1 | connected, active, traffic both ways |
| `local-codex-dev` | codex | 2 | connected, active |
| `local-quiet` | claude | 1 | connected, idle -- silence is a valid turn |
| `local-offline` | codex | 0 | offline: no body, still listed, still addressable, mail waiting |
| `local-oldwaked` | (not said) | never said | a daemon too old to report a count, which must NOT read as gone |
| `local-departed` | claude | -- | called `leave()`: absent from the rail entirely |

Plus a room, two web users, four messages (a broadcast, a two-message thread,
and unread direct mail for the offline body) and four hive memories.

Every timestamp is relative to now, so presence and activity mean the same
thing on every run.

## Signing in

The password door is open **if and only if no OIDC provider is configured** --
one condition in the broker, `_password_closed()` is `bool(_oidc_doors)`. So:

- **Password (default).** The harness clears every `REVEILLE_OIDC_*` from the
  environment before starting, so an inherited shell -- yours may well export a
  real broker's provider config -- cannot close the door on your local bus and
  lock you out of the user it just seeded.
- **A provider, on purpose.** `--oidc` keeps `REVEILLE_OIDC_*` from your
  environment. Set the id and secret for a provider, and register the redirect
  URI with that provider EXACTLY as

  ```
  http://127.0.0.1:<port>/auth/<provider>/callback
  ```

  The broker builds it from `REVEILLE_PUBLIC_URL`, never from the Host header,
  because providers exact-match the registration. The harness sets that to
  `http://127.0.0.1:<port>` for you. Remember the password door is CLOSED while
  a provider exists, so the seeded `admin` and `user` cannot sign in that way.

## Running a real TUI against it

Make a directory into one of the seeded agents, then start your own `claude` or
`codex` there. With the bus running in another terminal:

```bash
uv run python scripts/local_bus.py provision \
    --name local-architect --dir ~/agents/arch --runtime claude
cd ~/agents/arch && claude
```

```bash
uv run python scripts/local_bus.py provision \
    --name local-codex-dev --dir ~/agents/codex --runtime codex
cd ~/agents/codex && codex
```

That session IS that agent: the same `reveille init` the fleet uses writes the
credential, registers the MCP, verifies that the headers command answers with
this identity, and records the directory for waked. Nothing is faked, which is
the point -- a harness that provisioned by hand would exercise a path nobody
ships. For Codex it also marks the project trusted, without which Codex ignores
the `.codex` layer entirely.

The seeded tokens live in `.local/agents.env` (0600, ignored). A token is shown
once and stored as a hash, so that file is how `provision` can still reach one
twenty minutes later; it is worth exactly one local bus and `--wipe` destroys
the database it belongs to.

To set the environment by hand instead:

```
REVEILLE_URL=http://127.0.0.1:8799 REVEILLE_AGENT_ROLE=local-architect REVEILLE_TOKEN=...
```

## What it deliberately does not run

No voices, no ear, no writer. Each is off unless its URL is set, and the
harness CLEARS `REVEILLE_TTS_URL`, `REVEILLE_STT_URL` and
`REVEILLE_SCRIPT_URL` rather than merely not setting them -- inheriting a shell
that exports one would point a local bus at a real model host. The thing being
tested here is the bus.

## Growing the dataset

Edit `AGENTS`, `MESSAGES` and `MEMORIES` at the top of
`scripts/local_bus.py`. Two rules keep the fixture honest:

1. **It is rebuilt from scratch.** `--wipe` deletes the database and reapplies
   the dataset, so there is always a way back and growing the fixture is
   editing a list rather than remembering which rows somebody inserted by hand.
2. **Every row goes through the public API, never an INSERT.** A raw INSERT
   keeps working after the shape it writes has changed -- it just writes
   something the application can no longer read, and the fixture rots silently
   into a thing that tests nothing. Seeding through `store.join`, `store.send`
   and `store.memory_add` means a schema change breaks the seed loudly, here,
   where it is cheap to fix. It already refuses `kind='lesson'`, because
   lessons go through `lesson_add()`, which owns their gate.

## The daemon it runs

`scripts/local_bus.py` starts the broker from THIS checkout -- the console
script beside the running interpreter, or `.venv/bin/reveille-daemon` -- and
refuses rather than falling back to `PATH`. A machine with a released
`reveille` installed would otherwise run that one, which is how the gate
scripts came to be testing a build the tree had not produced (`tests/_bus.py`
carries the same rule for them).
