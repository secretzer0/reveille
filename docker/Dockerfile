# Reveille agent: a machine that sets two env vars and runs claude.
# The broker knows nothing about containers -- this image is just another client, exactly
# like a standalone agent on a laptop. Nothing here may become required to reach the bus.
FROM debian:trixie-slim

# uid 1000 to match the host: bind-mounted state stays writable both ways, and it costs
# one line now versus a chown migration across every volume later.
ARG UID=1000
ARG GID=1000

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg jq less openssl ripgrep sudo tmux unzip xvfb \
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

ARG CLAUDE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION} && npm cache clean --force

RUN groupadd -g ${GID} agent \
    && useradd -m -u ${UID} -g ${GID} -s /bin/bash agent \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent

USER agent
WORKDIR /home/agent

# uv installs per-user, so it lands in the image rather than in the state volume -- the
# volume is for what the agent LEARNS, not for its toolchain.
ARG UV_VERSION=0.9.28
RUN curl -fsSL https://astral.sh/uv/${UV_VERSION}/install.sh | sh

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

# The wake client. Without it a containerised agent has no waiter, so unicast rings never
# arrive and it is deaf between turns -- it would only ever see mail by polling inbox() on
# its next turn, which is the degraded mode, not the design. Shipped from THIS repo's
# source (one source for the protocol, no second implementation to drift), which is why the
# build context is the repo root, not docker/.
COPY --chown=${UID}:${GID} pyproject.toml uv.lock README.md /tmp/agentbus/
COPY --chown=${UID}:${GID} src /tmp/agentbus/src
RUN uv tool install /tmp/agentbus && rm -rf /tmp/agentbus

# Two mounts, different lifecycles: what the agent knows vs what it is working on.
# Conflating them means you cannot throw away a bad checkout without burning the memory.
# These MUST exist, owned by agent, BEFORE the VOLUME lines: docker seeds a fresh named
# volume from the image path, and an absent path is created root-owned -- which silently
# costs the agent its memory and its claude login the first time it tries to write.
RUN mkdir -p /home/agent/.claude /home/agent/work
VOLUME /home/agent/.claude
VOLUME /home/agent/work

# The shared-tmux plane (DES-002 T1): every client attaches to session "agent";
# ttyd is the browser bridge and attach-gate is its only exec target -- browser
# clients never reach tmux except through a verified grant token.
COPY --chown=${UID}:${GID} docker/tmux.conf /home/agent/.tmux.conf
COPY docker/attach-gate /usr/local/bin/attach-gate
COPY docker/agent-probe /usr/local/bin/agent-probe
COPY --chown=${UID}:${GID} docker/entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
# Interactive claude with "reveille" queued as the first prompt: the agent joins the
# bus and arms its waiter the moment the session is up, no human at the keyboard.
CMD ["claude", "reveille"]
