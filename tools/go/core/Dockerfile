# syntax=docker/dockerfile:1

# scadbuddy-core (#7667): ScadBuddy's API in Go. Built with the repo root as context:
#   docker build -f tools/go/core/Dockerfile .
# Published by .github/workflows/build-go-image.yml as ghcr.io/eh-homelab/scadbuddy-core.
# The golang tag is go.work's `toolchain`; tools/go/CLAUDE.md lists the pins that move together.
FROM --platform=$BUILDPLATFORM golang:1.26.9-bookworm@sha256:d9c68c2c51161e12fd77e4c6320687c9cd86e1af1e3ad6e6cd63ff970641453c AS build
ARG TARGETOS
ARG TARGETARCH
WORKDIR /src
COPY go.work* ./
COPY vendor/ vendor/
COPY tools/go/ tools/go/
RUN CGO_ENABLED=0 GOOS=$TARGETOS GOARCH=$TARGETARCH GOFLAGS=-mod=vendor \
    go build -trimpath -ldflags="-s -w" -o /out/scadbuddy-core ./tools/go/core

FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10002 --user-group --no-create-home --shell /usr/sbin/nologin core
COPY --from=build /out/scadbuddy-core /usr/local/bin/scadbuddy-core
# Not the Python API's uid (10001), which untrusted programs in its container run as.
USER 10002
EXPOSE 8080
ENTRYPOINT ["scadbuddy-core"]
