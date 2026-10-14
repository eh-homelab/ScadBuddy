# syntax=docker/dockerfile:1

# bambuddy-fs (#2686, spec 2026-10-13 §7.1): the library as a FUSE filesystem.
# Built with the repo root as context (it needs the workspace and the root vendor/):
#   docker build -f tools/go/bambuddy-fs/Dockerfile .
# Published by .github/workflows/build-go-image.yml as ghcr.io/eh-homelab/scadbuddy-bambuddy-fs.
#
# Tag plus index digest, as the root Dockerfile pins its base. The golang tag is
# go.work's `toolchain`; CLAUDE.md lists the five pins that move together.
FROM --platform=$BUILDPLATFORM golang:1.25.6-bookworm@sha256:f4490d7b261d73af4543c46ac6597d7d101b6e1755bcdd8c5159fda7046b6b3e AS build
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
    go build -trimpath -ldflags="-s -w" -o /out/bambuddy-fs ./tools/go/bambuddy-fs

# Not distroless: FUSE needs fusermount3, setuid root, which is what lets the
# unprivileged user below mount. Running it needs, from the container,
# /dev/fuse (`--device /dev/fuse`), CAP_SYS_ADMIN in the bounding set
# (`--cap-add SYS_ADMIN`), privilege escalation allowed (no
# `no-new-privileges`; in Kubernetes `allowPrivilegeEscalation: true`) and,
# under Docker's default AppArmor profile, `--security-opt apparmor:unconfined`
# (the profile denies mount). The mountpoint must be writable by uid 10001, or
# run with `--user 0`. See README.md, "Container image".
FROM debian:bookworm-slim@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587
# DL3008 (apt pins) is ignored in .hadolint.yaml, as for the root Dockerfile.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fuse3 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# user_allow_other: without it fusermount3 refuses `--allow-other` to anyone but root.
RUN sed -i 's/^#user_allow_other/user_allow_other/' /etc/fuse.conf \
    && grep -qx user_allow_other /etc/fuse.conf \
    && useradd --uid 10001 --user-group --create-home --shell /usr/sbin/nologin bambuddy \
    && install -d -o bambuddy -g bambuddy -m 0700 \
       /home/bambuddy/.local/state/bambuddy-fs /home/bambuddy/.cache/bambuddy-fs
COPY --from=build /out/bambuddy-fs /usr/local/bin/bambuddy-fs
# The state directory holds the unsent and refused writes, the only copy of a save
# ScadBuddy has not taken yet: never the container's writable layer. Mount a named
# volume (or a host directory uid 10001 owns) here; Docker copies this directory's
# owner into an empty named volume. Without one, Docker makes an anonymous volume,
# which `docker run --rm` deletes with the container. The cache directory is
# optional: without a volume, downloads start cold after a recreate.
VOLUME ["/home/bambuddy/.local/state/bambuddy-fs"]
USER bambuddy
ENTRYPOINT ["bambuddy-fs"]
CMD ["mount", "--help"]
