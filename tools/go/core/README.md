# scadbuddy-core

ScadBuddy's API in Go (#7667). It answers the areas it has and gives every other request,
unchanged, to the Python API: WebSockets, SSE and large bodies included. It is not deployed yet.

| Variable | Default | What |
|---|---|---|
| `SCADBUDDY_DATABASE_URL` | required | The API's database. At most 4 connections |
| `SCADBUDDY_CORE_UPSTREAM` | required | The Python API: an `http://` URL, or `unix:///path` |
| `SCADBUDDY_CORE_AREAS` | none | What Go answers, comma-separated (below). An unknown name stops the start |
| `SCADBUDDY_CORE_LISTEN` | `:8080` | The address it listens on |
| `SCADBUDDY_AUTH_MODE` | `off` | Only `enforce` changes a refusal's words |
| `SCADBUDDY_SECRET_KEY_FILE` | none | The KEK, read as Python reads it. A sealed area (†) does not start without it; a file that is set and cannot be read stops the start |
| `SCADBUDDY_SECRET_KEY_PREVIOUS_FILE` | none | The KEK before it, during a rotation: what it sealed still opens |

| Area | Routes under `/api/v1/internal/crud/agent` |
|---|---|
| `session-events:read` | `GET` `/sessions/{id}/events`, `…/events/last-seq`, `…/events/user-turn`, `…/events/turn` |
| `session-events` | those, and `POST` `/sessions/{id}/events` and `…/fork-log` |
| `session-turns:read` | `GET` `/sessions/{id}/turn`, `…/turn/queue`, `/turns/stale` |
| `session-turns` | those, and `POST` `/sessions/{id}/turn/send-claim`, `…/queueable`, `…/give-back`, `…/interrupt`, `…/placement`, `…/spend`, `…/finish`, `…/begin`, `…/queue`, `…/end-lost` |
| `settings`, `settings:read` | `GET`, `POST` `/settings` |
| `credentials` †, `credentials:read` † | `GET` `/credentials`, `…/one`, `…/key`; `POST` `/credentials` and `…/save`, `…/delete`, `…/order`, `…/reset`, `…/name`, `…/events`, `…/reveal` |
| `payload-keys` † | `POST /payload-keys/{subject}` |
| `keys` † | `GET /keys/input-hash` (`POST /fetch-grants` stays in Python) |
| `notify` †, `notify:read` † | `/push/vapid`, `/push/subscriptions` (and `…/remove`), `/push/sent`, `/push/poll-lease`, `/notify-channels` and every route under it (`GET /push/waiting` stays in Python) |
| `transcripts` †, `transcripts:read` † | `/sessions/{id}/entries` (and `…/last`, `…/landed`, `…/delete`), `…/transcript` (and `…/head`, `…/moved`), `…/cwd`, `…/subkeys`, `/transcripts` |
| `access`, `access:read` | `/access/rows`, `/access/row`, `/access/write`, `/tool-listings/*`, `/leases/{key}` |
| `plugin-sessions`, `plugin-sessions:read` | `/plugin-sessions/seen`, `…/ended`, `…/mark`, `…/dead`, `…/own` |
| `mcp-relay` | `POST /mcp-relay/*` |
| `gate` | `POST` `/gate/open`, `…/grant`, `…/deny`, `…/resolve`, `…/decide`; `GET` `/gate/response` |
| `pending-input` | `/pending-input/*`: the open entries, responses, traces, the orphan sweep and a call's recorded approval |
| `approvals` | `/approvals/*`: the classic approvals read, made, settled once and used once |
| `questions` | `/questions/*`: the `done` summaries read, dismissed and posted, and the attention count |
| `escalations` | `/escalations/*`: a flow run's calls passed up, denied, moved, delivered, and a run's entries |
| `audit` | `POST` `/audit/record`, `…/list`, `…/prune` |

`internal/seal` is the agent's sealed format (`backend/scadbuddy/core/secrets.py`), the payload
codec's subjects and payloads, and the canonical JSON of the input and response hashes; its tests
open every vector in `backend/tests/fixtures/`. `internal/gate` is the gate's validator, pinned by
`pending-input-vectors.json` and `response-hash-vectors.json`, and the `gate` area's routes.

A request that reaches Python carries `X-ScadBuddy-Peer`, the TCP peer, always overwritten.

## Where it differs from Python

- The credential is checked before the body is read, so a malformed body with no credential is
  401, not 422.
- `NaN` and `Infinity` in an event's JSON are refused (422). Python took them, and the log's
  `::jsonb` reads then failed.
- A 422's `errors[].loc` is the same; `msg` may differ. Clients read `loc`.
- An unexpected error is a 500 with detail `InternalError`, not the Python exception's name.
- The KEK files are read once, at the start. Python reads them again after a failed read, and
  answers 503 `kek-missing` while it has none; here a sealed area without a KEK does not start.
- Times are written in UTC. Python writes them in the database session's time zone, which is UTC
  on prod.
- A body bound refuses a body with no `Content-Length` (411) on the routes with one. Python's
  JSON gate counts a chunked body instead.
- The approvals' input hash refuses JSON past 512 levels; Python's parser goes to about 1000.
- A number, boolean or UUID in a body is read strictly: pydantic's lax mode also takes a numeric
  string. A UUID in another spelling is still read and written lower-case with hyphens.
- A lone surrogate escape (`"\ud800"`) in a gate response is kept, so its hash is the agent's,
  and it is written to the log escaped, as `JSON.stringify` writes it. Python could not send it to
  Postgres. In any other body text it reads as U+FFFD.
- `session-turns`' writes wait for a connection for at most 10 s (503 `pool_busy`), and for the
  row's lock for at most `lock_timeout` (503 `row_busy`), as in Python.

## Tests

`go test -race ./...` with `SCADBUDDY_TEST_DATABASE_URL` naming a Postgres where the role may
create databases; without it the database tests skip. Each test gets a database migrated by the
backend's own files, in `backend/scadbuddy/migrations` and `migrations_agent`.
