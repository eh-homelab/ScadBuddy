# scadbuddy-core

ScadBuddy's API in Go (#7667). It answers the areas it has and gives every other request,
unchanged, to the Python API: WebSockets, SSE and large bodies included. On prod it is the API pod's front on 8080 and 8082 (eh-homelab/clusters#4334, #4366).

| Variable | Default | What |
|---|---|---|
| `SCADBUDDY_DATABASE_URL` | required | The API's database. At most 4 connections |
| `SCADBUDDY_CORE_UPSTREAM` | required | The Python API: an `http://` URL, or `unix:///path` |
| `SCADBUDDY_CORE_AREAS` | none | What Go answers, comma-separated (below). An unknown name stops the start |
| `SCADBUDDY_CORE_LISTEN` | `:8080` | The address it listens on |
| `SCADBUDDY_CORE_CRUD_LISTEN` | none | The CRUD port (`:8082`): only the CRUD routes, the service-token exchange and `/healthz`, as `crud/listener.py` |
| `SCADBUDDY_CORE_CRUD_UPSTREAM` | required with the CRUD port | Python's own CRUD listener (`http://127.0.0.1:<SCADBUDDY_CRUD_PORT>`): what the CRUD port does not answer goes there, never to the full API |
| `SCADBUDDY_CORE_TRUSTED_PROXIES` | none | Proxies whose `X-Forwarded-For` is believed, as `SCADBUDDY_TRUSTED_PROXIES` reads them |
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
| `plugins` | `POST` `/plugins/{name}/delete`, `/plugin-oauth/grants/{plugin}/mark`, `…/forget` only: the routes that need no KEK |
| `remote-plugins` † | every route of `crud_agent_plugins.py`: `/plugins` (`GET`, `POST`), `GET /plugins/{name}`, `POST` `/plugins/open-enabled`, `…/{name}/update`, `…/tool-access`, `…/reveal`, `…/delete`; `POST` `/plugin-oauth/flows` and its `…/authorize`, `…/take`; `/plugin-oauth/grants` and its `…/{plugin}/open`, `…/refreshed` (only while the lease `plugin-oauth:<plugin>` holds), `…/mark`, `…/forget`; `/plugin-oauth/stored-secrets` |
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

Area `fleet` (`internal/fleet`) is caller `fleet`'s (scope `crud.fleet`), under
`/api/v1/internal/crud/fleet`: `POST /agents/{session_id}/touch`, `GET /agents`, `PUT` and
`DELETE /channels/{name}/members/{session_id}`, `GET /agents/{session_id}/channels`, `POST
/messages`, `GET /agents/{session_id}/inbox`, `PUT /agents/{session_id}/cursor`. A send publishes
`fleet.message` after its commit. An inbox page holds up to `limit` messages (50 by
default, at most 200) and at most 4 MiB of message text: it stops before the message that
would pass 4 MiB, always holds at least one, and then says `more`; the cursor moves only past
the messages returned. A text over 16 MiB is a 422 whose detail names the limit (reason
`text-too-long`). A cursor `PUT` only moves it back: a `seq` past the cursor is a 422
(reason `seq-ahead`). `GET /changes` stays Python's.

Each of `browser-origins`, `bridge-tabs`, `pairings`, `mcp-tokens`, `plugin-packages` and
`plugin-feeds` also has a `:read` form, its GETs only. Area `package-mirrors` (the blob store)
is Python's.

An area Go answers in part leaves its other routes to Python: name it only where Python still runs.

`internal/seal` is the agent's sealed format (`backend/scadbuddy/core/secrets.py`), the payload
codec's subjects and payloads, and the canonical JSON of the input and response hashes; its tests
open every vector in `backend/tests/fixtures/`. `internal/gate` is the gate's validator, pinned by
`pending-input-vectors.json` and `response-hash-vectors.json`, and the `gate` area's routes.

A request that reaches Python carries one `X-Forwarded-For` value: its client, as the trusted
proxies name it (`core/proxies.py`). So Python runs with `SCADBUDDY_TRUSTED_PROXIES=127.0.0.1`
(and `SCADBUDDY_CORE_UPSTREAM` names the literal `127.0.0.1`, not `localhost`, which may connect over
`::1`, which Python would not trust),
listens on loopback only, and its own CRUD listener moves to a fixed port Go forwards the CRUD
port to (`SCADBUDDY_CRUD_PORT=8092`). That listener binds `0.0.0.0`; the NetworkPolicy, which
admits only 8080 and 8082, is what keeps other pods off it.
Loopback, not a Unix socket: `openscad-lsp` in Python's container may open `AF_UNIX` sockets but
no TCP ones (`editor/nonet.py`). Over a `unix://` upstream uvicorn gives Python no client
address, so `core/proxies.py` ignores the `X-Forwarded-For` the front sets: use TCP.

