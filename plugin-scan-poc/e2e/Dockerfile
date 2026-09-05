# Throwaway sandbox for confirming what a plugin install actually writes to disk.
# Nothing here touches the host: the repo is mounted read-only, the Claude
# config lives at a container path, and `docker run --rm` deletes the container.
FROM node:22-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 ca-certificates jq \
 && rm -rf /var/lib/apt/lists/*

# Pin via build arg so the sandbox is reproducible; override with
# --build-arg CLAUDE_CODE_VERSION=x.y.z
ARG CLAUDE_CODE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} \
 && claude --version

# Isolated, disposable Claude config. Never the host's ~/.claude.
ENV CLAUDE_CONFIG_DIR=/claude-home
RUN mkdir -p /claude-home /out

COPY verify.sh /usr/local/bin/verify.sh
RUN chmod +x /usr/local/bin/verify.sh

WORKDIR /work
ENTRYPOINT ["/usr/local/bin/verify.sh"]
