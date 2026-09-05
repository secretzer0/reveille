REPO := $(abspath .)
PREFIX ?= $(HOME)/.local/bin
LOG  := $(REPO)/reveille.log
PID  := $(REPO)/reveille.pid

# Tag-per-image-change (architect ruling, msg 8433): any Dockerfile change bumps
# this tag in the same commit -- a fixed tag over drifting content makes
# launcher.db image records ambiguous.
AGENT_IMAGE ?= reveille-agent:0.2.36

.PHONY: help sync build test smoke daemon start stop restart status logs register unregister lint clean agent-image agent-container agent-spike server-image tts-image up down branch-orphans shots ui-drive

help:
	@echo "make sync           create/refresh the uv env (Python 3.14, locked)"
	@echo "make test           unit suite (uv run pytest)"
	@echo "make smoke          end-to-end smoke: real daemon, HTTP-MCP + WS wake + auth"
	@echo "make shots          phone-layout pictures + the width gate (scripts/mobile-shots)"
	@echo "make ui-drive       drive the page in a real browser -> shots/ui + the checks"
	@echo "make build          sync + test + smoke"
	@echo "make daemon         run the broker in the FOREGROUND (Ctrl-C to stop)"
	@echo "make start          start the broker in the background -> reveille.log. Env: REVEILLE_PORT, REVEILLE_DB"
	@echo "make stop           stop the background broker"
	@echo "make restart        stop + start"
	@echo "make status         is the background broker running?"
	@echo "make logs           tail -f reveille.log"
	@echo "make register [URL=] register reveille once (user scope); identity = per-session \$$REVEILLE_AGENT_ROLE"
	@echo "make unregister      remove the reveille MCP registration"
	@echo "make lint           ruff check"
	@echo "make agent-image    build the agent container image ($(AGENT_IMAGE))"
	@echo "make tts-image     build the DES-009 voice synthesizer image ($(TTS_IMAGE)) -- NOT part of make up"
	@echo "make branch-orphans commits that live on a branch and nowhere else"
	@echo "make agent-container ROLE=<name> [WORK=<dir>] [URL=]  run one agent in a container"
	@echo "make agent-spike    prove a container keeps its knowledge: join from inside it"

sync:
	uv sync

# build == prove it works: locked env, unit suite, real HTTP+WS smoke.
build: sync test smoke

test:
	uv run pytest -q

smoke:
	uv run python tests/smoke_ws.py

# THE INSTRUMENT IS PART OF THE WORK (lesson a-harness-that-lived-in-a-session-
# dies-with-it): both harnesses drive a scratch broker in headless chromium and
# EXIT NON-ZERO on a failed check, so they are gates that also leave pictures.
# They have a make target because a script nobody can name is a script nobody
# runs -- and mobile-shots, committed but targetless, had been dead since
# store.send stopped taking bare names, which no gate noticed.
# Chromium comes from ~/.cache/ms-playwright; if it is missing:
#   uv run --with playwright playwright install chromium
shots:
	scripts/mobile-shots $(OUT)

ui-drive:
	scripts/ui-drive $(OUT)

# The broker daemon. One process on an always-on host serves every agent (local at
# 127.0.0.1, remote at the LAN name) over the same SQLite -> one bus. Set
# REVEILLE_PORT to change the port (default 8765); REVEILLE_DB to move the database.
# Auth is no longer an env var: users and tokens live in the database (see /ui).
daemon:
	uv run reveille-daemon

# Background lifecycle. Logs go to reveille.log next to this Makefile; PID in
# reveille.pid. Pass env through make, e.g.:  REVEILLE_PORT=9000 make start
start: sync
	@if [ -f "$(PID)" ] && kill -0 `cat "$(PID)"` 2>/dev/null; then \
	  echo "reveille already running (pid `cat $(PID)`)"; \
	else \
	  nohup "$(REPO)/.venv/bin/reveille-daemon" >> "$(LOG)" 2>&1 & echo $$! > "$(PID)"; \
	  sleep 1; \
	  if kill -0 `cat "$(PID)"` 2>/dev/null; then \
	    echo "reveille started (pid `cat $(PID)`) -> $(LOG)"; \
	  else \
	    echo "reveille FAILED to start -- last log lines:"; tail -3 "$(LOG)"; rm -f "$(PID)"; exit 1; \
	  fi; \
	fi

