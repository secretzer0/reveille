# Reveille agent: a machine that sets two env vars and runs claude.
# The broker knows nothing about containers -- this image is just another client, exactly
# like a standalone agent on a laptop. Nothing here may become required to reach the bus.
FROM debian:trixie-slim

# uid 1000 to match the host: bind-mounted state stays writable both ways, and it costs
# one line now versus a chown migration across every volume later.
ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive
# A UTF-8 LOCALE, OR THE TERMINAL SPELLS EVERY MULTIBYTE CHARACTER "_".
#
# LANG, LC_ALL and LC_CTYPE were all EMPTY, so a tmux CLIENT fell back to ASCII
# and substituted an underscore for anything it could not represent. The trap is
# that the damage is invisible from inside: `tmux capture-pane` prints the cells
# it stores, which were correct UTF-8 the whole time, so every check run in the
# container said the text was fine while the browser showed em dashes and arrows
# as underscores. Three fixes were spent on the renderer and the font stack
# before the characters were captured and turned out to be U+2014 and U+2192 --
# ordinary, and never a font's fault.
#
# C.UTF-8 rather than a generated en_US.UTF-8: it needs no locales package, no
# locale-gen step, and carries no language policy into someone else's agent.
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
# build-essential/pkg-config/cmake: standard C/C++ work is part of what an agent
# gets asked to do; a dev image without a C toolchain fails at the first
# node-gyp or pip sdist anyway.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake curl git gnupg jq less openssl \
      pkg-config ripgrep sudo tmux unzip xvfb \
    && rm -rf /var/lib/apt/lists/*

# node (claude code ships via npm), go, uv (owns python). Versions are pinned: the tag is
# the reproducibility claim, so two builds of one tag must be the same agent.
ARG NODE_MAJOR=24
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

ARG GO_VERSION=1.24.5
RUN curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-amd64.tar.gz" \
      | tar -C /usr/local -xz
ENV PATH=/usr/local/go/bin:/home/agent/.local/bin:$PATH

# ttyd is not packaged in trixie; upstream ships a static binary, pinned like
# everything else here.
ARG TTYD_VERSION=1.7.7
RUN curl -fsSL "https://github.com/tsl0922/ttyd/releases/download/${TTYD_VERSION}/ttyd.x86_64" \
      -o /usr/local/bin/ttyd \
    && chmod 0755 /usr/local/bin/ttyd

ARG GH_VERSION=2.63.2
RUN curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /tmp \
    && install -m 0755 /tmp/gh_${GH_VERSION}_linux_amd64/bin/gh /usr/local/bin/gh \
    && rm -rf /tmp/gh_*

# THE AGENT OWNS WHAT ITS TOOLING AUTO-UPDATES. claude's updater runs as the
# agent uid and rewrites its own npm global tree in place; installed as root
# under /usr/lib it could only warn "npm global folder isn't writable" on
# every /status (operator screenshot, 2026-08-13). A dedicated prefix, chowned
# to the agent after the root-stage installs, gives the updater a tree it may
# write without handing it /usr/bin.
ENV NPM_CONFIG_PREFIX=/opt/npm
ENV PATH=/opt/npm/bin:$PATH

ARG CLAUDE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} && npm cache clean --force

# Browser for web-dev testing/visualization: playwright's own chromium build,
# pinned -- claude's browser tooling (playwright MCP, npx playwright) wants
# playwright's build, so a distro chromium would still trigger an unpinned
# runtime download. Deps install here as root; the browser itself installs
# below as the agent user so it lands in the agent's cache, not root's.
ARG PLAYWRIGHT_VERSION=1.62.0
RUN npm install -g playwright@${PLAYWRIGHT_VERSION} && npm cache clean --force \
    && apt-get update && playwright install-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# Globally-installed packages must be require()-able from anywhere. npm's global
# root here is the dedicated prefix's node_modules, but node's default
# resolution for a bare require() outside a project walks ~/.node_modules,
# ~/.node_libraries and /usr/lib/node -- none of which is that. So
# `node -e "require('playwright')"` threw MODULE_NOT_FOUND from every cwd
# until the caller happened to discover the path themselves (reported by
# reveille-senior-ui-ux, msg 8523, after hitting it mid-task).
# Installed-but-unreachable is the same defect class as a capability with no
# reachable path.
ENV NODE_PATH=/opt/npm/lib/node_modules

RUN groupadd -g ${GID} agent \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chown -R ${UID}:${GID} /opt/npm

USER agent
WORKDIR /home/agent

# uv installs per-user, so it lands in the image rather than in the state volume -- the
# volume is for what the agent LEARNS, not for its toolchain.
ARG UV_VERSION=0.9.28
RUN curl -fsSL https://astral.sh/uv/${UV_VERSION}/install.sh | sh

# Chromium into the agent's browser cache -- image layer, not the state volume,
# same reasoning as uv: toolchain lives in the image.
RUN playwright install chromium

# Caveman comms plugin, baked into the image-side claude home so a FRESH volume is
# seeded with it (docker copies the image path into an empty named volume). Ultra is
# the fleet default for containerised agents -- env, so the launcher can override
# per-container without an image fork. PINNED to a commit like everything else here:
# the plugin CLI can only pin a marketplace by branch/tag, so we clone the source at
# the exact SHA and add THAT as a local marketplace -- an unpinned third-party GitHub
# source pulled at build time is a supply-chain hole. The clone stays in the image so
# `claude plugin marketplace` can still resolve the source.
ARG CAVEMAN_REF=c2ed24b3e5d412cd0c25197b2bc9af587621fd99
RUN git clone https://github.com/JuliusBrussee/caveman /home/agent/caveman-marketplace \
    && git -C /home/agent/caveman-marketplace checkout ${CAVEMAN_REF} \
    && claude plugin marketplace add /home/agent/caveman-marketplace \
    && claude plugin install caveman@caveman
ENV CAVEMAN_DEFAULT_MODE=ultra

# Ponytail (lazy-senior-dev mode), same discipline as caveman: clone at a pinned
# commit, add the local checkout as a marketplace, install -- enabled at boot via
# the plugin's own SessionStart hook. Mode default is env, launcher-overridable
# per container without an image fork.
ARG PONYTAIL_REF=16f29800fd2681bdf24f3eb4ccffe38be3baec6b
RUN git clone https://github.com/DietrichGebert/ponytail /home/agent/ponytail-marketplace \
    && git -C /home/agent/ponytail-marketplace checkout ${PONYTAIL_REF} \
    && claude plugin marketplace add /home/agent/ponytail-marketplace \
    && claude plugin install ponytail@ponytail
ENV PONYTAIL_DEFAULT_MODE=full

# The wake client. Without it a containerised agent has no waiter, so unicast rings never
# arrive and it is deaf between turns -- it would only ever see mail by polling inbox() on
# its next turn, which is the degraded mode, not the design. Shipped from THIS repo's
# source (one source for the protocol, no second implementation to drift), which is why the
# build context is the repo root, not docker/.
COPY --chown=${UID}:${GID} pyproject.toml uv.lock README.md /tmp/reveille/
COPY --chown=${UID}:${GID} src /tmp/reveille/src
RUN uv tool install /tmp/reveille && rm -rf /tmp/reveille

# Two mounts, different lifecycles: what the agent knows vs what it is working on.
# Conflating them means you cannot throw away a bad checkout without burning the memory.
# These MUST exist, owned by agent, BEFORE the VOLUME lines: docker seeds a fresh named
# volume from the image path, and an absent path is created root-owned -- which silently
# costs the agent its memory and its claude login the first time it tries to write.
# The work dir is /home/agent/repos -- the SAME path DES-005 P0's data root
# binds (data/<user>/<agent>/repos); pre-P3 images used ~/work, which meant
# checkouts silently missed the persistence mount.
RUN mkdir -p /home/agent/.claude /home/agent/repos
VOLUME /home/agent/.claude
VOLUME /home/agent/repos

# The shared-tmux plane (DES-002 T1): every client attaches to session "agent";
# ttyd is the browser bridge and attach-gate is its only exec target -- browser
# clients never reach tmux except through a verified grant token.
COPY --chown=${UID}:${GID} docker/tmux.conf /home/agent/.tmux.conf
COPY docker/attach-gate /usr/local/bin/attach-gate
COPY docker/agent-probe /usr/local/bin/agent-probe
# The Stop hook, from THIS repo's copy -- one source, so the container's backstop
# cannot drift from the host's. The entrypoint points settings.json at this path.
# Its waked block is a harmless no-op here (the entrypoint's own supervisor holds
# the flock); what the container needs from it is the WATCHER REQUIREMENT, which
# is the only thing that makes an agent recover from ending a turn unarmed.
COPY --chmod=0755 src/reveille/agent-stop-hook /usr/local/bin/agent-stop-hook
COPY --chown=${UID}:${GID} docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Interactive claude with "reveille" queued as the first prompt: the agent joins the
# bus and arms its waiter the moment the session is up, no human at the keyboard.
CMD ["claude", "reveille"]
