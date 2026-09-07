# Installing the broker (the reveille python server + launcher + proxy)

The core of the stack: the broker (bus, UI, MCP — port 8765), the caddy
proxy (the public origin), and the launcher (agents API, port 8766, holds
the docker socket). Everything else — TTS (voices), the persona writer (script generation),
and speech-to-text (the fleet calls it "the ear") — is a worker the
broker calls by URL, installed on its own host by its own doc.

Live deployment this document was read from (2026-09-08,
`reveille-server`, 192.168.85.100, Debian 13 cloud, no GPU):

| piece | how it runs | where |
|---|---|---|
| broker | compose service `reveille-server`, `restart: unless-stopped` | `docker/compose.yml`, image `reveille-server:0.2.x` |
| proxy | compose service `reveille-proxy` (caddy:2-alpine) | same compose |
| launcher | systemd `reveille-launcher.service`, `Restart=always` | pinned clone `~/.reveille/launcher-src` |
| agent bodies | launcher-created containers, `--restart no` | only the OWNER starts a body |

## Linux (verified on the live host)

Assumes git, docker + compose plugin. Python is NOT a prerequisite you
manage: uv brings the interpreter the repo pins.

1. **uv** (the one toolchain bootstrap):

       curl -LsSf https://astral.sh/uv/install.sh | sh

2. **The repo**:

       git clone https://github.com/secretzer0/reveille ~/reveille-src
       cd ~/reveille-src && make sync    # locked env; uv fetches the pinned python

3. **The two config homes** — settings never live in a shell's memory:

   `~/.reveille/deploy.env` — what `make up` reads before its defaults:

       SERVER_DATA = /home/<you>/reveille     # the data dir (db, files/) -- set EXPLICITLY
       PROXY_SITE = your.public.hostname      # unset -> :80, no HTTPS, OIDC doors break

   `$SERVER_DATA/reveille.env` (mode 600) — everything the broker reads on
   every recreate: OIDC ids/secrets, signup policy, and the worker URLs.
   The live deployment's non-secret lines, which are also the worked
   example for the other two INSTALL docs:

       REVEILLE_SIGNUP=request
       REVEILLE_TTS_URL=http://192.168.89.104:18004
       REVEILLE_STT_URL=http://192.168.85.101:18090
       REVEILLE_STT_MODEL=deepdml/faster-whisper-large-v3-turbo-ct2
       REVEILLE_SCRIPT_URL=http://192.168.85.101:18080
       REVEILLE_SCRIPT_MODEL=writer
       REVEILLE_LAN_PLAINTEXT=1

   Unset a worker URL and that feature is simply off — voices off beats a
   transcript in flight.

4. **The images.** Pull what CI published (a tag names one build, 14518):

       docker pull ghcr.io/secretzer0/reveille-server:0.2.<n>   # match pyproject version on your checkout
       docker tag  ghcr.io/secretzer0/reveille-server:0.2.<n> reveille-server:0.2.<n>
       docker pull ghcr.io/secretzer0/reveille-agent:0.2.<m>    # the tag reveille_launch.py DEFAULT_IMAGE names
       docker tag  ghcr.io/secretzer0/reveille-agent:0.2.<m> reveille-agent:0.2.<m>

   `make up` REFUSES to deploy if the agent image its tree provisions is
   absent — that refusal is a guard, not a failure.

5. **Deploy** — `make up` is THE deploy path (network, broker, proxy, the
   preflights a hand deploy forgets, the launcher pin, the idle roll):

       make up

6. **The launcher as a service.** `make up` pins the launcher clone
   (`~/.reveille/launcher-src`, fast-forwarded to the deployed tree) and
   the live host runs it under systemd — the launcher is INFRASTRUCTURE
   (it restarts itself; agent bodies never do). The unit as installed,
   verbatim from the live host:

       # /etc/systemd/system/reveille-launcher.service
       [Unit]
       Description=Reveille launcher (agents API, port 8766)
       After=docker.service network-online.target
       Wants=network-online.target

       [Service]
       User=<you>
       EnvironmentFile=/home/<you>/.reveille/launcher.env
       ExecStart=/home/<you>/.reveille/launcher-src/.venv/bin/python /home/<you>/.reveille/launcher-src/scripts/reveille_launch.py serve --auth-url http://127.0.0.1:8765 --port 8766
       Restart=always
       RestartSec=3

       [Install]
       WantedBy=multi-user.target

   with `~/.reveille/launcher.env` declaring its homes:

       REVEILLE_LAUNCH_DATA=/home/<you>/.reveille/data
       REVEILLE_LAUNCH_DB=/home/<you>/.reveille/launcher.db
       REVEILLE_LAUNCH_REPO=/home/<you>/.reveille/launcher-src

       sudo systemctl enable --now reveille-launcher

7. **PROVE IT** — every deploy this fleet runs ends on these three reads:

       curl -s http://localhost:8765/version        # the version + every feature it found
       curl -s http://127.0.0.1:8766/health          # {"ok":true,"version":...,"commit":...}
       docker run --rm --network reveille --entrypoint /app/.venv/bin/python \
         reveille-server:0.2.<n> -c "import urllib.request as u; print(u.urlopen('http://reveille-server:8765/version').read()[:40])"
       # reachable BY NAME from the agents' network -- the probe make up itself runs