stop:
	@if [ -f "$(PID)" ] && kill -0 `cat "$(PID)"` 2>/dev/null; then \
	  p=`cat "$(PID)"`; kill $$p 2>/dev/null; \
	  for i in 1 2 3 4 5 6; do kill -0 $$p 2>/dev/null || break; sleep 0.5; done; \
	  kill -9 $$p 2>/dev/null || true; \
	  echo "reveille stopped (pid $$p)"; \
	else \
	  echo "no live pid; clearing any stray daemon"; pkill -f "$(REPO)/.venv/bin/reveille-daemon" 2>/dev/null || true; \
	fi; \
	rm -f "$(PID)"

restart: stop start

status:
	@if [ -f "$(PID)" ] && kill -0 `cat "$(PID)"` 2>/dev/null; then \
	  echo "running (pid `cat $(PID)`) -> $(LOG)"; else echo "stopped"; fi

logs:
	@touch "$(LOG)"; tail -n 80 -f "$(LOG)"

# Register the daemon ONCE per machine (user scope). Identity is NOT baked in here --
# the X-Agent header and the bearer token are ${VAR} templates that Claude Code expands
# per session from that session's own env. THE DIRECTORY IS THE AGENT: `reveille init`
# in a project directory writes the identity into its .claude/settings.local.json env
# block, so plain `claude` there carries it -- one registration serves every agent dir.
# URL is 127.0.0.1 on the daemon host, the LAN name elsewhere (override: URL=...).
register:
	-claude mcp remove reveille --scope user 2>/dev/null
	claude mcp add --transport http --scope user reveille "$(or $(URL),http://127.0.0.1:8765)/mcp" \
	  --header 'Authorization: Bearer $${REVEILLE_TOKEN:-}' \
	  --header 'X-Agent: $${REVEILLE_AGENT_ROLE:-unset-agent}'
	uv run python -m reveille.install
	@echo "registered. per agent directory: run 'reveille init' there, then plain 'claude'."

unregister:
	claude mcp remove reveille --scope user || true

