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
# The one exception is `--target agent`: the AI agent service, a separate
# Node image deployed as a sidecar container beside this one (see its stage).

# The library pins the `libraries` stage bakes in (#169), global so that stage and
# the `app` stage's catalogue check read one value. See that stage.
# Moving BOSL2_REF/BOSL2_COMMIT or baking in another library: update THIRD_PARTY_NOTICES.md.
ARG BOSL2_REF=v2.0.761
ARG BOSL2_COMMIT=f47030c41d88d0676bca73be1c6b7ba58564f9dd

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

COPY frontend/ ./
RUN pnpm build

# ── agent: the AI sidecar (#261) ──────────────────────────────────────────────
# A SEPARATE image, reached with `--target agent` and deployed as a second
# container in the ScadBuddy pod — the sidecar layout the AI design spec picks
# in §4.1 (docs/superpowers/specs/2026-09-27-ai-integration-design.md): one
# process per container, no supervisor under tini, independent restarts. It
# shares nothing with the OpenSCAD stages below, and it sits ABOVE them so
# `runtime` stays the last stage and a bare `docker build .` still produces the
# backend image.
#
# Same Node major as the `frontend` stage and ci.yml, for the same reasons
# (see the comment on that stage); move all three together.
FROM node:24-bookworm-slim AS agent-build

WORKDIR /src/agent
RUN corepack enable

# agent/ has no pnpm-workspace.yaml because none of its dependencies has an
# install script to approve (a frozen install passes without one). If one ever
# does, pnpm fails here with ERR_PNPM_IGNORED_BUILDS: add the file with its
# `allowBuilds` entry and copy it in on this line, as the frontend stage does.
COPY agent/package.json agent/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile

COPY agent/ ./
RUN pnpm build

# Production dependencies only, installed from the same lockfile in a stage of
# their own so the shipped node_modules carries no eslint/vitest/typescript.
FROM node:24-bookworm-slim AS agent-deps

WORKDIR /src/agent
RUN corepack enable
COPY agent/package.json agent/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile --prod

FROM node:24-bookworm-slim AS agent

# tini for the same reason as the backend image: the Agent SDK spawns the
# Claude Code binary as a child process per query, and node as PID 1 does not
# reap orphans.
# hadolint ignore=DL3008
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
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
COPY --from=agent-build /src/agent/package.json ./
COPY --from=agent-build /src/agent/dist ./dist

# The Claude Code binary the Agent SDK bundles is pinned the way
# OPENSCAD_VERSION is: the SDK "runs the Claude Code binary"
# (https://code.claude.com/docs/en/agent-sdk/overview), so an SDK bump changes
# the harness underneath every query. This fails the build when either the
# SDK's declared `claudeCodeVersion` or the binary's own `--version` differs
# from the pin. Bump it together with the SDK version in agent/package.json.
# It runs against the node_modules that ship, for the platform being built.
ARG CLAUDE_CODE_VERSION=2.1.283
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

# ── uv ────────────────────────────────────────────────────────────────────────
# A FROM line, not `COPY --from=ghcr.io/astral-sh/uv:...`, so Dependabot's
# docker ecosystem sees the version and can bump it. The image is scratch-based
# and holds nothing but the two static binaries.
FROM ghcr.io/astral-sh/uv:0.12.18 AS uv

# ── base: OS packages, fonts, users ───────────────────────────────────────────
FROM openscad/openscad:dev AS base

# DL3008 (pin apt versions) is disabled repo-wide in .hadolint.yaml: the base is
# a rolling nightly on Debian trixie, so a pinned version here would break the
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
# measured against one nightly. `:dev` is a rolling tag, so a silent OpenSCAD
# swap would change render output with nothing anywhere reporting it. When this
# fires, re-verify §3 of the design spec against the new build and bump the
# default below in the same commit.
ARG OPENSCAD_VERSION=2026.09.23
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
# seeded ref is not the catalogue's, or the seed holds anything not listed.
ARG BOSL2_REF
COPY --from=libraries /opt/scadbuddy-libraries /app/libraries
COPY THIRD_PARTY_NOTICES.md /app/THIRD_PARTY_NOTICES.md
RUN python -m scadbuddy.library.library_seed verify /app/libraries "BOSL2=${BOSL2_REF}"

# ── test: the same tree plus dev dependencies ─────────────────────────────────
# `pytest -m requires_openscad` can only run here — a real openscad binary is
# what those tests are marked for, and it exists nowhere else in CI.
FROM app AS test

# Re-chown: this sync runs as root and rewrites both the venv and the cache.
RUN uv sync --frozen \
    && chown -R scadbuddy:scadbuddy /opt/venv /opt/uv-cache
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
CMD ["uvicorn", "scadbuddy.main:app", "--host", "0.0.0.0", "--port", "8080"]

# start-period covers uv's first import of the app; the interval is short
# because a wedged render worker is the failure this is meant to catch.
# JSON form, so this is an exec and not a shell: `curl --fail` already exits
# non-zero on a non-2xx, which is exactly the signal HEALTHCHECK reads.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD ["curl", "--fail", "--silent", "--show-error", "http://127.0.0.1:8080/healthz"]
