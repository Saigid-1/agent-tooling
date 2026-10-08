# syntax=docker/dockerfile:1
# The one distributable image. Build from the repository root:
#   docker build -f deploy/Dockerfile --target product \
#     --build-arg SOURCE_REVISION="$(git rev-parse HEAD)" -t agent-tooling:product .
# Targets:
#   runtime     the slim Python package (memory and navigation CLI); no Node, torch or Serena
#   product     navigation, memory, capture, refresh and the Kanban board (`kanban`)
#   agents      product plus the pinned claude and codex CLIs (opt-in)
#   opencode    product plus the pinned OpenCode CLI (opt-in; no claude or codex)
#   ops         product plus the OPS extension (extensions/ops): its tools, assets and
#               console scripts, for the existing OPS desks during the transition
#   acceptance  test-only derivative of product; never a production image
# Only `ops` carries kp_agent_tooling_ops; runtime, product, agents and opencode are the core alone.
# No target contains credentials. Every target is labelled with SOURCE_REVISION
# (org.opencontainers.image.revision) and SOURCE_URL (org.opencontainers.image.source).
# The release workflow (.github/workflows/release.yml) publishes runtime, product, ops and the
# optional opencode, each with org.opencontainers.image.licenses; agents and acceptance are
# local builds only and are never published.
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG NODE_IMAGE=node:22-bookworm-slim
# The repository every target's source label names. No target hard-codes one.
ARG SOURCE_URL=https://github.com/Saigid-1/agent-tooling

FROM ${NODE_IMAGE} AS node

FROM ${PYTHON_IMAGE} AS python-upstream
RUN find /root -mindepth 1 -delete

# The upstream Python filesystem without its build-time environment (GPG_KEY,
# PYTHON_SHA256), so no target carries a variable named like a credential.
FROM scratch AS base
COPY --from=python-upstream / /
ENV PATH=/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin LANG=C.UTF-8

FROM base AS wheels
WORKDIR /src
COPY packages/tooling /src/packages/tooling
RUN python -m pip wheel --no-cache-dir --disable-pip-version-check \
      --constraint=packages/tooling/runtime.lock --wheel-dir=/wheels ./packages/tooling

# The OPS extension wheel alone; its dependencies are the core wheels above.
FROM base AS ops-wheels
WORKDIR /src
COPY extensions/ops/pyproject.toml /src/extensions/ops/pyproject.toml
COPY extensions/ops/src /src/extensions/ops/src
RUN python -m pip wheel --no-cache-dir --disable-pip-version-check --no-deps \
      --wheel-dir=/wheels ./extensions/ops

FROM base AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /state tooling \
    && mkdir -p /state/tmp && chown -R tooling:tooling /state
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    python -m pip install --no-cache-dir --disable-pip-version-check --no-index \
      --find-links=/wheels kp-agent-tooling==0.4.0 \
    && rm -rf /root/.cache
COPY --chmod=755 deploy/image/entrypoint.sh /usr/local/bin/tooling-entrypoint
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/state TMPDIR=/state/tmp
USER 10001:10001
WORKDIR /state
ENTRYPOINT ["tooling-entrypoint"]
CMD ["--help"]
ARG SOURCE_REVISION=unknown
COPY --chmod=644 <<EOF /usr/local/share/agent-tooling-build-identity.json
{"schema_version": "agent-tooling.build-identity.v1", "source_revision": "${SOURCE_REVISION}"}
EOF
ARG SOURCE_URL
LABEL org.opencontainers.image.title="Agent Tooling: runtime" \
      org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision=${SOURCE_REVISION}

# Local providers: Node, Serena, SCIP/compiler toolchain, CPU embeddings, telemetry exporters.
FROM base AS providers
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && python -m venv /opt/serena \
    && /opt/serena/bin/pip install --no-cache-dir --disable-pip-version-check \
      'serena-agent @ git+https://github.com/oraios/serena.git@c6fbd1c5932df2494ffa0020af5a9fbe80b82143' \
    && rm -rf /root/.cache
