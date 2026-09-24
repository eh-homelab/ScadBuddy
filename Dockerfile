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

ENV SCADBUDDY_DATA_DIR=/data \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/scadbuddy

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
