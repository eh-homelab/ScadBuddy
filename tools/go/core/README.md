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
| `session-blobs:read` | `GET /session-blobs` |
| `session-blobs` | that, and `POST /session-blobs` and `…/deletes` |
| `attachments:read` | `GET /attachments/{id}/image` and `…/preview` |
| `attachments` | those, and `POST /attachments`, `…/resolve`, `…/deletes` and `…/sweeps` |
| `http-bodies:read` | `GET /http-bodies/meta` and `…/bytes` |
| `http-bodies` | those, and `POST /http-bodies` |
| `background-tasks:read` | every `GET` under `/background-tasks` |
| `background-tasks` | every route under `/background-tasks` |
| `operations:read` | `GET /operations/{id}` and `…/by-key/{key}`. The insert and the end stay Python's: they write the activity row |
| `workspace:read` | `GET /workspace/files`, `…/file`, `…/usage`, and `POST …/session-files` |
| `workspace:sql` | those, and `POST /workspace/file-deletes`, `…/sweeps` and `…/reconciles`. The status, the bytes, a write, a copy and the move to blobs stay Python's: they need the blob store or the KEK |
| `activity-kinds` | `POST /activity-kinds` |
| `browser-origins` | `GET` `/browser-origins`, `…/approved`; `POST` `…/approve`, `…/revoke` |
| `bridge-tabs` | `/session-tabs`, `/tab-owners` and its `claim`, `refresh`, `release`, `release-all`; `PUT /tab-images/{name}`, `POST …/{name}/take` |
| `pairings` | `/pairings` and every route under it |
| `mcp-tokens` | `/mcp-tokens` and every route under it |
| `plugins` | `POST` `/plugins/{name}/delete`, `/plugin-oauth/grants/{plugin}/mark`, `…/forget` only. The rest seal or open with the KEK, and stay Python's |
| `plugin-packages` | `/plugin-packages` and every route under it |
| `hosted-plugins:read` | `GET /hosted-plugins/{name}`. A version's zip (the blob store) stays Python's |
| `plugin-feeds` | `GET /plugin-feeds/{feed}`; `PUT`, `DELETE` `…/packages/{name}` |
| `sessions:read` | `GET /session-starts`, `GET /sessions/{id}`, `POST /sessions/list` |
| `sessions` | those, and `POST /sessions`, `…/{id}/agent`, `…/offer`, `…/offer/cancel`, `…/owner`, `…/budget`, `…/discard` |
| `session-resources:read` | `GET /sessions/{id}/resources` |
| `session-resources` | that, and `POST /sessions/{id}/resources` |
| `session-order-costs` | `POST /sessions/{id}/order-costs`, its `…/report`, `…/attempt`, `…/settle`, and `/order-costs/sweep` |
| `publish` | `POST /publish/session-event`, `/publish/warm-evict` |
| `session-reads` | `GET /sessions/{id}/open-calls`, `…/reasoning` |
| `session-sweeps` | `POST /sessions/live` |
| `forget-rows` | `POST /sessions/{id}/forget-rows` |
| `session-edits` | `POST /sessions/{id}/edit` |
| `migrations` | `POST /migrations/ledger` |
| `auth-sessions` | `POST /auth-sessions/lookup` |
| `forget:read` | `GET /flow-blobs/{subject}/{name}` |
| `forget` | that, and `POST /payload-keys/{subject}/drop`, `/payload-key-sweeps`, `/flow-blobs/{subject}`; `POST /forgotten-subjects/{subject}` stays Python's (it opens transcripts under the KEK) |
| `workspace-blob-refs:read` | `GET /workspace-blob-refs` |
| `workspace-blob-refs` | that, and `POST /workspace-blob-refs`, `…/release` |
| `events` | `GET /events/head`, `GET /events` |
| `flows` | `GET /flow-runs/{id}/starter`, `…/queue`, `…/steps`, `/flow-runs/waiting`, `/flow-runs/parent-hop-approvals`, `/flow-decisions`, `/flow-grants/in-force`, `POST /flow-runs/parent-hop/waiting` |
| `gate` | `POST` `/gate/open`, `…/grant`, `…/deny`, `…/resolve`, `…/decide`; `GET` `/gate/response` |
| `pending-input` | `/pending-input/*`: the open entries, responses, traces, the orphan sweep and a call's recorded approval |
| `approvals` | `/approvals/*`: the classic approvals read, made, settled once and used once |
| `questions` | `/questions/*`: the `done` summaries read, dismissed and posted, and the attention count |
| `escalations` | `/escalations/*`: a flow run's calls passed up, denied, moved, delivered, and a run's entries |
| `audit` | `POST` `/audit/record`, `…/list`, `…/prune` |

Each of `browser-origins`, `bridge-tabs`, `pairings`, `mcp-tokens`, `plugin-packages` and
`plugin-feeds` also has a `:read` form, its GETs only. Area `package-mirrors` (the blob store)
is Python's.

An area Go answers in part leaves its other routes to Python: name it only where Python still runs.

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
  on prod. A time sent with no offset is read as UTC, not in the database's zone.
- A body bound refuses a body with no `Content-Length` (411) on the routes with one. Python's
  JSON gate counts a chunked body instead. A multipart write with no `Content-Length` is 411
  before its Content-Type is checked.
- A number, boolean or UUID in a body is read strictly: pydantic's lax mode also takes a numeric
  string. A UUID in another spelling is still read and written lower-case with hyphens. The
  blob, background-task and workspace areas refuse `5.0` for an integer (422), and a time given
  as a number.
- The approvals' input hash refuses JSON past 512 levels; Python's parser goes to about 1000.
- A lone surrogate escape (`"\ud800"`) in a gate response is kept, so its hash is the agent's,
  and it is written to the log escaped, as `JSON.stringify` writes it. Python could not send it to
  Postgres. In any other body text it reads as U+FFFD.
- `session-turns`' writes wait for a connection for at most 10 s (503 `pool_busy`), and for the
  row's lock for at most `lock_timeout` (503 `row_busy`), as in Python.
- A token's `expires_at` is read by Postgres, so it takes most of what `timestamptz` takes. Its
  special values (`infinity`, `now`, `today`, `tomorrow`, `yesterday`, `epoch`) are refused, as
  pydantic refused them; a value Postgres does not take is a 422 at `["body", "expires_at"]`.
- A package write's `plugin_packages.changed` is logged and NOTIFYed after the commit, in the
  request's handler but not under its cancellation. Python queued it on its outbox.
- A re-pin whose review's `name` is a list or an object names it as JSON in the 409's detail,
  not as Python's `repr`.
- `POST /flow-blobs/{subject}`: a malformed multipart body's 422 has other words, and an image's
  `size` is a JSON number (Python also took a numeric string).
- `POST /sessions`: a scope's size counts each number as it was sent; Python counted its own
  spelling of it (`1e3` as `1000.0`).
- `POST /workspace-blob-refs` answers 204 with no `Content-Type`.

## Tests

`go test -race ./...` with `SCADBUDDY_TEST_DATABASE_URL` naming a Postgres where the role may
create databases; without it the database tests skip. Each test gets a database migrated by the
backend's own files, in `backend/scadbuddy/migrations` and `migrations_agent`.
