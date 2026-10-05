# syntax=docker/dockerfile:1

# ScadBuddy ships as ONE image. The design spec's verified facts (the
# customizer-schema JSON shape, the per-triangle material index in the 3MF
# Manifold emits) were all measured against `openscad/openscad:dev`, so the
# runtime is that exact image with a Python backend layered on — not a Python
# base with OpenSCAD installed beside it.
#
# Stage order is deliberate: `runtime` is LAST so a bare `docker build .`
# produces the shippable image. `test` sits before it and is reached with
# `--target test`; it is the same tree plus the dev dependency group, which is
# the only place `pytest -m requires_openscad` can run, since a real `openscad`
# binary only exists inside this image.
#
# The exceptions are `--target agent`: the AI agent service, a separate Node image
# deployed as a sidecar container beside this one (see its stage), and
# `--target agent-durable`, its durable-sessions twin on Python.

# The library pins the `libraries` stage bakes in (#169), global so that stage and
# the `app` stage's catalogue check read one value. See that stage.
# Moving BOSL2_REF/BOSL2_COMMIT or baking in another library: update THIRD_PARTY_NOTICES.md.
ARG BOSL2_REF=v2.0.761
ARG BOSL2_COMMIT=f47030c41d88d0676bca73be1c6b7ba58564f9dd

# The Claude Code the two agent images bundle (`agent` through the TypeScript SDK,
# `agent-durable` through the Python one), global so both stages assert one value.
# Bump with @anthropic-ai/claude-agent-sdk in agent/package.json and claude-agent-sdk
# in agent-durable/pyproject.toml, in the same commit.
ARG CLAUDE_CODE_VERSION=2.1.283

# ── uv ────────────────────────────────────────────────────────────────────────
# A FROM line, not `COPY --from=ghcr.io/astral-sh/uv:...`, so Dependabot's
# docker ecosystem sees the version and can bump it. The image is scratch-based
# and holds nothing but the two static binaries.
FROM ghcr.io/astral-sh/uv:0.12.19 AS uv

# ── base: OS packages, fonts, users ───────────────────────────────────────────
# Pinned to a dated nightly by tag AND index digest (amd64 + arm64), so the base
# cannot move under a build. OpenSCAD's only stable release (2021.01) has no
# Manifold backend, so a nightly it has to be. Bump deliberately: tag, digest and
# OPENSCAD_VERSION below together, after re-verifying §3 of the design spec.
FROM openscad/openscad:dev.2026-09-28@sha256:992508950d86ed5ea6a6ed19934e7d65aa6b1959df69823666f575e9c1579b49 AS base

# DL3008 (pin apt versions) is disabled repo-wide in .hadolint.yaml: the base is
# a nightly on Debian trixie, so a pinned version here would break the
# build the first time trixie moves, which is the opposite of reproducibility.
#
# Fonts are runtime dependencies, not niceties — `text()` in a .scad silently
# falls back to a substitute face when the requested family is missing, so a
# keychain renders with the wrong glyph widths and the bounding box the
# acceptance test asserts moves. All four packages verified present on trixie
# (fonts-dejavu 2.37-8, fonts-noto-core 20201225-2, fonts-lobster 2.0-2.1,
# fonts-lobstertwo 2.0-2.1).
#
# `libpq5` is the Postgres client library the render queue's store talks through
# (backend/scadbuddy/render/pg_store.py, SCADBUDDY_DATABASE_URL). The runtime
# installs plain `psycopg`, which loads it from here, rather than psycopg's
# binary wheel with its own bundled libpq and OpenSSL, so their security fixes
# come with this layer's apt packages. It is loaded at import, so it is needed
# even when no database is configured.
#
# `git` is a runtime dependency too, not tooling: the models directory on the data
# volume IS a git repository (backend/scadbuddy/library/history.py), and every
# upload, edit, restore and delete is a commit in it. Without the binary the app
# still serves models, but the history API answers 503 and nothing is versioned.
# A build-time FLOOR is asserted below — deliberately a floor and not a pin like
# OPENSCAD_VERSION, for the opposite reason that one exists.
#
# TRAP, measured in this image: there is NO family called "Lobster". Debian's
# `fonts-lobster` ships /usr/share/fonts/opentype/lobster/lobster.otf, whose
# internal family name is "Lobster Two" (style "Bold Italic"), so `fc-list`
# reports exactly two script family names — "Lobster Two" and nothing else. A
# .scad asking for font="Lobster" gets a silent DejaVu substitution, which
# changes glyph widths and therefore the bounding box. Script text must ask for
# "Lobster Two".
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        fontconfig \
        fonts-dejavu \
        fonts-lobster \
        fonts-lobstertwo \
        fonts-noto-core \
        git \
        libpq5 \
        python3 \
        python3-venv \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=uv /uv /uvx /usr/local/bin/