COPY deploy/image/toolchain /opt/toolchain
RUN cd /opt/toolchain \
    && NODE_DISABLE_COMPILE_CACHE=1 npm ci --omit=dev --ignore-scripts --no-audit --no-fund --cache /tmp/npm-cache \
    && python -m pip install --no-cache-dir --disable-pip-version-check \
      --index-url https://download.pytorch.org/whl/cpu 'torch==2.8.0' \
    && python -m pip install --no-cache-dir --disable-pip-version-check \
      'sentence-transformers==5.1.0' 'transformers==4.56.2' \
      'opentelemetry-sdk==1.44.0' 'opentelemetry-exporter-otlp-proto-http==1.44.0' \
    && rm -rf /tmp/npm-cache /tmp/node-compile-cache /root/.npm /root/.cache

# The Kanban board: web UI and server bundle plus the native node-pty addon,
# compiled against the same Node that the product image runs.
FROM node AS kanban-build
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY apps/kanban/package*.json ./
ENV HUSKY=0
RUN npm ci
COPY apps/kanban/web-ui/package*.json ./web-ui/
RUN npm --prefix web-ui ci
COPY apps/kanban ./
# The approved intake contract is bundled by the CLI build from its authoritative source.
COPY docs/work/adr-backlog-intake-v1.2.schema.json /docs/work/adr-backlog-intake-v1.2.schema.json
RUN npm run build

# Shared content of product, agents and acceptance. SOURCE_REVISION is declared only
# in the final stages, so a new revision relabels them without rebuilding layers.
FROM providers AS product-base
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    --mount=type=bind,source=packages/tooling/runtime.lock,target=/tmp/runtime.lock \
    python -m pip install --no-cache-dir --disable-pip-version-check --no-index \
      --find-links=/wheels --constraint=/tmp/runtime.lock kp-agent-tooling==0.4.0 \
    && rm -rf /root/.cache \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /state tooling \
    && mkdir -p /state/tmp && chown -R tooling:tooling /state
COPY --from=kanban-build /app/package.json /app/package.json
COPY --from=kanban-build /app/node_modules/node-pty /app/node_modules/node-pty
COPY --from=kanban-build /app/dist /app/dist
COPY deploy/image/serena.yml /usr/local/share/ops-tooling/serena.yml
COPY --chmod=755 deploy/image/container.py /usr/local/bin/tooling-container
COPY --chmod=755 deploy/image/git-credential-file /usr/local/bin/git-credential-file
COPY --chmod=755 deploy/image/kanban /usr/local/bin/kanban
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/state TMPDIR=/state/tmp \
    PATH=/opt/toolchain/node_modules/.bin:/usr/local/bin:/usr/local/sbin:/usr/sbin:/usr/bin:/sbin:/bin \
    HF_HOME=/state/models HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 \
    NODE_PATH=/app/node_modules
# Board defaults. Every integration is an executable inside this image; none uses
# docker. The operator adds `--config <path>` to the desk registry command, sets
# KANBAN_SESSION_IMPORT_CONFIG, and adds `--binding <path>` to the assistant
# memory command (with KANBAN_ASSISTANT_MEMORY_WORKSPACE) to enable each one.
ENV KANBAN_STORAGE_ROOT=/state/kanban \
    KANBAN_PUBLIC_ORIGIN=http://127.0.0.1:3486 \
    KANBAN_NO_AUTO_UPDATE=1 \
    KANBAN_SESSION_IMPORT_EXECUTABLE=/usr/local/bin/kp-agent-session-import \
    KANBAN_DESK_REGISTRY_COMMAND='["/usr/local/bin/kp-agent-desk-registry"]' \
    KANBAN_ASSISTANT_MEMORY_COMMAND='["/usr/local/bin/python3","-m","kp_agent_tooling.assistant_host_cli"]'
USER 10001:10001
ENTRYPOINT ["tooling-container"]
CMD ["wait"]