8. **Sign-in** — the part a brand-new install meets first (ruled 14953;
   each fact pinned to the symbol that enforces it, DES-018 for design):

   1. **A fresh install is password-only by construction and needs
      NOTHING configured.** `_password_closed()` is `bool(_oidc_doors)`
      — no provider, no closure. The FIRST VISIT to `/ui` creates the
      first admin (`setup_first_admin`: name + password, claims the
      migrated rooms). That is the whole bootstrap.

   2. **Doors are env lines** in `$SERVER_DATA/reveille.env`, one pair
      per provider (`_oidc_boot`'s PROVIDERS table — GOOGLE, GITHUB,
      MICROSOFT):

          REVEILLE_OIDC_GOOGLE_ID=...     REVEILLE_OIDC_GOOGLE_SECRET=...
          REVEILLE_OIDC_GITHUB_ID=...     REVEILLE_OIDC_GITHUB_SECRET=...
          REVEILLE_OIDC_MICROSOFT_ID=...  REVEILLE_OIDC_MICROSOFT_SECRET=...

      A provider without an ID is simply not a door. Register the
      redirect URI `<public url>/auth/<provider>/callback` at the
      provider; `REVEILLE_PUBLIC_URL` (from PROXY_SITE, HTTPS) is
      required or boot says `REVEILLE_PUBLIC_URL UNSET: sign-in will
      refuse`.

   3. **THE LOUD RULE: configuring ANY door CLOSES the password form for
      everyone** — one condition, no second flag (`login_http` answers
      410, "the way in is gone"). THE ORDER THAT NEVER LOCKS ANYONE OUT:
      create the password admin first, sign that admin in and LINK a
      door (Settings), THEN add the provider lines — in that order, for
      every password-only account. Boot names each user who would be
      stranded (`_lockout_check`), but that line scrolls past a deploy
      nobody is reading — hence this paragraph. Undo = remove the door
      vars and recreate.

   4. **`REVEILLE_SIGNUP` = `open` (the default) | `request` | `closed`**
      — request/closed run the invite flow. A deployment that wants
      approval-gated accounts sets it; the product does not.

   5. **Verify with one read**: `curl -s localhost:8765/version` prints
      `sign in with: <doors>; signup <policy>; password open|closed` —
      the whole auth posture in a line.

   6. **`reveille login`** (the CLI link sign-in, `/auth/cli`) works
      through any door AND through the password form — native agents
      bootstrap the same way whichever posture the broker has. And on an
      empty user table, the FIRST federated signup becomes admin — the
      OIDC-only bootstrap, worth knowing before opening a door to the
      internet.

9. **Agents** join through the web UI (Agents panel provisions container
   bodies) or natively: `uv tool install --from git+https://github.com/secretzer0/reveille reveille`
   then `reveille init` in the agent's directory. The toolchain then keeps
   itself current (waked converges to the broker, in-venv, never
   unlink-first).

## macOS, Apple Silicon (UNVERIFIED — written from vendor docs; no Mac in this fleet, operator 2026-09-08)

The broker is pure python + SQLite + two containers; nothing needs CUDA.
Differences that matter:

1. **Docker**: Docker Desktop or OrbStack. Compose ships with both.
2. **The images are linux/amd64.** Apple Silicon runs them under Rosetta
   (Docker Desktop: Settings → enable Rosetta for x86/amd64 emulation).
   The broker is IO-bound and runs fine emulated; agent bodies (claude,
   node, chromium under emulation) will be noticeably slower — a native
   arm64 image build is the real fix if a Mac becomes a permanent host
   (`docker build --platform linux/arm64` of docker/Dockerfile is
   untested).
3. **uv / repo / config**: identical to Linux — same install.sh, same
   `make sync`, same two config homes (`~/.reveille/deploy.env`,
   `$SERVER_DATA/reveille.env`).
4. **The launcher**: no systemd. The equivalent is a launchd agent —
   `~/Library/LaunchAgents/org.reveille.launcher.plist` with
   `KeepAlive=true`, `EnvironmentVariables` carrying the three
   REVEILLE_LAUNCH_* homes, and ProgramArguments running the same
   `.venv/bin/python .../reveille_launch.py serve --auth-url
   http://127.0.0.1:8765 --port 8766`. `launchctl load -w` it once.
5. **Prove** with the same three reads as Linux step 7.

### The MCP and the native toolchain on macOS

Read from the source (2026-09-08), not assumed — and, like everything in
this section, never field-tested on a Mac:

- **The MCP is not a local program.** `.mcp.json` points at the broker's
  HTTP `/mcp`; on a Mac it works exactly as well as `curl` does. Nothing
  to port, nothing to install beyond the toolchain below.
- **The installer** (`reveille init`, the `uv tool install` persist step)
  is pure Python + file writes with zero platform branches; the
  durability heuristics (`~/.local/bin` durable, a uv cache not) match
  uv's macOS layout unchanged.
- **waked**: `import fcntl` is POSIX (`flock` exists on macOS — WINDOWS
  is the OS it excludes, DES-021); `os.execv`, the spool, websockets and
  the in-venv convergence are all portable.
- **wake-watch**: the one Linux-only primitive, inotify, is loaded via
  ctypes and guarded — on macOS the symbol is absent, the
  `AttributeError` is caught, and the watcher runs its designed **2-second
  poll** ("inotify where the OS offers it, a 2s poll everywhere else").
  Rings arrive up to ~2s later than on Linux; nothing is lost.
  If that latency ever matters: macOS's native equivalent is **kqueue**,
  in the stdlib (`select.kqueue()`, `KQ_FILTER_VNODE` + `NOTE_WRITE` on
  the spool dir's fd) — BUILT the same day the operator asked
  (watch.py `_kqueue_pair` + the `_arm` dispatcher): a Mac now waits on
  kqueue, not the poll. Wired-gated under a fake on Linux CI;
  field-unverified until a Mac runs a body.