# Non-root, with a real writable HOME. OpenSCAD and fontconfig both want one:
# fontconfig writes its cache under $HOME/.cache when the system cache misses,
# and a read-only HOME turns that into a per-render warning storm.
RUN groupadd --gid 10001 scadbuddy \
    && useradd --uid 10001 --gid 10001 --create-home --home-dir /home/scadbuddy --shell /usr/sbin/nologin scadbuddy \
    && install -d -o scadbuddy -g scadbuddy /data /app

# Build the system font cache as root so the runtime user never has to, and so
# `fc-list` (the font dropdown in the customizer) answers immediately.
RUN fc-cache --force --system-only

# ── api-spec: the OpenAPI spec the clients are typed against ──────────────────
# backend/openapi.json and both schema.d.ts files are not committed (#492). The
# spec is exported here, off `base`, with the backend's own interpreter and
# locked dependencies, and the `frontend` and `agent-build` stages copy it in to
# generate their clients (their `pnpm gen:api` reads SCADBUDDY_OPENAPI_JSON).
# That is why `uv` and `base` sit above the Node stages: a stage can only copy
# from one defined before it.
FROM base AS api-spec

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_LINK_MODE=copy

WORKDIR /src/backend
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# The venv's python, not `uv run`: that would sync the dev group too.
COPY backend/ ./
RUN uv sync --frozen --no-dev \
    && /opt/venv/bin/python -m scadbuddy.tools.export_openapi /src/openapi.json

# ── frontend bundle ───────────────────────────────────────────────────────────
# Built here rather than copied from the host so a stale local `frontend/dist`
# can never reach the image (.dockerignore drops it from the context too).
# Node MAJORS here are LTS-only, and that is a constraint rather than a
# preference. Odd-numbered releases (25, 27, ...) never become LTS, and they do
# not ship corepack -- which the next line depends on. Installing it from npm
# does not rescue them either: corepack 0.36 declares
# `node: ^22.22.2 || ^24.15.0 || >=26.0.0`, so npm refuses node 25 outright.
# Dependabot bumped this 24 -> 25 in #55 and broke every build on main; that is
# now excluded in .github/dependabot.yml. Move it deliberately, to the next
# EVEN major, together with ci.yml's `node-version` (they must not diverge --
# a mismatch passes the frontend job and fails only in the image).
FROM node:24-bookworm-slim AS frontend

WORKDIR /src/frontend

# corepack reads the `packageManager` field in package.json, so the pnpm
# version is pinned by the frontend's own lockfile rather than by this file.
RUN corepack enable

# Manifest + lockfile + workspace config first: `pnpm install` is the expensive
# layer and only these can invalidate it.
#
# pnpm-workspace.yaml is NOT optional and is easy to leave out. pnpm 10+ refuses
# to silently skip a dependency's build scripts — it hard-errors with
# ERR_PNPM_IGNORED_BUILDS — and the approvals live in that file
# (`allowBuilds: {esbuild, msw}`), not in package.json. Copying only the
# manifest and lockfile produced an install that worked in the `frontend` CI job
# (whole tree checked out) and failed only here, which is the worst shape for
# this class of bug. Verified on CI run 35820566117.
COPY frontend/package.json frontend/pnpm-lock.yaml frontend/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

# After the install, so an API change does not re-download node_modules.
COPY --from=api-spec /src/openapi.json /src/openapi.json
ENV SCADBUDDY_OPENAPI_JSON=/src/openapi.json

COPY frontend/ ./
# The browser's `service.version` (tracing spec 2026-10-01 §3): the same label the
# runtime stage gets. Declared here, after the copy, so a new version reruns only this
# build step. build-image.yml passes it; ci.yml's builds keep the default.
ARG SCADBUDDY_VERSION=dev
RUN VITE_SCADBUDDY_VERSION="${SCADBUDDY_VERSION}" pnpm build