# ---- the standalone server --------------------------------------------------------
# ---- the platform, declared (docker/compose.yml) -----------------------------------
# `make up` is THE deploy path: network + broker + proxy, with the two refusals a
# hand deploy cannot be trusted to remember. The launcher is NOT here -- it stays a
# host process (pinned clone + Stop hook); agent containers are launcher-created and
# join the same network.
SERVER_IMAGE ?= reveille-server:$(shell grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
# DES-009 section 4.1: devnen/Chatterbox-TTS-Server built from THEIR
# Dockerfile.cu128 at the SHA docker/tts.upstream pins -- their resolution, our
# provenance. Pinned to OUR FORK (secretzer0/Chatterbox-TTS-Server) while the
# fixes it carries are open upstream: upstream main plus (1) an inference_mode
# guard around generate (upstream PR #164; without it every distinct voice
# pins ~200 MB of autograd graph in their conds cache and a 12 GB card OOMs
# after about 40 voices), (2) divider lines split chunks (upstream PR #156;
# without it "---" glues a whole message into one chunk), and (3) upstream
# PR #161's batched turbo decode (with #160 it builds on; guarded with
# inference_mode on the fork) made pad-aware on the fork (fc464a8): the
# packaged inference_turbo reads right-padded EOS as text, so short rows in a
# mixed-length batch babbled 2-3x past their words; the fork's decode passes
# the tokenizer mask and per-row position ids through, gated by
# scripts/babble_proof.py there; and (4) the stream path's FIRST chunk is cut
# short and synthesized alone (d534a39, TTS_FIRST_CHUNK_CHARS default 60),
# then the rest batched -- the Web Audio player starts on that first chunk,
# which is what makes send-to-first-sound a function of the chunk and not of
# the message. See the TTS_BATCH_SIZE note in docker/compose.yml. Move the pin
# back to upstream the day they merge. THE MODEL LIBRARY (chatterbox-v2)
# FLOATS ON PURPOSE (operator 11076): newer models arrive on the next cold
# build; the cost is that (3) reaches into its T3, so every rebuilt tag is
# vetted with the fork's babble_proof on the GPU host BEFORE compose points at
# it. Not the repo version: the broker's identity does not depend on it and it
# must not be rebuilt on every bump. Move the pin, bump this tag
# (image-pin-check enforces the pair).
# THIS BOX'S OWN DEPLOY SETTINGS, IF IT HAS ANY (operator, 2026-08-18: "these
# are not persisted in an env or conf file"). Every `make up` had to carry
# SERVER_DATA and PROXY_SITE by hand, and the defaults below are not harmless
# if one is forgotten: PROXY_SITE falls back to :80, which means no hostname,
# no automatic HTTPS, and an EMPTY public origin -- so the OIDC redirect URI
# stops matching what the providers were registered with and the session cookie
# loses its __Host- prefix. That is the same failure family as the upstreams
# that lived only in a shell (0.2.167), one layer up, and it deserves the same
# answer: a file the box keeps.
#
# Included BEFORE the defaults so its values win over them; `make VAR=x up` and
# a scratch invocation still win over the file. Absent file = today's defaults,
# unchanged, so a fresh clone behaves exactly as before.
DEPLOY_CONF ?= $(HOME)/.reveille/deploy.env
-include $(DEPLOY_CONF)

TTS_IMAGE ?= reveille-tts:0.2.5
TTS_UPSTREAM := $(shell cat docker/tts.upstream)
SERVER_DATA  ?= $(HOME)/reveille
# The shared docker network agent containers live on. THE BROKER MUST BE ON IT: an
# agent reaches the bus at http://reveille-server:8765, and container names only
# resolve on a user-defined network -- the default bridge has no DNS. Must match
# reveille_launch.py's DEFAULT_NETWORK.
SERVER_NETWORK ?= reveille
# Where the PROXY mounts the launcher, as the browser sees it. The broker uses it
# for two things and nothing else: whether to render the Agents control at all,
# and the prefix its fetches carry. Empty = no launcher in this deployment, and
# the bus renders exactly as it did before agent management existed.
AGENTS_PATH ?= /agents
PROXY_IMAGE ?= caddy:2-alpine
PROXY_PORT  ?= 80
# Full Caddy site address for the proxy. The default keeps the historical shape
# (plain HTTP on PROXY_PORT); setting a hostname (PROXY_SITE=reveille.mythos.org)
# turns on Caddy's automatic HTTPS -- LE issuance and renewal via TLS-ALPN-01.
PROXY_SITE  ?= :$(PROXY_PORT)
# The public origin the broker tells providers to send the browser back to
# (DES-018). A hostname PROXY_SITE means https://<hostname>; the port-only
# default has no public origin -- override PUBLIC_URL for a plain-http eval.
PUBLIC_URL  ?= $(if $(filter :%,$(PROXY_SITE)),,https://$(PROXY_SITE))
BROKER_NAME ?= reveille-server
PROXY_NAME  ?= reveille-proxy
# COMPOSE_EXTRA: overlay files layered by variant targets (up-dev). Empty for
# the real deploy, so `make up` composes exactly one file.
COMPOSE_EXTRA =
# The compose PROJECT owns the containers: two invocations sharing a project
# ADOPT each other's containers, so a scratch run with scratch names RECREATES
# the live broker onto scratch config and its teardown stops the live stack
# (measured 2026-08-13: compose_gate took the live bus down for ~15 minutes).
# Scratch invocations MUST override this; the default is the live project.
COMPOSE_PROJECT ?= reveille
# THE UPSTREAMS ARE NOT SET HERE, DELIBERATELY (operator + architect 11825,
# after the 2026-08-18 outage). Their home is $(SERVER_DATA)/reveille.env,
# which every recreate reads; compose passes them through only when the shell
# has them, so an export or `REVEILLE_STT_URL=... make up` still overrides the
# file for one run. Defaulting them HERE would put this deployment's LAN in the
# repo and would silently outrank the operator's own file -- which is the
# failure being fixed, wearing different clothes.
COMPOSE = SERVER_IMAGE=$(SERVER_IMAGE) SERVER_DATA=$(SERVER_DATA) \
  REVEILLE_NET=$(SERVER_NETWORK) AGENTS_PATH=$(AGENTS_PATH) \
  PROXY_IMAGE=$(PROXY_IMAGE) PROXY_SITE=$(PROXY_SITE) REVEILLE_PUBLIC_URL=$(PUBLIC_URL) \
  BROKER_NAME=$(BROKER_NAME) PROXY_NAME=$(PROXY_NAME) \
  docker compose -p $(COMPOSE_PROJECT) -f docker/compose.yml $(COMPOSE_EXTRA)

# REFUSES TO REBUILD AN EXISTING TAG. The version string is the image tag; building
# changed content under a tag that exists makes two images answer to one name and
# puts rollback out of reach (msg 8595 -- it nearly happened twice in one day, once
# from each side of a review). "Is this tag built" is a fact with a lifetime; this
# refusal is what lets nobody hold the timing in their head.
server-image:
	@if docker image inspect $(SERVER_IMAGE) >/dev/null 2>&1; then \
	  echo "REFUSING to rebuild $(SERVER_IMAGE): the tag already exists."; \
	  echo "If the source changed, bump the version (pyproject.toml) so the"; \
	  echo "artifact keeps one identity. To discard the old image deliberately:"; \
	  echo "  docker rmi $(SERVER_IMAGE)"; \
	  exit 1; \
	fi
	docker build -t $(SERVER_IMAGE) -f docker/Dockerfile.server .

# DES-009. NOT wired into `up`: this image carries CUDA torch and a 350M model,
# takes minutes to build and gigabytes to hold, and a fleet that has not asked
# for voices must never pay for it during a deploy.
#
# THERE IS NO COMPOSE SERVICE TO START ANY MORE (S3, ruled 11961/11965; the
# `voices` profile removed here). The synthesizer runs as a HOST service under
# deploy/reveille-tts.service, on whatever machine has the card -- since
# 2026-08-19 that is titan.vyzon.ai, not the host running the broker. Build the
# image on THAT host and start it with systemctl; the broker only ever needs
# REVEILLE_TTS_URL to point at it.
# The build context IS the upstream repo at the pinned SHA -- no Dockerfile of
# ours, nothing vendored, and the pin file is the only input (publish-images and
# image-pin-check read the same file). No refuse-to-rebuild here, deliberately:
# this tag is not a version claim about the repo.
tts-image:
	docker build -t $(TTS_IMAGE) -f Dockerfile.cu128 $(TTS_UPSTREAM)

up:
	@bash scripts/deploy-preflight "$(SERVER_DATA)" "$(BROKER_NAME)" "$(PROXY_NAME)" "$(PROXY_SITE)"
	@bash scripts/agent-image-check
	@docker image inspect $(SERVER_IMAGE) >/dev/null 2>&1 || $(MAKE) server-image
	@docker network create $(SERVER_NETWORK) 2>/dev/null || true
	$(COMPOSE) up -d --wait
	@# REACHABLE BY NAME, from the network the agents are on -- not just the
	@# host probe. A deploy that answers on 127.0.0.1 and is invisible to every
	@# agent cut the whole fleet off the bus once already; the healthcheck
	@# cannot see this because it runs INSIDE the container.
	@docker run --rm --network $(SERVER_NETWORK) --entrypoint /app/.venv/bin/python \
	  $(SERVER_IMAGE) -c "import urllib.request as u; \
	  print('reachable by name:', u.urlopen('http://reveille-server:8765/version', \
	  timeout=5).read().decode())" \
	  || { echo "FAILED: broker up on the host but NOT reachable as"; \
	       echo "reveille-server on the $(SERVER_NETWORK) network -- every agent"; \
	       echo "container is cut off from the bus"; exit 1; }
	@# A DEPLOY IS BOTH HALVES, AND NOW IT DOES BOTH. The launcher is the second
	@# deploy unit: it runs from a pinned clone that nothing restarts on merge,
	@# so a fix could be merged, reviewed and NOT RUNNING with nothing saying so
	@# -- which is what happened to the login-home crash, for six reviews (msg
	@# 8681). The pin check caught that and then asked a human to run three
	@# commands, which the operator hit twice in one evening and correctly asked
	@# why it was not simply done. It is done here. deploy-launcher pins,
	@# restarts by exact pid, and VERIFIES the launcher came back on the new
	@# commit -- the one thing the instructions could not do, because the Stop
	@# hook only respawns at the end of an agent turn and a box with no agent
	@# running has nothing to bring it back.
	@bash scripts/deploy-launcher || exit 1
	@# The check stays, now as the assertion that the fix above worked rather
	@# than as the thing that tells someone to go and do it.
	@bash scripts/launcher-pin-check || exit 1
	@# AGENT CONTAINERS BEHIND THE IMAGE ROLL HERE (DES-006 s7.2, ruling 11807),
	@# and only the IDLE ones: no live attach grant, empty spool, nothing unread,
	@# no bus send in REVEILLE_ROLL_IDLE_MIN (default 10) minutes -- each read,
	@# never guessed. A busy agent is LISTED with why and retried on the next
	@# deploy; nothing is ever killed mid-task. BUSY NEVER FAILS THE DEPLOY
	@# -- roll_idle exits 0 listing the skips -- but a CRASHED roll step now
	@# DOES (ruling 13245: two deploys in a row carried a locked-db traceback
	@# inside a green trip, found only because a human read the log). A died
	@# step inside a trip that exits 0 is the trip lying about itself; if a
	@# step's exit status will control anything, nothing downstream may
	@# swallow it.
	@# THIS IS THE SCHEDULER for the roll: an image bump that reaches no running
	@# container is the defect the lesson image-fix-never-reaches-a-running-
	@# container is about, and a verb nobody invokes is how it happens again.
	@REVEILLE_AGENT_IMAGE=$(AGENT_IMAGE) uv run --quiet python scripts/reveille_launch.py \
		upgrade --all --idle --image $(AGENT_IMAGE)
	@echo "reveille up: proxy $(PROXY_SITE) (/ = bus, /agents = launcher), broker :8765, data=$(SERVER_DATA), network=$(SERVER_NETWORK)"
	@# WHERE THOSE TWO CAME FROM. A deploy that silently used a default for
	@# PROXY_SITE would answer on :80 with no public origin and break the doors,
	@# so the run says whether this box has a settings file or is defaulting.
	@if [ -f "$(DEPLOY_CONF)" ]; then echo "  settings: $(DEPLOY_CONF)"; \
	else echo "  settings: NONE -- $(DEPLOY_CONF) does not exist, so SERVER_DATA and"; \
	     echo "            PROXY_SITE came from this command line or the defaults."; fi

# `make up` with the working tree's UI mounted LIVE (docker/compose.dev.yml):
# edit src/reveille/ui/bus/index.html, refresh, done -- no rebuild. Same
# preflight, same guards, same by-name probe; the ONLY delta is the overlay,
# and the override announces itself everywhere it can be seen. `make down`
# stops this stack too (same project, same services).
up-dev: COMPOSE_EXTRA = -f docker/compose.dev.yml
up-dev: up
	@echo "UI DEV MODE: serving src/reveille/ui/bus LIVE (see /version); plain 'make up' returns to the baked UI"

# Stops the platform containers. Deliberately NOT `compose down`: down removes the
# network, agent containers live on it, and removing a network with live endpoints
# half-fails into exactly the hand-assembled state this file exists to end.
down:
	$(COMPOSE) stop

# ---- containerised agents ---------------------------------------------------------
# A container is just another client: it sets the same two env vars and runs the same
# `claude mcp add` as `make register`. Nothing here may become required to reach the bus,
# because standalone agents on a laptop must keep working exactly as they do today.
agent-image:
	docker build -t $(AGENT_IMAGE) -f docker/Dockerfile .

# One agent, one container, one role. State and workspace are SEPARATE mounts on purpose:
#   reveille-<role>  (volume) -- what the agent KNOWS: its claude login + memory. Survives.
#   $(WORK)          (dir)    -- what it is working ON. Throw away a bad checkout without
#                               burning 200 memory files.
# The token is passed by NAME so its value never lands in argv (ps, shell history).
# --network host is the Linux answer to reaching a broker on 127.0.0.1; point URL at the
# LAN name instead and this runs anywhere.
# TTY= (empty) runs it without a terminal, for scripts and CI: `claude` wants -it, a
# one-shot command cannot have it.
WORK ?= $(HOME)/agents/$(ROLE)
TTY  ?= -it
agent-container:
	@test -n "$(ROLE)" || { echo "usage: make agent-container ROLE=<name> [WORK=<dir>] [URL=]"; exit 2; }
	@test -n "$$REVEILLE_TOKEN" || { echo "export REVEILLE_TOKEN first (the broker maps it to your rooms)"; exit 2; }
	@mkdir -p "$(WORK)"
	@docker volume create reveille-$(ROLE) >/dev/null
	docker run --rm $(TTY) --network host --name reveille-$(ROLE) \
	  -e REVEILLE_AGENT_ROLE=$(ROLE) \
	  -e REVEILLE_TOKEN \
	  -e REVEILLE_URL=$(or $(URL),http://127.0.0.1:8765) \
	  -v reveille-$(ROLE):/home/agent/.claude \
	  -v "$(WORK)":/home/agent/work \
	  $(AGENT_IMAGE) $(or $(CMD),claude)

# The claim under test: a containerised agent needs NO migration -- rooms, history and
# lessons come from the token, server-side. Read-only but for a heartbeat.
agent-spike:
	@test -n "$$REVEILLE_TOKEN" || { echo "export REVEILLE_TOKEN and REVEILLE_AGENT_ROLE first"; exit 2; }
	docker run --rm --network host \
	  -e REVEILLE_AGENT_ROLE -e REVEILLE_TOKEN \
	  -e REVEILLE_URL=$(or $(URL),http://127.0.0.1:8765) \
	  -v "$(REPO)/docker/spike_join.py:/tmp/spike_join.py:ro" \
	  --entrypoint bash $(AGENT_IMAGE) -c 'uv run --quiet --with mcp python /tmp/spike_join.py'

# ---- the launcher (DES-002 T2) ----------------------------------------------------
# The ONLY thing that touches docker; a normal bus client for its health check. Runs
# from the repo env so it stays lockstep with the broker it reads.
launch:
	@uv run python scripts/reveille_launch.py $(ARGS)

# End-to-end gate: real broker on a scratch db, provision one container through the
# launcher (agent-probe stands in for claude, no Anthropic login needed), assert the
# launcher sees it live+connected, then destroy. Proves provision + health-by-presence
# + destroy against a real broker, and that launcher.db holds no token bytes.
launch-smoke: agent-image
	uv run python tests/launch_smoke.py

# T3 gate: grants end to end -- mirror, -r server-side, revoke <1s, exclusivity
# race named, expiry sweep, audit lines, kill-and-reprovision (section 5).
grant-smoke: agent-image
	uv run python tests/grant_smoke.py

# DES-003 W1 gate: waked + spool + wake-watch end to end against a real broker --
# attach+ring, supersede (old holder exits 2), kill -9 reclaim, broker restart
# absorbed with zero agent re-arms.
waiter-smoke: sync
	uv run python tests/waiter_smoke.py

# DES-005 P0 gate: tenancy against real docker -- namespaced names, per-agent
# homes (cross-user AND same-user), pid cap with the host unaffected, restart=no,
# per-user container cap, destroy+recreate keeps data, idle sweep stops.
tenancy-smoke: agent-image
	uv run python tests/tenancy_smoke.py

# DES-005 P1 gate: full lifecycle over the launcher HTTP API behind a real
# broker session -- 401 without cookie, cross-user unreachable, attach URL in
# exactly one response, provision token in no response and no file.
launcher-api-smoke: agent-image
	uv run python tests/launcher_api_smoke.py

# DES-006 U2 gate: the launcher refuses to serve without the docker socket,
# says where its state lives, is one-per-data-root by flock, binds 127.0.0.1
# only, and a blind respawn after kill -9 brings it back.
launcher-supervision-smoke: sync
	uv run python tests/launcher_supervision_smoke.py

# DES-003 W2 gate: join-here from a clean shell (scratch HOME + scratch broker):
# checklist walked, token in exactly one file (0600 fragment), MCP config carries
# the env template not the value, live+connected from the bootstrap alone.
joinhere-smoke: sync
	uv run python tests/joinhere_smoke.py

# Attachment gate: a file on the bus comes back byte-identical over the raw-body
# route and the MCP upload() tool, a multipart form is refused rather than stored
# as an envelope, and /files/* serves nothing the browser will render on our own
# origin. Headers asserted off the wire, not read out of the source.
upload-gate: sync
	uv run python tests/upload_gate.py

# Offline-recovery gate: an agent that ends a turn while the broker is DOWN still
# recovers by itself when it comes back -- no keystroke. The hook must do its
# LOCAL work (spawn the waiter, demand the watcher) without probing the bus.
offline-recovery-smoke: sync
	uv run python tests/offline_recovery_smoke.py

# Readmit gate: an agent whose membership was REAPED comes back on its next
# ordinary call (visible, addressable, pre-outage mail still unread), while an
# agent that deliberately LEFT stays gone until it joins again. The two absences
# must not be the same absence -- when they were, re-admission voided
# DIRECTIVE:LEAVE within one tool call.
readmit-gate: sync
	uv run python tests/readmit_gate.py

# Deafness gate: a silent agent with unread direct mail is VISIBLY deaf from
# both presence surfaces, with the reason (no-waiter / not-draining), and one
# ordinary call clears it. The verdict is computed at read time, never stored.
deafness-gate: sync
	uv run python tests/deafness_gate.py

# Deploy-both-halves gate: `make up` refuses when the LAUNCHER answers and is
# running older code than the tree being deployed. The broker's version was
# probed on every deploy; the launcher's was never checked, so a merged fix sat
# un-run for six reviews and broke first-time login the whole time.
launcher-pin-check-gate: sync
	uv run python tests/launcher_pin_check.py

# Room-events gate: the room PUSHES its own events, so a browser learns that
# someone arrived or left without asking. Every /feed frame names its event
# type, and presence rides that channel carrying the whole list, not a diff.
room-events-gate: sync
	uv run python tests/room_events_gate.py

# Leave-sticks gate: a DIRECTIVE:LEAVE survives the boot ritual. join() used to
# clear every leave mark unconditionally, and join() at startup is the standing
# ritual -- so leaving lasted until the next restart. Gated through the real MCP
# tool, because a store-level test that filters the rooms itself would pass on
# the broken daemon: the daemon's fault was calling join at all.
leave-sticks-gate: sync
	uv run python tests/leave_sticks_gate.py

# Feed-ghost gate: a CLOSED TAB IS NOT A WATCHER. 0.2.35 computes a person's
# presence from the set of browsers holding a room's feed, so a socket that is
# never read from -- and therefore never notices the close -- keeps someone
# reading as live in a room they left. Found live at 26 entries for 2 browsers.
feed-ghost-gate: sync
	uv run python tests/feed_ghost_gate.py

# The code-relay boundary, gated by its NEGATIVE cases (ruling 8644): scoped
# to the caller's own pending login, one relay, opaque code, zero leakage.
login-relay-smoke: sync
	uv run python tests/login_relay_smoke.py

# SIGTERM gate: with a /feed socket held open by a client that never hangs up
# (a browser tab -- the live incident's holder), SIGTERM still exits within the
# bounded graceful timeout, courtesy frame first. On the unfixed daemon this
# wedges: listeners closed, process alive, docker reporting Up.
sigterm-gate: sync
	uv run python tests/sigterm_gate.py

# Compose gate: the platform comes up DECLARED on a scratch project (own name,
# network, ports, data root -- the live stack untouched), broker healthy and
# reachable by name, one front door; rebuilding an existing tag refuses;
# preflight refuses an empty data root over a live db; down keeps the network.
compose-gate: sync
	uv run python tests/compose_gate.py

# Single-origin gate (DES-006 U3/U4/U6): the front door, through the REAL shipped
# Caddyfile with a REAL session cookie. One login covers both services, the bus
# page carries the launcher prefix it fetches with, every endpoint the embedded
# Agents pane calls answers 200 JSON there, and the same paths unprefixed do not
# answer at all. U6 shipped calling them unprefixed; its harness mocked fetch.
single-origin-smoke: sync
	uv run python tests/single_origin_smoke.py

# Pinned-source gate: the serving launcher does not live in a working tree.
# pin clones to a declared path on main and refuses a dirty tree, the supervisor
# spawns from THAT path (and refuses loudly when none is declared), and
# rewriting the dev tree's launcher mid-flight changes nothing about what serves.
launcher-pin-smoke: sync
	uv run python tests/launcher_pin_smoke.py

# Sweep SCHEDULER gate: proves the 4.6 tick runs by itself. Starts serve, plants
# an expired grant, and waits -- calling nothing. The gates that call _sweep_once
# by hand all passed while the sweep had never run in production once.
sweep-scheduler-smoke: sync
	uv run python tests/sweep_scheduler_smoke.py

# NOT part of any deploy: most unapplied commits are a branch legitimately in
# review, and a check that fires on every open branch is one nobody reads. Run
# it during hygiene, when a branch you BELIEVE is finished still prints a line.
branch-orphans:
	@bash scripts/branch-orphans

lint:
	uv run ruff check src tests scripts

clean:
	rm -rf src/reveille/__pycache__ tests/__pycache__ .ruff_cache .mypy_cache .pytest_cache
