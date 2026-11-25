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
  | `Inbox` | `GET /agents/{session_id}/inbox?limit=&peek=` (`{"messages", "more"}`; a page holds at most 4 MiB of text, and at least one message) |
  | `Rewind` | `PUT /agents/{session_id}/cursor` `{"seq"}` |

  A problem's `reason` maps to an error that `errors.Is` finds: `unknown-agent` (404)
  to `fleetchat.ErrUnknownAgent`, `not-member` (403) to `fleetchat.ErrNotMember`, and
  `handle-taken` (409, two session ids that share their first 8 hex characters) to
  `sbfleet.ErrHandleTaken`, `seq-ahead` (422, a rewind past the cursor) to
  `fleetchat.ErrBadSeq`, and `text-too-long` (422, a text over core's 16 MiB, detail
  `text is longer than 16777216 bytes`) to `fleetchat.ErrTooLong`. Any other answer is an error that names the status. A 403
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
  - `ready`, data `{"resync", "reconnect"}`. It becomes `Event{Resync: true}` when
    `resync` is true (the server could not place `Last-Event-ID`: pruned, unknown, or
    too far behind) or `reconnect` is false (a stream sent no `Last-Event-ID`, the first
    one or any before a frame was seen, starts at the newest event, so what came before
    was never seen). With `reconnect` true and no `resync`, the replay before it lost
    nothing (every `fleet.*` event is logged), so it is no event.
  - `reconnected` (the server's own reader fell behind and replayed): `Event{Resync: true}`.
  - `: keepalive` comments, every 15 s.
  - A `ready` or `change` frame that cannot be read is logged and becomes
    `Event{Resync: true}`.

  Each stream logs a line once core answers 200, and each frame that becomes a resync
  logs one too.

  Every frame's `id` is kept and sent as `Last-Event-ID` on each reconnect. A stream
  that fails, is refused (a 401 or 403 too: the credential rotates), ends, or sends
  nothing for 45 s is followed again after a back-off of 500 ms doubling to 30 s,
  reset after a stream that delivered a frame. `Subscribe` returns only when its
  context ends.

The token function is called for every request. A read error fails that request (and,
for the stream, that attempt). Both take `OnUnauthorized(f)`: a request core refuses
with 401 calls `f` with the refused credential and is sent once more with the next one
(core's CRUD guard refuses before a route runs, so no route runs twice).

- `NewExchange(coreURL, proofFile, proof)`: the `fleet` service credential from the
  service-token exchange (root `README.md`, "The service-token exchange"). `Token`
  reads the proof file again and posts it to `POST /api/v1/auth/service-token`
  (`client_id` `fleet`) when it holds no credential, at half-life (7.5 of the 15
  minutes), and after `Invalidate`, which drops a credential core refused; it exchanges
  again at once, but never under 10 s after the last exchange. A failed exchange is
  tried again after 5 s, doubling to 60 s, and a 429 after its `Retry-After`; meanwhile
  the credential held serves until it expires. The proof is `ProofKubernetes` (a
  projected ServiceAccount token, ServiceAccount `scadbuddy-fleet`, audience
  `scadbuddy`) or `ProofBootstrap` (a secret whose hash the API holds as the file
  `fleet` in `SCADBUDDY_SERVICE_BOOTSTRAP_DIR`). It mirrors the agent's
  `ExchangeCredential` (`agent/src/api/serviceCredential.ts`). It never logs the proof
  or the credential.

## Binary

`main.go` serves `/mcp` (fleetchat's streamable HTTP handler, server name
`scadbuddy-fleet`, its version from the build info or `dev`) and `GET /healthz`
(`ok`). On SIGTERM it takes no new request and lets the open ones finish for 5 s,
then ends those still open (the MCP GET streams) and stops within 10 s. The server has
no auth of its own; a NetworkPolicy admits only ScadBuddy's pods.

| Flag | Environment | Default |
|---|---|---|
| `-listen` | `FLEETCHAT_LISTEN` | `:8080` |
| `-core-url` | `FLEETCHAT_CORE_URL` | required: core's URL, such as `http://scadbuddy-core:8080` |
| `-service-token-file` | `FLEETCHAT_SERVICE_TOKEN_FILE` | a projected ServiceAccount token, exchanged for the `fleet` credential |
| `-service-bootstrap-file` | `FLEETCHAT_SERVICE_BOOTSTRAP_FILE` | a bootstrap secret, exchanged for the `fleet` credential |
| `-credential-file` | `FLEETCHAT_CREDENTIAL_FILE` | a fixed `fleet` credential, read again at every request and never renewed (tests, local runs) |
| `-max-text` | `FLEETCHAT_MAX_TEXT` | `16777216` (16 MiB) |
| `-page-size` | `FLEETCHAT_PAGE_SIZE` | `50` |
| `-max-page-size` | `FLEETCHAT_MAX_PAGE_SIZE` | `200` |
| `-session-timeout` | `FLEETCHAT_SESSION_TIMEOUT` | `24h`: a Go duration; `0` never closes a session |

Exactly one of the three credential settings is required. An exchanged credential is
renewed in the process (`NewExchange`); a fixed one lasts 15 minutes unless something
rewrites the file. A flag wins over its variable, which wins over the default. A bad
value, from either, stops the start. `--help` and `-version` need no core URL.

`-session-timeout` closes an MCP session, with its inbox subscriptions, after that long
without a request. Claude Code never sends `DELETE /mcp` when it exits (measured
2026-11-25 with Claude Code 2.1.332: four `claude -p` runs through a logging proxy
against a local stack sent none), so with `0` every run would leave its session
and its subscription open for the life of the process. The MCP SDK counts only POSTs as activity, so a timeout also ends a
session that only holds its GET stream open.

Core caps a send's body at twice its own 16 MiB text limit plus 1 MiB, and refuses a
longer text with a 422 that names the limit, so a `-max-text` above 16 MiB does not let
longer texts through.

## Deploying

Nothing deploys it yet; the Kubernetes objects belong in eh-homelab/clusters. Before a
deploy:

- Core serves the fleet's routes only when `SCADBUDDY_CORE_AREAS` names `fleet`
  (`tools/go/core/README.md`). Without it the routes go to Python, which has none of them
  but `GET …/fleet/changes`.
- The pod runs as the ServiceAccount `scadbuddy-fleet`, which the API maps to caller
  `fleet` (`backend/scadbuddy/auth/exchange.py` `SERVICE_ACCOUNTS`).
- The proof is a projected ServiceAccount token, audience `scadbuddy`, from a projected
  volume (not `subPath`), named by `FLEETCHAT_SERVICE_TOKEN_FILE`. On compose or a laptop,
  use `FLEETCHAT_SERVICE_BOOTSTRAP_FILE` and the file `fleet` in the API's
  `SCADBUDDY_SERVICE_BOOTSTRAP_DIR` instead. Never deploy `FLEETCHAT_CREDENTIAL_FILE`: a
  fixed credential is not renewed.
- A NetworkPolicy that admits only ScadBuddy's pods on 8080 (the server has no auth of
  its own) and lets the pod reach only DNS and core.
- `FLEETCHAT_SESSION_TIMEOUT`: keep the 24 h default, or set a shorter one; never `0`
  while Claude Code is a client.

## Commands

```bash
cd tools/go/fleet && go vet ./... && go test -race ./...
docker build -f tools/go/fleet/Dockerfile -t scadbuddy-fleet:dev .   # repo root as context
```
