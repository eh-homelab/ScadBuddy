# syntax=docker/dockerfile:1

# scadbuddy-fleet: the agent fleet chat's MCP server (spec 2026-11-25-agent-fleet-messaging-design
# §6.6). Built with the repo root as context:
#   docker build -f tools/go/fleet/Dockerfile .
# Published by .github/workflows/build-go-image.yml as ghcr.io/eh-homelab/scadbuddy-fleet.
# The golang tag is go.work's `toolchain`; tools/go/CLAUDE.md lists the pins that move together.
FROM --platform=$BUILDPLATFORM golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY go.work* ./
COPY vendor/ vendor/
COPY tools/go/ tools/go/
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOFLAGS=-mod=vendor \
    go build -trimpath -ldflags="-s -w" -o /out/scadbuddy-fleet ./tools/go/fleet

FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10003 --user-group --no-create-home --shell /usr/sbin/nologin fleet
COPY --from=build /out/scadbuddy-fleet /usr/local/bin/scadbuddy-fleet
# Not the Python API's uid (10001) nor core's (10002).
USER 10003
EXPOSE 8080
ENTRYPOINT ["scadbuddy-fleet"]