FROM product-base AS product
# Each final stage declares SOURCE_REVISION, the value of its revision label, in
# /usr/local/share/agent-tooling-build-identity.json; tooling.identity reports it
# (origin image-build-declaration). Without the build arg it declares "unknown",
# which is no revision. Only this last file layer and the label change per revision.
# The file sits in an existing directory: --chmod would also apply to a directory
# COPY creates, leaving it untraversable.
ARG SOURCE_REVISION=unknown
COPY --chmod=644 <<EOF /usr/local/share/agent-tooling-build-identity.json
{"schema_version": "agent-tooling.build-identity.v1", "source_revision": "${SOURCE_REVISION}"}
EOF
ARG SOURCE_URL
LABEL org.opencontainers.image.title="Agent Tooling: product" \
      org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision=${SOURCE_REVISION}

# Opt-in agent CLIs at exact versions (deploy/image/agents/package-lock.json).
# Provider logins happen inside a running container and persist only under /state.
FROM product-base AS agents
ARG CLAUDE_CODE_VERSION=2.1.280
ARG CODEX_VERSION=0.159.2
USER root
COPY deploy/image/agents/package.json deploy/image/agents/package-lock.json /opt/agents/
RUN cd /opt/agents \
    && HOME=/tmp/agents-home TMPDIR=/tmp NODE_DISABLE_COMPILE_CACHE=1 \
       npm ci --omit=dev --no-audit --no-fund --cache /tmp/npm-cache \
    && test "$(node -p "require('./node_modules/@anthropic-ai/claude-code/package.json').version")" = "${CLAUDE_CODE_VERSION}" \
    && test "$(node -p "require('./node_modules/@openai/codex/package.json').version")" = "${CODEX_VERSION}" \
    && ln -s /opt/agents/node_modules/.bin/claude /usr/local/bin/claude \
    && ln -s /opt/agents/node_modules/.bin/codex /usr/local/bin/codex \
    && rm -rf /tmp/agents-home /tmp/npm-cache /tmp/node-compile-cache /root/.npm /root/.cache
ENV DISABLE_AUTOUPDATER=1
USER 10001:10001
ARG SOURCE_REVISION=unknown
COPY --chmod=644 <<EOF /usr/local/share/agent-tooling-build-identity.json
{"schema_version": "agent-tooling.build-identity.v1", "source_revision": "${SOURCE_REVISION}"}
EOF
ARG SOURCE_URL
LABEL org.opencontainers.image.title="Agent Tooling: product with agent CLIs" \
      org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.revision=${SOURCE_REVISION} \
      agent-tooling.claude-code.version=${CLAUDE_CODE_VERSION} \
      agent-tooling.codex.version=${CODEX_VERSION}

# Opt-in OpenCode CLI at an exact version (deploy/image/opencode/package-lock.json). Never a
# default: an installation uses this image only when the operator passes its digest. It is
# FROM product-base, not agents, so it carries no claude or codex; the OpenCode CLI is MIT
# (NOTICE), so this image may be published where the agents image may not.
# The binary is the lockfile's platform package for the build's architecture
# (opencode-linux-<arch>). The wrapper's postinstall links it into place, and would fall back
# to a networked install if that package were absent, so the build refuses unless the linked
# binary is that package's own file and answers the pin. Nothing is fetched at run time.
FROM product-base AS opencode
ARG OPENCODE_VERSION=1.18.34
USER root
COPY deploy/image/opencode/package.json deploy/image/opencode/package-lock.json \
     deploy/image/opencode/LICENSE /opt/opencode/
