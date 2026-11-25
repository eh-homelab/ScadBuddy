# fleet

ScadBuddy's agent fleet chat server: `fleetchat`'s MCP server (`tools/go/fleetchat`)
over ScadBuddy's own store and bus. The design is
`docs/superpowers/specs/2026-11-25-agent-fleet-messaging-design.md` §5 and §6.5-6.6.

The image is `ghcr.io/eh-homelab/scadbuddy-fleet`, published by
`.github/workflows/build-go-image.yml`. It runs as uid 10003 and listens on 8080.

## Package `sbfleet`

`sbfleet` imports `fleetchat` and nothing from `tools/go/core`: it reaches core only
over HTTP (`TestNoCoreImports` checks it).

- `NewStore(coreURL, token)`: a `fleetchat.Store` over core's routes under
  `/api/v1/internal/crud/fleet/` (caller `fleet`; `tools/go/core/internal/fleet`):

  | Method | Route |
  |---|---|
  | `Touch` | `POST /agents/{session_id}/touch` |
  | `Agents` | `GET /agents` (`{"agents"}`) |
  | `Join` / `Leave` | `PUT` / `DELETE /channels/{name}/members/{session_id}` |
  | `Channels` | `GET /agents/{session_id}/channels` (`{"channels"}`) |
  | `Send` | `POST /messages` `{"from", "channel" or "agent", "text"}` (`{"seq", "recipients"}`) |
  | `Inbox` | `GET /agents/{session_id}/inbox?limit=&peek=` (`{"messages", "more"}`) |
  | `Rewind` | `PUT /agents/{session_id}/cursor` `{"seq"}` |

  A problem's `reason` maps to an error that `errors.Is` finds: `unknown-agent` (404)
  to `fleetchat.ErrUnknownAgent`, `not-member` (403) to `fleetchat.ErrNotMember`, and
  `handle-taken` (409, two session ids that share their first 8 hex characters) to
  `sbfleet.ErrHandleTaken`. Any other answer is an error that names the status. A 403
  with no `not-member` reason (the CRUD guard's refusal) is not `ErrNotMember`. Every
  request has a 30 s timeout.
- `NewBus(coreURL, token)`: a `fleetchat.Bus`. `Publish` does nothing: core publishes
  `fleet.message` itself when a send commits. `Subscribe` follows the change stream
  `GET /api/v1/internal/crud/fleet/changes` (served by the Python API,
  `backend/scadbuddy/api/crud_fleet.py`; core forwards it), a server-sent event
  stream whose frames are `backend/scadbuddy/agent_changes/hub.py`'s:
  - `change`, data `{"c", "p"}`, where `p` is the stored event as a JSON string. A
    `fleet.message` event's `recipients` (session ids) become
    `Event{SessionIDs: recipients}`; other kinds are skipped.
  - `ready`, data `{"resync", "reconnect"}`. With `resync` true (the server could not
    place `Last-Event-ID`: pruned, unknown, or too far behind) it becomes
    `Event{Resync: true}`. Without it the replay before it lost nothing, so it is no
    event.
  - `reconnected` (the server's own reader fell behind and replayed): `Event{Resync: true}`.
  - `: keepalive` comments, every 15 s.

  Every frame's `id` is kept and sent as `Last-Event-ID` on each reconnect. A stream
  that fails, is refused (a 401 or 403 too: the credential rotates), ends, or sends
  nothing for 45 s is followed again after a back-off of 500 ms doubling to 30 s,
  reset after a stream that delivered a frame. `Subscribe` returns only when its
  context ends.

The token function is called for every request. A read error fails that request (and,
for the stream, that attempt).

## Binary

`main.go` serves `/mcp` (fleetchat's streamable HTTP handler, server name
`scadbuddy-fleet`, its version from the build info or `dev`) and `GET /healthz`
(`ok`). On SIGTERM it ends the open requests and stops within 10 s. The server has
no auth of its own; a NetworkPolicy admits only ScadBuddy's pods.

| Flag | Environment | Default |
|---|---|---|
| `-listen` | `FLEETCHAT_LISTEN` | `:8080` |
| `-core-url` | `FLEETCHAT_CORE_URL` | required: core's URL, such as `http://scadbuddy-core:8080` |
| `-credential-file` | `FLEETCHAT_CREDENTIAL_FILE` | required: the `fleet` service credential, a bearer token, read again at every request |
| `-max-text` | `FLEETCHAT_MAX_TEXT` | `16777216` (16 MiB) |
| `-page-size` | `FLEETCHAT_PAGE_SIZE` | `50` |
| `-max-page-size` | `FLEETCHAT_MAX_PAGE_SIZE` | `200` |
| `-session-timeout` | `FLEETCHAT_SESSION_TIMEOUT` | `0` (never): a Go duration |

A flag wins over its variable, which wins over the default. A bad value, from either,
stops the start. `--help` and `-version` need no core URL.

`-session-timeout` closes an MCP session, with its inbox subscriptions, after that long
without a request. The MCP SDK counts only POSTs as activity, so a timeout also ends a
session that only holds its GET stream open.

Core caps a send's body at twice its own 16 MiB text limit plus 1 MiB, so a
`-max-text` above 16 MiB does not let longer texts through.

## Commands

```bash
cd tools/go/fleet && go vet ./... && go test -race ./...
docker build -f tools/go/fleet/Dockerfile -t scadbuddy-fleet:dev .   # repo root as context
```
