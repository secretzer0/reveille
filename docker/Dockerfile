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
# ffmpeg (GO 12872): the voice-pipeline tests (test_script_writer, test_clips,
# test_voice_worker) shell out to it, so no container body could run
# `pytest tests/ -q` locally -- 20 failures on every 0.2.22/0.2.23 body,
# measured in three of them. CI carries its own pinned static ffmpeg and stays
# the authoritative gate; this restores the local run.
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates cmake curl ffmpeg git gnupg jq less \
      openssl pkg-config ripgrep sudo tmux unzip xvfb zip \
    && rm -rf /var/lib/apt/lists/*

# NON-APT TOOLS RUN THEIR VENDOR'S RECOMMENDED INSTALLER, UNPINNED (operator
# 14461/14464/14469, ruled 14471): each resolves whatever the vendor serves on
# build day. THE TAG THEREFORE MARKS INTENT, NOT CONTENT -- two builds of one
# tag can differ; the doctrine that claimed otherwise is superseded, and what
# replaces the pin is the BUILD MANIFEST below: every tool's resolved version,
# recorded at build and printed in the boot report, so a broken agent is
# datable from inside the running container. apt is the default for anything
# NEW; this exception list is closed and gated (a twelfth installer is a
# ruling, not a diff).
# node: NodeSource ships no "latest" script -- the major IS the URL, which is
# their recommended form; moving majors stays a deliberate edit.
ARG NODE_MAJOR=24
RUN curl -fsSL https://deb.nodesource.com/setup_${NODE_MAJOR}.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

RUN GO_VERSION=$(curl -fsSL "https://go.dev/VERSION?m=text" | head -1) \
    && curl -fsSL "https://go.dev/dl/${GO_VERSION}.linux-amd64.tar.gz" \
      | tar -C /usr/local -xz
ENV PATH=/usr/local/go/bin:/home/agent/.local/bin:$PATH

# ttyd is not packaged in trixie; upstream ships a static binary.
RUN curl -fsSL "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd.x86_64" \
      -o /usr/local/bin/ttyd \
    && chmod 0755 /usr/local/bin/ttyd

# gh's release assets carry the version in the filename, so "latest" is
# resolved by asking the API which tag is current -- still their artifact,
# still their recommended tarball, no version literal here.
RUN GH_TAG=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | jq -r .tag_name) \
    && { [ -n "$GH_TAG" ] && [ "$GH_TAG" != null ]; } \
      || { echo "gh release lookup failed (API rate limit? 60/hour/IP unauthenticated)" >&2; exit 1; } \
    && GH_VERSION=${GH_TAG#v} \
    && curl -fsSL "https://github.com/cli/cli/releases/download/${GH_TAG}/gh_${GH_VERSION}_linux_amd64.tar.gz" \
      | tar -xz -C /tmp \
    && install -m 0755 /tmp/gh_${GH_VERSION}_linux_amd64/bin/gh /usr/local/bin/gh \
    && rm -rf /tmp/gh_*

# CLAUDE IS THE ONE BUILD INPUT THAT IS DELIBERATELY NOT PINNED (operator rule
# 2026-08-22, superseding 12401 D1's image-pin doctrine): the ENTRYPOINT pulls
# @latest at every container START, and the copy baked here is only the
# fallback for a boot where npm is unreachable. Pinning at build froze the
# fleet at whatever npm served on rebuild day -- the 2.1.240 bus-deaf incident
# was found because of exactly that snapshot drift. What makes any version safe
# is the entrypoint's converged zero-prompt contract, not a frozen number.
# DISABLE_AUTOUPDATER stays ON: the boot-time install is the ONLY updater. The
# MID-SESSION rename-unpack-swap is what bricked binaries twice on 2026-08-19
# when interrupted by a stop; a boot-time install into the container's own
# layer just retries on the next boot. IMAGE AND DATA-ROOT ONLY: nothing may
# ever write this switch onto a human's native install (same rule as
# laptop-awake.conf, 0.2.182).
# /opt/npm stays a dedicated agent-owned prefix -- it is what lets the
# entrypoint (uid 1000) install at boot, and a root-owned global tree made
# claude warn on every /status (operator screenshot, 2026-08-13).
ENV DISABLE_AUTOUPDATER=1
ENV NPM_CONFIG_PREFIX=/opt/npm
ENV PATH=/opt/npm/bin:$PATH

ARG CLAUDE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} && npm cache clean --force

# Browser for web-dev testing/visualization: playwright's own chromium build,
# pinned -- claude's browser tooling (playwright MCP, npx playwright) wants
# playwright's build, so a distro chromium would still trigger an unpinned
# runtime download. Deps install here as root; the browser itself installs
# below as the agent user so it lands in the agent's cache, not root's.
RUN npm install -g playwright && npm cache clean --force \
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
RUN curl -LsSf https://astral.sh/uv/install.sh | sh

# Chromium into the agent's browser cache -- image layer, not the state volume,
# same reasoning as uv: toolchain lives in the image.
RUN playwright install chromium

# Caveman comms plugin, baked into the image-side claude home so a FRESH volume is
# seeded with it (docker copies the image path into an empty named volume). Ultra is
# the fleet default for containerised agents -- env, so the launcher can override
# per-container without an image fork. Default branch, per the unpinned-installers
# ruling (14471, superseding this block's old commit pin); the clone's HEAD sha is
# recorded in the build manifest below. The clone stays in the image so
# `claude plugin marketplace` can still resolve the source.
RUN git clone https://github.com/JuliusBrussee/caveman /home/agent/caveman-marketplace \
    && claude plugin marketplace add /home/agent/caveman-marketplace \
    && claude plugin install caveman@caveman
ENV CAVEMAN_DEFAULT_MODE=ultra

# Ponytail (lazy-senior-dev mode), same shape as caveman: default branch, HEAD
# recorded in the manifest, local checkout as marketplace -- enabled at boot via
# the plugin's own SessionStart hook. Mode default is env, launcher-overridable
# per container without an image fork.
RUN git clone https://github.com/DietrichGebert/ponytail /home/agent/ponytail-marketplace \
    && claude plugin marketplace add /home/agent/ponytail-marketplace \
    && claude plugin install ponytail@ponytail
ENV PONYTAIL_DEFAULT_MODE=full

# sdkman (operator 14456): the JVM-ecosystem switcher -- 82 candidates, one
# `sdk install java <ver>-<vendor>` per JDK. The recommended installer runs
# VERBATIM (operator 14464/14465: no assert, no hash, no mirror). Its store
# lives at ~/.sdkman, which the launcher bind-mounts ({root}/sdkman) so
# installed JDKs survive a container recreate -- without the mount every SDK
# died with the writable layer.
RUN curl -s "https://get.sdkman.io" | bash \
    && echo "sdkman $(cat /home/agent/.sdkman/var/version 2>/dev/null)"

# The wake client. Without it a containerised agent has no waiter, so unicast rings never
# arrive and it is deaf between turns -- it would only ever see mail by polling inbox() on
# its next turn, which is the degraded mode, not the design. Shipped from THIS repo's
# source (one source for the protocol, no second implementation to drift), which is why the
# build context is the repo root, not docker/.
COPY --chown=${UID}:${GID} pyproject.toml uv.lock README.md /tmp/reveille/
COPY --chown=${UID}:${GID} src /tmp/reveille/src
RUN uv tool install /tmp/reveille && rm -rf /tmp/reveille

# THE BUILD MANIFEST (ruled 14471): with installers unpinned, the tag marks
# intent, not content -- this file is what says WHAT ACTUALLY LANDED, resolved
# from the installed tools themselves at build and printed in the boot report.
# "Which agent is this and what is in it" is answered from the running
# container; a broken agent becomes datable by diffing manifests.
RUN { echo "built $(date -u +%Y-%m-%dT%H:%M:%SZ)"; \
      echo "node $(node --version)"; \
      echo "go $(/usr/local/go/bin/go version | awk '{print $3}')"; \
      echo "ttyd $(ttyd --version 2>&1 | head -1)"; \
      echo "gh $(gh --version | head -1)"; \
      echo "claude-baked $(claude --version 2>/dev/null | head -1)"; \
      echo "playwright $(npm ls -g playwright --depth=0 2>/dev/null | grep -o 'playwright@.*')"; \
      echo "uv $(uv --version)"; \
      echo "sdkman $(cat /home/agent/.sdkman/var/version 2>/dev/null)"; \
      echo "caveman $(git -C /home/agent/caveman-marketplace rev-parse --short HEAD)"; \
      echo "ponytail $(git -C /home/agent/ponytail-marketplace rev-parse --short HEAD)"; \
      echo "reveille $(uv tool list 2>/dev/null | grep -m1 reveille || echo from-source)"; \
    } | tee /home/agent/.reveille-build-manifest

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
# Bus-deaf detection (see entrypoint): a body that cannot reach the bus must
# say so where the Agents list looks, instead of showing as running.
COPY --chmod=0755 docker/busdeaf-probe /usr/local/bin/busdeaf-probe
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