# ── agent: the AI sidecar (#261) ──────────────────────────────────────────────
# A SEPARATE image, reached with `--target agent` and deployed as a second
# container in the ScadBuddy pod — the sidecar layout the AI design spec picks
# in §4.1 (docs/superpowers/specs/2026-09-27-ai-integration-design.md): one
# process per container, no supervisor under tini, independent restarts. It
# shares nothing with the OpenSCAD stages except the exported spec (`api-spec`),
# and it sits above `app` so `runtime` stays the last stage and a bare
# `docker build .` still produces the backend image.
#
# Same Node major as the `frontend` stage and ci.yml, for the same reasons
# (see the comment on that stage); move all three together.
FROM node:24-bookworm-slim AS agent-build

WORKDIR /src/agent
RUN corepack enable

# agent/pnpm-workspace.yaml holds `allowBuilds` (msw, a test dependency, has an
# install script that is declined there); without it the frozen install fails
# with ERR_PNPM_IGNORED_BUILDS, so copy it with the lockfile, as the frontend
# stage does.
COPY agent/package.json agent/pnpm-lock.yaml agent/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile

COPY --from=api-spec /src/openapi.json /src/openapi.json
ENV SCADBUDDY_OPENAPI_JSON=/src/openapi.json

COPY agent/ ./
# ScadBuddy's own plugin's skills and subagents (#896): agent/plugins/scadbuddy
# links to them, and `pnpm build` copies the authoring skill into dist/docs for
# `scadbuddy://docs/authoring` (#252, agent/src/tools/guide.ts).
COPY plugins/scadbuddy/skills /src/plugins/scadbuddy/skills
COPY plugins/scadbuddy/agents /src/plugins/scadbuddy/agents
RUN pnpm build
# COPY keeps a symlink as a link, which would dangle in the agent stage:
# replace the links with the files (agent/src/harness/ownPlugin.ts), readable
# by the agent's non-root user whatever modes the build context had.
RUN cp -rL plugins/scadbuddy /tmp/own-plugin \
    && rm -r plugins/scadbuddy \
    && mv /tmp/own-plugin plugins/scadbuddy \
    && chmod -R a+rX plugins/scadbuddy \
    && test -z "$(find plugins -type l)" \
    && test -f plugins/scadbuddy/skills/customize/SKILL.md

# Production dependencies only, installed from the same lockfile in a stage of
# their own so the shipped node_modules carries no eslint/vitest/typescript.
FROM node:24-bookworm-slim AS agent-deps

WORKDIR /src/agent
RUN corepack enable
COPY agent/package.json agent/pnpm-lock.yaml agent/pnpm-workspace.yaml ./
RUN pnpm install --frozen-lockfile --prod

FROM node:24-bookworm-slim AS agent

# tini for the same reason as the backend image: the Agent SDK spawns the
# Claude Code binary as a child process per query, and node as PID 1 does not
# reap orphans. git (and ca-certificates for its https) fetches plugin
# packages at their pinned commit (#297, agent/src/plugins/packages/git.ts).
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Numeric uid 10001, the same as the backend image, so one pod
# securityContext covers both containers. The state directory is the only
# writable tree the service needs (agent/src/harness/options.ts
# DEFAULT_STATE_DIR): `claude/` is CLAUDE_CONFIG_DIR, `work/` the scratch cwd.
# Mount an emptyDir (or the data volume) there and the root filesystem can be
# read-only (spec §4.4).
RUN groupadd --gid 10001 scadbuddy \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/scadbuddy-agent --shell /usr/sbin/nologin scadbuddy \
    && install -d -o 10001 -g 10001 /var/lib/scadbuddy-agent /var/lib/scadbuddy-agent/claude /var/lib/scadbuddy-agent/work

WORKDIR /app/agent
COPY --from=agent-deps /src/agent/node_modules ./node_modules