A forward that Python refuses (nothing listens yet, at a pod's start) is dialled again every
250 ms, for at most 30 s or until its client goes away; then the answer is 502. This holds only
until Python has taken one connection: after that, Python is up, and a refusal (Python restarted)
is a 502 at once. Only the
dial is tried again, so no request reaches Python twice.

## Telemetry

Core sends OpenTelemetry traces, metrics and logs over OTLP/HTTP, set up by the shared
`tools/go/telemetry` module (#7667, epic #988). Only the standard `OTEL_*` variables set it:

| Variable | What |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | The collector's base URL: `/v1/traces`, `/v1/metrics` and `/v1/logs` go there. Unset, with no signal's own endpoint, telemetry is off and costs nothing |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `…_METRICS_ENDPOINT`, `…_LOGS_ENDPOINT` | One signal's full URL, used as is |
| `OTEL_TRACES_EXPORTER`, `OTEL_METRICS_EXPORTER`, `OTEL_LOGS_EXPORTER` | Unset or a list that names `otlp`: on. `none` turns that signal off; any other value too, with a warning |
| `OTEL_SDK_DISABLED` | `true` turns every signal off |
| `OTEL_SERVICE_NAME` | Default `scadbuddy-core` |
| `OTEL_RESOURCE_ATTRIBUTES` | Added to the resource, as for the API (`deployment.environment=prod`) |
| `OTEL_TRACES_SAMPLER`, `…_ARG` | Replace the default sampler, which is the API's: a client span with no parent (a query from a background loop) is dropped |
| `OTEL_EXPORTER_OTLP_HEADERS`, `…_TIMEOUT`, `…_CERTIFICATE`, `OTEL_METRIC_EXPORT_INTERVAL`, `OTEL_BSP_*`, `OTEL_BLRP_*` | As the SDK reads them |

A setting telemetry cannot use (such as a malformed endpoint URL) is logged once as
"telemetry is off", and core runs with every signal off; it never stops core.

- **Traces.** Each request on both listeners is a server span, a child of the `traceparent` it
  carries. A route Go answers names it as the API's FastAPI spans are (`GET
  /api/v1/internal/crud/agent/sessions/{session_id}/events/last-seq`) and sets `http.route`;
  spans carry `http.method`, `http.status_code` and `http.request.method`,
  `http.response.status_code`. A forward is a client span to the upstream, and Python gets its
  `traceparent`, so Python's span is a child of core's: one trace. Every query, batch and pool
  wait is a span (`db.system=postgresql`, the statement's text with its values bound apart, named
  by `db.query.summary`). A 5xx is an error; a problem records `scadbuddy.failure_class` and
  `scadbuddy.problem.reason`. `/healthz`, `/metrics` and `/telemetry/v1/traces` are not traced,
  as in Python. No path, query string or header is recorded.
- **Metrics.** `http.server.request.duration`, `http.server.request.body.size`,
  `http.server.response.body.size` (by method, route, status), the same three as
  `http.client.*` for the forward, `db.client.operation.duration`,
  `db.client.connection.count` (used, idle), `db.client.connection.max` and
  `pgxpool.acquire.waits`, `…wait_time`, `…canceled`; the Go runtime's (`go.goroutine.count`,
  `go.memory.used`, `go.memory.class`, `go.gc.count`, `go.gc.pause.time`, `go.processor.limit`,
  `go.schedule.duration`, …) and the process's (`process.cpu.time`, `process.memory.usage`,
  `process.unix.file_descriptor.count`, `process.uptime`). Always cumulative.
- **Logs.** The JSON on stderr stays, with every attribute. With logs on, each record also goes
  over OTLP, and a record logged in a request carries its `trace_id` and `span_id` (on stderr
  too). The OTLP copy leaves out the attributes `path`, `error`, `err`, `url`, `query` and
  `dsn`, and any value that is a URL with credentials; a message's credentials are replaced
  (`telemetry.DefaultKeep`). The `log` package's lines (net/http's and the forward's, which
  can hold client addresses and upstream errors) stay on stderr only (`SetDefaultLogger`).

At a stop, the servers drain and the three signals flush within the 20 s shutdown.

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
