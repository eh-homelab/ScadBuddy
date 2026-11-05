# Go tools — notes for agents

These notes cover `tools/go/`; paths are repo-relative; the root `CLAUDE.md` has the repo-wide rules.

## Commands
Go tools (`tools/go/`, one `go.work` at the root, dependencies vendored in `vendor/`):

```bash
go work vendor                                   # after a go.mod change
scripts/generate-go-client.sh [--check]          # the ScadBuddy client; needs docker, uv, jq
cd tools/go/<m> && go vet ./... && go test -race ./...
docker build -f tools/go/bambuddy-fs/Dockerfile -t bambuddy-fs:dev .   # repo root as context
```

The generator vendors the client in module mode (`GOWORK=off go mod tidy && go mod
vendor`, for consumers outside the workspace) and then runs `go work vendor`; it never
runs `go work sync`, which would move a lagging client's `go.mod` past its own
`vendor/modules.txt` and edit bambuddy-fs's `go.mod` on a client-only change (#2686).
bambuddy-fs's mount tests need `/dev/fuse` and `fusermount3` (they skip without the
device). The Go toolchain is pinned in six places, moved together: `go.work`'s
`toolchain`, the golang tag in `tools/go/bambuddy-fs/Dockerfile`, and `GOTOOLCHAIN` in
`go-module-ci.yml`, in `ci.yml`'s two freshness steps and in its `go-client` job.


## Layout
- `tools/go/` — Go modules in one workspace (#2686): `go.work` and the root `vendor/`
  at the repo root, so every build is offline. `scadbuddy-client/` is the
  Kiota-generated client (`client/`, committed, plus its own `vendor/` for module-mode
  consumers); `bambuddy-fs/` is the FUSE mount of Bambuddy's library over it
  (`internal/api` the client wrapper, `internal/fsys` the tree, cache, writes and the
  unsent queue, `internal/telemetry` OTel; README there). Its unsent writes are the
  only copy of a save, under the state directory, never the cache.

## See also

- The Go client is the exception to the root "Generated API files"; Go CI rules: root "CI and caching rules".