# The headless browser (#349, AI spec D11 and §5.3): the Chromium build the
# pinned @playwright/mcp's own `playwright-core` expects, installed at build
# time with its system libraries, so nothing is downloaded at runtime (the
# upstream plugin's `npx @playwright/mcp@latest` is what spec D11 rejects).
# `install-browser` is @playwright/mcp's cli.js passing through to
# `playwright install`. `--only-shell` installs chromium-headless-shell alone,
# which is what a headless launch without a `channel` uses
# (agent/src/harness/headlessBrowser.ts `playwrightConfig`); measured on
# 0.0.82: 603 MB for it and its libraries, against 740 MB for full Chromium.
# Bump with @playwright/mcp in agent/package.json.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers
RUN node node_modules/@playwright/mcp/cli.js install-browser --with-deps --only-shell chromium \
    && rm -rf /var/lib/apt/lists/*

COPY --from=agent-build /src/agent/package.json ./
COPY --from=agent-build /src/agent/dist ./dist
# The vendored plugin manifest (agent/plugins/playwright/README.md); each
# session gets a copy of it with its own `.mcp.json` (headlessBrowser.ts).
COPY --from=agent-build /src/agent/plugins ./plugins

# The Claude Code binary the Agent SDK bundles is pinned the way
# OPENSCAD_VERSION is: the SDK "runs the Claude Code binary"
# (https://code.claude.com/docs/en/agent-sdk/overview), so an SDK bump changes
# the harness underneath every query. This fails the build when either the
# SDK's declared `claudeCodeVersion` or the binary's own `--version` differs
# from the pin. Bump it together with the SDK version in agent/package.json.
# It runs against the node_modules that ship, for the platform being built.
ARG CLAUDE_CODE_VERSION
RUN node dist/check-cli-version.js "$CLAUDE_CODE_VERSION"
ENV CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    NODE_ENV=production \
    HOME=/var/lib/scadbuddy-agent \
    CLAUDE_CONFIG_DIR=/var/lib/scadbuddy-agent/claude

USER 10001:10001
EXPOSE 8081

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "dist/main.js"]

# No curl in this image; node's fetch is the probe. Exec form, like the
# backend's, so the exit status is the signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:8081/healthz').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]

# ── agent-durable: durable agent sessions on Temporal (spec 2026-10-01 §6.2, #1056) ──
# A sidecar in the ScadBuddy pod, trusted like `agent` (#1030). Python on its own
# slim base: the "no Python in the base image" rule is about the OpenSCAD image.
FROM python:3.12-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3 AS agent-durable
# git: uv fetches the pinned ai-integrations commit (spec §6.2). libpq5: psycopg's
# libpq (the dev group's psycopg[binary] is not installed here). tini reaps the
# Claude Code processes the plugin spawns per segment.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates git libpq5 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv
# Numeric uid 10001 as in `agent`. The state directory is the only writable tree:
# `claude/` is CLAUDE_CONFIG_DIR. Mount an emptyDir there and the root filesystem can
# be read-only; a mount hides the directories made here, which is fine: Claude Code
# creates a missing CLAUDE_CONFIG_DIR itself (measured on 2.1.283). /srv/agent is
# the segments' cwd and is never written.
RUN groupadd --gid 10001 scadbuddy \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /var/lib/scadbuddy-agent-durable --shell /usr/sbin/nologin scadbuddy \
    && install -d -o 10001 -g 10001 /var/lib/scadbuddy-agent-durable /var/lib/scadbuddy-agent-durable/claude /srv/agent
WORKDIR /app/agent-durable
ENV UV_PROJECT_ENVIRONMENT=/app/agent-durable/.venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
COPY agent-durable/pyproject.toml agent-durable/uv.lock agent-durable/.python-version ./
RUN uv sync --frozen --no-dev
COPY agent-durable/scadbuddy_durable ./scadbuddy_durable
COPY agent-durable/scripts ./scripts
# The tool manifest and the prompt policy from the agent build (phase 4).
COPY --from=agent-build /src/agent/dist/tools.json ./tools.json
COPY --from=agent-build /src/agent/dist/durable-prompt.txt ./durable-prompt.txt
# Skills only (§6.3b): no agents/, no .mcp.json. The skills are copied from
# plugins/scadbuddy/skills, not agent-durable/plugin/skills (a symlink to them), so
# no link reaches the image.
COPY agent-durable/plugin/.claude-plugin ./plugin/.claude-plugin
COPY plugins/scadbuddy/skills ./plugin/skills
RUN chmod -R a+rX ./plugin && test -f plugin/skills/customize/SKILL.md && test -z "$(find plugin -type l)"
# The Claude Code the Python SDK bundles, asserted like the agent stage's.
ARG CLAUDE_CODE_VERSION
RUN .venv/bin/python scripts/check_cli_version.py "$CLAUDE_CODE_VERSION"
# PYTHONPATH: the package is not installed (`package = false`) and the cwd is /srv/agent.
ENV PATH=/app/agent-durable/.venv/bin:$PATH \
    PYTHONPATH=/app/agent-durable \
    CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION} \
    HOME=/var/lib/scadbuddy-agent-durable \
    CLAUDE_CONFIG_DIR=/var/lib/scadbuddy-agent-durable/claude \
    SCADBUDDY_AGENT_TOOLS_MANIFEST=/app/agent-durable/tools.json
USER 10001:10001
WORKDIR /srv/agent
EXPOSE 8082
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "scadbuddy_durable.worker"]
# No curl in this image; the stdlib is the probe. Exec form, so the exit status is the signal.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8082/healthz', timeout=4).status == 200 else 1)"]

# ── openscad-lsp: the editor's language server ────────────────────────────────
# Completion, hover and go-to-definition in the source editor (#95), bridged to
# the browser by backend/scadbuddy/library/lsp.py. Upstream publishes prebuilt
# linux-gnu binaries for both architectures build-image.yml targets, so nothing
# is compiled here. The checksums are pinned in this file rather than read off
# the release, so a replaced asset fails the build instead of shipping; bump all
# three ARGs together.
#
# Its own stage, off `base`, so xz-utils (to unpack the .tar.xz) never reaches
# the runtime, and so `--version` below runs against the same glibc the runtime
# has — a binary linked against a newer one fails HERE, not on the first editor.
FROM base AS openscad-lsp

ARG TARGETARCH
ARG OPENSCAD_LSP_VERSION=2.0.1
ARG OPENSCAD_LSP_SHA256_AMD64=e51b7f84180d93a65387d3bbd00bb47ea1953af27d637c3698800f1b671005ea
ARG OPENSCAD_LSP_SHA256_ARM64=6e5f572bbbd193a5a1b7f538b4fea0ef5f082a9cafb3f8e978dd86905e3bfb9d

# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends xz-utils \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp/openscad-lsp
# The checksum goes through a file, not `echo | sha256sum`, for the same DL4006
# reason the version checks further down avoid pipes.
RUN case "$TARGETARCH" in \
      amd64) triple=x86_64-unknown-linux-gnu; sha="$OPENSCAD_LSP_SHA256_AMD64" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; sha="$OPENSCAD_LSP_SHA256_ARM64" ;; \
      *) echo "ERROR: openscad-lsp publishes no build for '${TARGETARCH}'." >&2; exit 1 ;; \
    esac \
    && curl --fail --silent --show-error --location --output openscad-lsp.tar.xz \
         "https://github.com/Leathong/openscad-LSP/releases/download/v${OPENSCAD_LSP_VERSION}/openscad-lsp-${triple}.tar.xz" \
    && printf '%s  openscad-lsp.tar.xz\n' "$sha" > openscad-lsp.sha256 \
    && sha256sum --check --strict openscad-lsp.sha256 \
    && tar -xJf openscad-lsp.tar.xz --strip-components=1 \
    && install -m 0755 openscad-lsp /usr/local/bin/openscad-lsp \
    && openscad-lsp --version

# ── libraries: the catalogue's common libraries, baked in (#169) ──────────────
# A fresh install renders a BOSL2 model offline: at boot the backend copies each
# checkout here onto the volume if it is not there yet
# (backend/scadbuddy/library/library_seed.py). Laid out as the volume lays out
# checkouts, `<name>/<commit>/<name>/`, without `.git`.
#
# The ref is the curated catalogue's (CURATED in
# backend/scadbuddy/library/libraries.py; the `app` stage fails the build when
# they differ), and the commit it resolves to is pinned here the way
# OPENSCAD_VERSION is: a tag moved upstream fails the build rather than shipping a
# different tree under the same commit's name. Bump the pair together with the
# catalogue's ref.
#
# Only BOSL2, by size: its checkout is ~12 MB. dotSCAD (~17 MB) and NopSCADlib
# (~44 MB) are left to be cloned when pinned; Round-Anything's ~8 MB is almost all
# a demo STL; MCAD already ships in the base image
# (/usr/local/share/openscad/libraries) and its catalogue ref is a branch, which
# would fail this check on every upstream commit. Each seeded library costs its
# size twice: once in the image, once on each volume.
FROM base AS libraries

ARG BOSL2_REF
ARG BOSL2_COMMIT

WORKDIR /opt/scadbuddy-libraries
RUN git -c advice.detachedHead=false clone --quiet --depth 1 --branch "$BOSL2_REF" \
        https://github.com/BelfrySCAD/BOSL2.git "BOSL2/${BOSL2_COMMIT}/BOSL2" \
    && actual="$(git -C "BOSL2/${BOSL2_COMMIT}/BOSL2" rev-parse HEAD)" \
    && if [ "$actual" != "$BOSL2_COMMIT" ]; then \
         echo "ERROR: BOSL2 '${BOSL2_REF}' now resolves to '${actual}', this build pins '${BOSL2_COMMIT}'." >&2; \
         echo "       Check what moved the tag upstream, then bump BOSL2_COMMIT." >&2; \
         exit 1; \
       fi \
    && rm -rf "BOSL2/${BOSL2_COMMIT}/BOSL2/.git"

# ── app: dependencies, backend, models, frontend bundle ───────────────────────
FROM base AS app

# `--version` goes to STDERR, not stdout — `$(openscad --version)` captures an
# empty string, which is how a version check silently passes against nothing.
#
# The assertion is deliberate and it is meant to break the build. Every
# structural fact the render pipeline depends on (the .param schema fields, the
# basematerials + per-triangle `p1` index, the `displaycolor` alpha quirk) was
# measured against one nightly. The base is pinned by digest, so this should
# never fire; it stays as the check that the tag, digest and version agree. When
# bumping the base, re-verify §3 of the design spec against the new build and
# change the FROM line and the default below in the same commit.
ARG OPENSCAD_VERSION=2026.09.28
# Written to a file rather than piped into sed: every `run:`-style pipe here
# trips hadolint's DL4006, and `SHELL -o pipefail` for one command is a worse
# trade than a temp file.
RUN openscad --version > /tmp/openscad-version 2>&1 \
    && actual="$(sed -n 's/^OpenSCAD version //p' /tmp/openscad-version)" \
    && rm -f /tmp/openscad-version \
    && if [ "$actual" != "$OPENSCAD_VERSION" ]; then \
         echo "ERROR: base image carries OpenSCAD '${actual}', this build declares '${OPENSCAD_VERSION}'." >&2; \
         echo "       Re-verify the render pipeline against the new build, then bump OPENSCAD_VERSION." >&2; \
         exit 1; \
       fi
ENV OPENSCAD_VERSION=${OPENSCAD_VERSION}

# git, unlike OpenSCAD, is asserted as a MINIMUM and not pinned to an exact
# build. The OpenSCAD assertion exists because every structural fact the render
# pipeline depends on was MEASURED against one nightly, so any drift has to
# break the build. Nothing here was measured off a git build: `history.py`
# depends on three dated, documented CLI contracts, and a floor is what actually
# states them — while a pin would break this image on every ordinary trixie git
# bump, which is noise rather than signal.
#
#   2.9     core.hooksPath          (hooks disabled on every invocation)
#   2.28    --initial-branch        (`init.defaultBranch` lives in the global
#                                    config this module refuses to read)
#   2.35.2  safe.directory as PROTECTED command-line scope — the one that makes
#           a PVC whose ownership does not match uid 10001 usable at all
#   2.37    http.curloptResolve     (libraries.py holds a library clone to the
#                                    addresses it vetted; older git ignores the
#                                    key and would resolve the host again)
#
# No pipes, for the same reason the OpenSCAD check above uses a temp file: every
# pipe in a RUN trips hadolint's DL4006, and `SHELL -o pipefail` for one command
# is the worse trade. `sort -V` reads and writes files here instead.
ARG MIN_GIT_VERSION=2.37
RUN git --version > /tmp/git-version \
    && actual="$(sed -n 's/^git version //p' /tmp/git-version)" \
    && actual="${actual%% *}" \
    && printf '%s\n%s\n' "$MIN_GIT_VERSION" "$actual" > /tmp/git-versions \
    && sort -V /tmp/git-versions > /tmp/git-sorted \
    && lowest="$(sed -n 1p /tmp/git-sorted)" \
    && rm -f /tmp/git-version /tmp/git-versions /tmp/git-sorted \
    && if [ "$lowest" != "$MIN_GIT_VERSION" ]; then \
         echo "ERROR: base image carries git '${actual}'; history.py and libraries.py need >= ${MIN_GIT_VERSION}." >&2; \
         echo "       See the version floors listed above this check." >&2; \
         exit 1; \
       fi

# UV_LINK_MODE=copy: the cache and the venv are on different layers, so uv's
# default hardlink strategy warns on every package.
# UV_PYTHON_INSTALL_DIR is set because UV_PYTHON_DOWNLOADS is left at its
# default (`automatic`) on purpose: trixie ships Python 3.13 and the backend
# targets 3.12, so if its `requires-python` excludes 3.13, uv fetches a
# matching interpreter rather than failing the build. Pointing the install dir
# at a fixed path keeps whatever it fetched inside the image.
# UV_CACHE_DIR is pinned outside $HOME on purpose. uv defaults it to
# ~/.cache/uv; the layers below run as root, so that cache would be created
# root-owned under the runtime user's HOME, and the first `uv run` as uid 10001
# then dies with "failed to open file ... Permission denied" — a failure that
# only appears when something invokes uv at RUNTIME, i.e. in the test stage,
# never in a plain image build.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_CACHE_DIR=/opt/uv-cache \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app/backend

# Lockfile layer: dependencies resolve from pyproject.toml + uv.lock alone, so
# editing backend source does not re-download the dependency tree.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY backend/ ./
RUN uv sync --frozen --no-dev

# The seed catalogue and the bundle the backend serves. Models live in the
# image (read-only, versioned with the repo); user uploads live on the PVC
# under SCADBUDDY_DATA_DIR.
COPY models/ /app/models/
COPY --from=frontend /src/frontend/dist /app/frontend/dist

RUN chown -R scadbuddy:scadbuddy /app /opt/venv /opt/uv-cache

# Last of the app layers, after the dependency sync and the chown: bumping the
# openscad-lsp ARGs then rebuilds this one copy and nothing above it. Outside
# /app, so the chown has nothing to do with it; `test` and `runtime` both
# inherit it from here.
COPY --from=openscad-lsp /usr/local/bin/openscad-lsp /usr/local/bin/openscad-lsp

ENV SCADBUDDY_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/scadbuddy

# The baked-in libraries (see the `libraries` stage), after everything above so a
# bump rebuilds only these two layers. Root-owned and read-only: the boot copies
# them onto the volume and never writes here. The check fails the build when a
# seeded ref is not the catalogue's, the seed holds anything not listed, or
# THIRD_PARTY_NOTICES.md does not name each seeded library's ref and commit.
ARG BOSL2_REF
COPY --from=libraries /opt/scadbuddy-libraries /app/libraries
COPY THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md
RUN python -m scadbuddy.library.library_seed verify /app/libraries \
        /app/THIRD_PARTY_NOTICES.md "BOSL2=${BOSL2_REF}"

# ── test: the same tree plus dev dependencies ─────────────────────────────────
# `pytest -m requires_openscad` can only run here — a real openscad binary is
# what those tests are marked for, and it exists nowhere else in CI.
FROM app AS test

# Re-chown: this sync runs as root and rewrites both the venv and the cache.
RUN uv sync --frozen \
    && chown -R scadbuddy:scadbuddy /opt/venv /opt/uv-cache

# The Temporal CLI's dev server backs the `requires_temporal` tests
# (tests/support/temporal.py). Pinned by version and per-arch digest like openscad-lsp
# above (the release's checksums.txt). ci.yml's `agent` job installs the same version
# and amd64 digest for the agent's Temporal tests (#1055); bump them together.
ARG TARGETARCH
ARG TEMPORAL_CLI_VERSION=1.9.1
ARG TEMPORAL_CLI_SHA256_AMD64=09a0326a51db84d02735e53542b9ebd8c4758daf47482a9ab0abce15844e60d5
ARG TEMPORAL_CLI_SHA256_ARM64=6c57c352d52fc3df34412376fd9ba6f74b7e3ace8e426e6cba8600156d36a145
RUN case "$TARGETARCH" in \
      amd64) sha="$TEMPORAL_CLI_SHA256_AMD64" ;; \
      arm64) sha="$TEMPORAL_CLI_SHA256_ARM64" ;; \
      *) echo "ERROR: no pinned Temporal CLI for '${TARGETARCH}'." >&2; exit 1 ;; \
    esac \
    && curl --fail --silent --show-error --location --output /tmp/temporal-cli.tar.gz \
        "https://github.com/temporalio/cli/releases/download/v${TEMPORAL_CLI_VERSION}/temporal_cli_${TEMPORAL_CLI_VERSION}_linux_${TARGETARCH}.tar.gz" \
    && printf '%s  /tmp/temporal-cli.tar.gz\n' "$sha" > /tmp/temporal-cli.sha256 \
    && sha256sum --check --strict /tmp/temporal-cli.sha256 \
    && tar -xzf /tmp/temporal-cli.tar.gz -C /usr/local/bin temporal \
    && rm -f /tmp/temporal-cli.tar.gz /tmp/temporal-cli.sha256 \
    && temporal --version
ENV SCADBUDDY_TEST_TEMPORAL_DEV_SERVER=/usr/local/bin/temporal

# Numeric, not `scadbuddy`: a name is unresolvable to anything outside this
# image, and Kubernetes' `runAsNonRoot` admission check can only read a uid.
USER 10001:10001
CMD ["uv", "run", "--frozen", "pytest"]

# ── runtime: what ships ───────────────────────────────────────────────────────
FROM app AS runtime

# Build provenance, passed by build-image.yml. /healthz reports both; the
# deploy pipeline (see README.md, "Deploying") proves a rollout by reading
# `revision` back from the running pod, so it has to be the exact commit.
ARG SCADBUDDY_REVISION=unknown
ARG SCADBUDDY_VERSION=dev
ENV SCADBUDDY_REVISION=${SCADBUDDY_REVISION} \
    SCADBUDDY_VERSION=${SCADBUDDY_VERSION}

USER 10001:10001
EXPOSE 8080
VOLUME ["/data"]

# tini reaps the openscad children the render runner forks. Without it PID 1 is
# uvicorn, which does not reap, and a killed-on-timeout openscad stays a zombie
# for the life of the pod.
ENTRYPOINT ["/usr/bin/tini", "--"]
# --ws-max-size bounds one inbound WebSocket message before the app sees it (uvicorn's
# default is 16 MiB: https://www.uvicorn.org/settings/#implementation). It is the same
# 8 MiB ceiling `api/limits.py` puts on a text body, because the LSP bridge carries a
# whole source (up to MAX_SOURCE_CHARS) in one message; `/api/v1/ws` caps its own
# frames far lower in the app (`api/realtime.py` MAX_FRAME_CHARS).
# A factory, not a module-level app: building one reads Settings, which refuses to
# start without SCADBUDDY_DATABASE_URL (#401) or SCADBUDDY_TEMPORAL_ADDRESS (#546),
# and importing the module must not.
# The render worker (#424) is this same image run as `python -m scadbuddy.worker`: it
# serves /healthz and /metrics on 9090 (probe that, not the HEALTHCHECK below, which
# is the API's 8080). Phase 1 runs one replica, sharing /data with the API.
# SIGTERM starts its drain (tini forwards it; no preStop needed): it polls until no
# workflow pinned to its build is running (or, after 30 s, until it sees its build is
# still current: a restart of the same build), for at most 2 x (SCADBUDDY_RENDER_TIMEOUT
# + 60) + 120 s, then gives its running activities SCADBUDDY_RENDER_TIMEOUT + 60 s.
# terminationGracePeriodSeconds must cover both: 3 x (RENDER_TIMEOUT + 60) + 120,
# plus a little slack for teardown (e.g. 30 s): 690 s at the default.
# A new build must become current before the old pod drains, so roll it out with
# RollingUpdate and maxSurge >= 1: under Recreate the old build stays current while it
# drains, and runs submitted then are pinned to a build no pod serves afterwards.
# --no-proxy-headers: uvicorn would otherwise believe X-Forwarded-For/-Proto from
# 127.0.0.1 (its default FORWARDED_ALLOW_IPS) and rewrite the request's client before the
# app sees it, so an in-pod caller (kubectl port-forward, a sidecar) could name any
# client. SCADBUDDY_TRUSTED_PROXIES (`core/proxies.py`) is the only trust decision.
CMD ["uvicorn", "--factory", "scadbuddy.main:create_app", "--host", "0.0.0.0", "--port", "8080", "--ws-max-size", "8388608", "--no-proxy-headers"]

# start-period covers uv's first import of the app; the interval is short
# because a wedged render worker is the failure this is meant to catch.
# JSON form, so this is an exec and not a shell: `curl --fail` already exits
# non-zero on a non-2xx, which is exactly the signal HEALTHCHECK reads.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8080/healthz"]
