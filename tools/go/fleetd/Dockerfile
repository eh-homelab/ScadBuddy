# syntax=docker/dockerfile:1

# fleetd (#7987, spec 2026-11-26-fleet-session-registry-and-rotator-design §6.6): the fleet's
# machine service. It is meant to run as a systemd user service on the machine itself
# (README.md); the image is how build-go-image.yml publishes every Go CLI.
# Built with the repo root as context (it needs the workspace and the root vendor/):
#   docker build -f tools/go/fleetd/Dockerfile .
# Published by .github/workflows/build-go-image.yml as ghcr.io/eh-homelab/scadbuddy-fleetd.
#
# Tag plus index digest, as the root Dockerfile pins its base. The golang tag is
# go.work's `toolchain`; tools/go/CLAUDE.md lists the pins that move together.
FROM --platform=$BUILDPLATFORM golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
# go.work*: there is no go.work.sum until a workspace dependency needs one.
COPY go.work* ./
COPY vendor/ vendor/
COPY tools/go/ tools/go/
# -mod=vendor is the workspace default with a root vendor/; said here so a
# missing package fails the build instead of reaching for the network.
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOFLAGS=-mod=vendor \
    go build -trimpath -ldflags="-s -w" -o /out/fleetd ./tools/go/fleetd

# git: fleetd runs `git -C <cwd>` for each session's branch, HEAD and remote.
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
# DL3008 (apt pins) is ignored in .hadolint.yaml, as for the root Dockerfile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10004 --user-group --no-create-home --shell /usr/sbin/nologin fleetd
COPY --from=build /out/fleetd /usr/local/bin/fleetd
# Not the Python API's uid (10001), core's (10002) nor the fleet server's (10003).
USER 10004
ENTRYPOINT ["fleetd"]
CMD ["--help"]
