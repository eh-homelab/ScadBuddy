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
device). core's tests need `SCADBUDDY_TEST_DATABASE_URL` (they skip without it;
CI sets `SCADBUDDY_TEST_POSTGRES_REQUIRED`). The Go toolchain is pinned in these places, moved together: `go.work`'s
`toolchain`, the golang tag in `tools/go/bambuddy-fs/Dockerfile`, `tools/go/core/Dockerfile`, `tools/go/fleet/Dockerfile` and `tools/go/fleetd/Dockerfile`, and `GOTOOLCHAIN` in
`go-module-ci.yml`, in `ci.yml`'s two freshness steps and in its `go-client` job.


## Layout
- `tools/go/` — Go modules in one workspace (#2686): `go.work` and the root `vendor/`
  at the repo root, so every build is offline. `scadbuddy-client/` is the
  Kiota-generated client (`client/`, committed, plus its own `vendor/` for module-mode
  consumers); `bambuddy-fs/` is the FUSE mount of Bambuddy's library over it
  (`internal/api` the client wrapper, `internal/fsys` the tree, cache, writes and the
  unsent queue, `internal/telemetry` its spans and metrics; README there). Its unsent
  writes are the only copy of a save, under the state directory, never the cache. `core/`
  is the API in Go (#7667): the areas it has, over Postgres, and a forward of the rest to
  Python (`internal/crud` the CRUD guard, `internal/<area>` the routes, such as
  `internal/labelprint`, the IPP client that sends label PDFs to CUPS, #7170; README there).
  `fleetchat/` is the agent fleet chat library (MCP tools over a host's `Store` and `Bus`,
  imports nothing else from this repo; README there). `fleet/` is ScadBuddy's host of it,
  the image `ghcr.io/eh-homelab/scadbuddy-fleet`: `sbfleet` (a `Store` and the session registry's reads, `Registry`, over core's
  `…/crud/fleet/` routes and a `Bus` over the API's fleet change stream, with the `fleet`
  credential; it imports nothing from `core/`), `ghsecrets` (the "ScadBuddy Fleet" GitHub
  App's Actions secret writer, #8012: a token scoped to each write, a sealed-box `PUT`, and
  the secret's `updated_at`; no route calls it yet, and it imports nothing else from this
  repo, so fleetd can reuse it), `ghwebhook` (the App's webhook receiver at
  `/github/webhook`, #8014: POST only, `X-Hub-Signature-256` first, installation events
  logged and counted) and the server binary (`/mcp`, `/healthz`, `FLEETCHAT_*` settings;
  README there).
  `fleetd/` is the fleet's machine service (#7987), a systemd user service on each Linux
  machine: `internal/discover` (the hashed machine id, live sessions from
  `<config dir>/sessions` and `/proc`, transcript paths by name only, git facts, the
  account uuid), `internal/registry` (the client of core's `…/crud/fleet-rotator/machines`
  routes, a `fleet` PAT from `--token-file`, https only), `internal/report` (changed rows
  only, all every 5 minutes) and `internal/watch` (inotify); README there.
  `claudecreds/` is a library for Claude Code logins (#7981): `ParseEntry` and `Tail`, the
  rate-limit and credential events in transcripts (#7988), and `ResetFromText`, the reset a
  usage-limit text names, in UTC or an IANA zone, held to the agent's
  `rate-limit-reset-vectors.json` with the TypeScript and Python ports (#7986), and
  `Rotate`, which replaces a config dir's login under Claude Code's own locks (#7989;
  README there).
  `telemetry/` is the OpenTelemetry setup every module uses (#7667): traces, metrics and
  logs over OTLP from the `OTEL_*` variables (off, at no cost, with no endpoint), the
  server and client HTTP wrappers, the slog bridge, the runtime and process metrics, and
  `pgxtrace` for pgx. A new Go service starts it with `telemetry.Start` and flushes it
  with `Shutdown`.

## See also

- The Go client is the exception to the root "Generated API files"; Go CI rules: root "CI and caching rules".
