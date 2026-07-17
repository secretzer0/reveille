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
      ca-certificates curl git gnupg jq less ripgrep sudo unzip xvfb \
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

# Two mounts, different lifecycles: what the agent knows vs what it is working on.
# Conflating them means you cannot throw away a bad checkout without burning the memory.
# These MUST exist, owned by agent, BEFORE the VOLUME lines: docker seeds a fresh named
# volume from the image path, and an absent path is created root-owned -- which silently
# costs the agent its memory and its claude login the first time it tries to write.
RUN mkdir -p /home/agent/.claude /home/agent/work
VOLUME /home/agent/.claude
VOLUME /home/agent/work

COPY --chown=${UID}:${GID} entrypoint.sh /usr/local/bin/entrypoint.sh
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["claude"]