RUN cd /opt/opencode \
    && HOME=/tmp/opencode-home TMPDIR=/tmp NODE_DISABLE_COMPILE_CACHE=1 \
       npm ci --omit=dev --no-audit --no-fund --foreground-scripts --cache /tmp/npm-cache \
    && arch="$(node -p process.arch)" \
    && test "$(node -p "require('./node_modules/opencode-ai/package.json').version")" = "${OPENCODE_VERSION}" \
    && test -d "node_modules/opencode-linux-${arch}" \
    && test "$(node -p "require('./node_modules/opencode-linux-${arch}/package.json').version")" = "${OPENCODE_VERSION}" \
    && { [ node_modules/opencode-ai/bin/opencode.exe -ef "node_modules/opencode-linux-${arch}/bin/opencode" ] \
         || [ node_modules/opencode-ai/bin/opencode.exe -ef "node_modules/opencode-linux-${arch}-baseline/bin/opencode" ]; } \
    && test "$(HOME=/tmp/opencode-home TMPDIR=/tmp node_modules/.bin/opencode --version)" = "${OPENCODE_VERSION}" \
    && ln -s /opt/opencode/node_modules/.bin/opencode /usr/local/bin/opencode \
    && install -D -m 644 LICENSE /usr/local/share/doc/opencode/LICENSE \
    && rm LICENSE \
    && rm -rf /tmp/opencode /tmp/opencode-home /tmp/npm-cache /tmp/node-compile-cache /root/.npm /root/.cache
ENV OPENCODE_DISABLE_AUTOUPDATE=1
USER 10001:10001
ARG SOURCE_REVISION=unknown
COPY --chmod=644 <<EOF /usr/local/share/agent-tooling-build-identity.json
{"schema_version": "agent-tooling.build-identity.v1", "source_revision": "${SOURCE_REVISION}"}
EOF
ARG SOURCE_URL
LABEL org.opencontainers.image.title="Agent Tooling: product with the OpenCode CLI" \
      org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.licenses="AGPL-3.0-only AND MIT" \
      org.opencontainers.image.revision=${SOURCE_REVISION} \
      org.opencontainers.image.version=${OPENCODE_VERSION} \
      agent-tooling.opencode.version=${OPENCODE_VERSION}

# The `-ops` variant: product plus the extension distribution (kp-agent-tooling-ops),
# installed offline from wheels built from this tree. Its tool provider registers in
# the kp_agent_tooling.tools entry-point group, so `kp-agent-tooling … serve` lists
# the extension tools a configuration enables. No other target installs it.
FROM product-base AS ops
USER root
RUN --mount=type=bind,from=wheels,source=/wheels,target=/wheels \
    --mount=type=bind,from=ops-wheels,source=/wheels,target=/ops-wheels \
    --mount=type=bind,source=packages/tooling/runtime.lock,target=/tmp/runtime.lock \
    python -m pip install --no-cache-dir --disable-pip-version-check --no-index \
      --find-links=/wheels --find-links=/ops-wheels --constraint=/tmp/runtime.lock \
      kp-agent-tooling-ops==0.4.0 \
    && python -c 'import kp_agent_tooling_ops' \
    && rm -rf /root/.cache
USER 10001:10001
ARG SOURCE_REVISION=unknown
COPY --chmod=644 <<EOF /usr/local/share/agent-tooling-build-identity.json
{"schema_version": "agent-tooling.build-identity.v1", "source_revision": "${SOURCE_REVISION}"}
EOF
ARG SOURCE_URL
LABEL org.opencontainers.image.title="Agent Tooling: product with the OPS extension" \
      org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.licenses="AGPL-3.0-only" \
      org.opencontainers.image.revision=${SOURCE_REVISION}

# Test-only derivative: production images do not carry test runners or fixtures.
FROM product-base AS acceptance
USER root
RUN python -m pip install --no-cache-dir --disable-pip-version-check 'pytest>=8,<10' && rm -rf /root/.cache
COPY pytest.ini /acceptance/pytest.ini
COPY tests /acceptance/tests
COPY config /acceptance/config
COPY scripts /acceptance/scripts
USER 10001:10001
WORKDIR /acceptance
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["tests", "-q", "-p", "no:cacheprovider", "--basetemp", "/state/pytest"]
ARG SOURCE_REVISION=unknown
COPY --chmod=644 <<EOF /usr/local/share/agent-tooling-build-identity.json
{"schema_version": "agent-tooling.build-identity.v1", "source_revision": "${SOURCE_REVISION}"}
EOF
ARG SOURCE_URL
LABEL org.opencontainers.image.title="Agent Tooling: acceptance (test-only)" \
      org.opencontainers.image.source=${SOURCE_URL} \
      org.opencontainers.image.revision=${SOURCE_REVISION}
