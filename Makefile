REPO := $(abspath .)
PREFIX ?= $(HOME)/.local/bin
LOG  := $(REPO)/reveille.log
PID  := $(REPO)/reveille.pid

# Tag-per-image-change (architect ruling, msg 8433): any Dockerfile change bumps
# this tag in the same commit -- a fixed tag over drifting content makes
# launcher.db image records ambiguous.
AGENT_IMAGE ?= reveille-agent:0.2.6

.PHONY: help sync build test smoke daemon start stop restart status logs register unregister install-agent lint clean agent-image agent-container agent-spike server-image server-run server-stop

help:
	@echo "make sync           create/refresh the uv env (Python 3.14, locked)"
	@echo "make test           unit suite (uv run pytest)"
	@echo "make smoke          end-to-end smoke: real daemon, HTTP-MCP + WS wake + auth"
	@echo "make build          sync + test + smoke"
	@echo "make daemon         run the broker in the FOREGROUND (Ctrl-C to stop)"
	@echo "make start          start the broker in the background -> reveille.log. Env: REVEILLE_PORT, REVEILLE_DB"
	@echo "make stop           stop the background broker"
	@echo "make restart        stop + start"
	@echo "make status         is the background broker running?"
	@echo "make logs           tail -f reveille.log"
	@echo "make register [URL=] register reveille once (user scope); identity = per-session \$$REVEILLE_AGENT_ROLE"
	@echo "make install-agent  install the 'agent <name>' launcher into $(PREFIX)"
	@echo "make unregister      remove the reveille MCP registration"
	@echo "make lint           ruff check"
	@echo "make agent-image    build the agent container image ($(AGENT_IMAGE))"
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
# per session from that session's own env. So one registration serves every tmux pane;
# each pane just exports its own $REVEILLE_AGENT_ROLE (see `agent` launcher / install-agent).
# URL is 127.0.0.1 on the daemon host, the LAN name elsewhere (override: URL=...).
register:
	-claude mcp remove reveille --scope user 2>/dev/null
	claude mcp add --transport http --scope user reveille "$(or $(URL),http://127.0.0.1:8765)/mcp" \
	  --header 'Authorization: Bearer $${REVEILLE_TOKEN:-}' \
	  --header 'X-Agent: $${REVEILLE_AGENT_ROLE:-unset-agent}'
	python3 scripts/install-hook
	@echo "registered. each session: export REVEILLE_AGENT_ROLE=<dev> (and REVEILLE_TOKEN) before 'claude',"
	@echo "or use: agent <dev>   (see make install-agent)"

# Install the 'agent <name>' launcher so a pane is one command: `agent roc-api-dev`.
install-agent:
	install -d "$(PREFIX)"
	install -m 0755 scripts/agent "$(PREFIX)/agent"
	@echo "installed $(PREFIX)/agent  (ensure $(PREFIX) is on PATH)"

unregister:
	claude mcp remove reveille --scope user || true

# ---- the standalone server --------------------------------------------------------
# The broker as a container: built from this source, run from anywhere. One bind mount
# ($(SERVER_DATA)) carries the database AND attachments (<db dir>/files). The server
# needs no agent credentials -- REVEILLE_AGENT_ROLE/REVEILLE_TOKEN are client env; auth
# lives in the database. Port published on 0.0.0.0 so the LAN (and remote agents) reach it.
SERVER_IMAGE ?= reveille-server:$(shell grep -m1 '^version' pyproject.toml | cut -d'"' -f2)
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

server-image:
	docker build -t $(SERVER_IMAGE) -f docker/Dockerfile.server .

server-run: server-image
	@mkdir -p "$(SERVER_DATA)"
	docker network create $(SERVER_NETWORK) 2>/dev/null || true
	docker rm -f reveille-server 2>/dev/null || true
	docker run -d --name reveille-server --restart unless-stopped \
	  --network $(SERVER_NETWORK) \
	  -p 8765:8765 \
	  -v "$(SERVER_DATA)":/data \
	  -e REVEILLE_AGENTS_PATH="$(AGENTS_PATH)" \
	  $(SERVER_IMAGE)
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  curl -sf http://127.0.0.1:8765/health >/dev/null && break; \
	  sleep 1; \
	  if [ $$i = 10 ]; then \
	    echo "FAILED (host) -- docker logs reveille-server:"; \
	    docker logs --tail 5 reveille-server; exit 1; fi; \
	done
	@# REACHABLE BY NAME, from the network the agents are on -- not just from the
	@# host. A deploy that answers on 127.0.0.1 and is invisible to every agent is
	@# the failure this check exists for: it happened, it cut the whole fleet off
	@# the bus, and nothing noticed for hours because the host-side probe was green.
	@docker run --rm --network $(SERVER_NETWORK) --entrypoint /app/.venv/bin/python \
	  $(SERVER_IMAGE) -c "import urllib.request as u; \
	  print('reachable by name:', u.urlopen('http://reveille-server:8765/version', \
	  timeout=5).read().decode())" \
	  || { echo "FAILED: the broker is up on the host but NOT reachable as" \
	       "reveille-server on the $(SERVER_NETWORK) network -- every agent" \
	       "container is cut off from the bus"; exit 1; }
	@echo "reveille-server up: http://0.0.0.0:8765  data=$(SERVER_DATA)  network=$(SERVER_NETWORK)"

server-stop:
	docker rm -f reveille-server

# ---- one front door (DES-006 U3) --------------------------------------------------
# The proxy is the ONLY thing that knows both addresses. --network host because the
# launcher binds 127.0.0.1 and must stay unreachable from the LAN and the docker
# network; the proxy reaches it over loopback and publishes :80 for everyone else.
PROXY_IMAGE ?= caddy:2-alpine
PROXY_PORT  ?= 80

proxy-run:
	docker rm -f reveille-proxy 2>/dev/null || true
	docker run -d --name reveille-proxy --restart unless-stopped --network host \
	  -v "$(REPO)/docker/Caddyfile":/etc/caddy/Caddyfile:ro \
	  $(PROXY_IMAGE)
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  curl -sf http://127.0.0.1:$(PROXY_PORT)/health >/dev/null && \
	    { echo "reveille-proxy up: http://0.0.0.0:$(PROXY_PORT)  (/ = bus, /agents = launcher)"; exit 0; }; \
	  sleep 1; \
	done; echo "FAILED -- docker logs reveille-proxy:"; docker logs --tail 10 reveille-proxy; exit 1

proxy-stop:
	docker rm -f reveille-proxy

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

# SIGTERM gate: with a /feed socket held open by a client that never hangs up
# (a browser tab -- the live incident's holder), SIGTERM still exits within the
# bounded graceful timeout, courtesy frame first. On the unfixed daemon this
# wedges: listeners closed, process alive, docker reporting Up.
sigterm-gate: sync
	uv run python tests/sigterm_gate.py

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

lint:
	uv run ruff check src tests scripts

clean:
	rm -rf src/reveille/__pycache__ tests/__pycache__ .ruff_cache .mypy_cache .pytest_cache
