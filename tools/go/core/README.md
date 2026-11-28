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
| `SCADBUDDY_TEMPORAL_ADDRESS` | required with area `inventory` | Temporal's frontend (`host:port`, no TLS, as the Python client). Without it, a start that names `inventory` stops. With only `inventory:read`, it is optional: the operation read then asks Temporal about a stale row. With `filament-presets`, it is optional too: without it the reads serve and each write answers 503 `writes_not_configured` ("The preset writes"). Naming the area does not set it: the container needs the variable as well |
| `SCADBUDDY_TEMPORAL_NAMESPACE` | `scadbuddy` | As `core/config.py` `DEFAULT_TEMPORAL_NAMESPACE` |
| `SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY` | `inventory` | The task queue the inventory worker polls |
| `SCADBUDDY_TEMPORAL_TASK_QUEUE_FILAMENT_PRESETS` | `filament-presets` | The task queue of the preset writes' workflow, which core's own worker polls when the area `filament-presets` is named and `SCADBUDDY_TEMPORAL_ADDRESS` is set |
| `SCADBUDDY_TEMPORAL_TASK_QUEUE_LIBRARY` | `library` | Python's own variable (`core/settings.py` `temporal_task_queue_library`): the queue the preset writes' activities run on |
| `SCADBUDDY_ORDERS_CURRENCY` | `USD` | The shop's currency when the setting `orders_currency` has no row (a lot's cost is in it) |

| Area | Routes under `/api/v1/internal/crud/agent` |
|---|---|
| `session-events:read` | `GET` `/sessions/{id}/events`, `…/events/last-seq`, `…/events/user-turn`, `…/events/turn` |
| `session-events` | those, and `POST` `/sessions/{id}/events` and `…/fork-log` |
| `session-turns:read` | `GET` `/sessions/{id}/turn`, `…/turn/queue`, `/turns/stale` |
| `session-turns` | those, and `POST` `/sessions/{id}/turn/send-claim`, `…/queueable`, `…/give-back`, `…/interrupt`, `…/placement`, `…/spend`, `…/finish`, `…/begin`, `…/queue`, `…/end-lost` |
| `settings`, `settings:read` | `GET`, `POST` `/settings` |
| `credentials` †, `credentials:read` † | `GET` `/credentials`, `…/one`, `…/key`; `POST` `/credentials` and `…/save`, `…/delete`, `…/order`, `…/reset`, `…/name`, `…/events`, `…/reveal` |
| `payload-keys` † | `POST /payload-keys/{subject}` |
| `keys` † | `GET /keys/input-hash` (`internal/secrets`) and `POST /fetch-grants` (`internal/fetchgrants`) |
| `notify` †, `notify:read` † | `/push/vapid`, `/push/subscriptions` (and `…/remove`), `/push/sent`, `/push/poll-lease`, `/notify-channels` and every route under it, and `GET /push/waiting` |
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
| `operations:read` | `GET /operations/{id}` and `…/by-key/{key}` |
| `operations` | those, and `POST /operations` and `…/{id}/finish`, each with its `activity` row and `activity.changed` event in its transaction (`internal/activity`) |
| `workspace:read` | `GET /workspace/files`, `…/file`, `…/usage`, and `POST …/session-files` |
| `workspace:sql` | those, and `POST /workspace/file-deletes`, `…/sweeps` and `…/reconciles` |
| `workspace` † | every route of `crud_agent_workspace.py`: those, and `GET /workspace/status`, `…/file-bytes`, `POST …/file-writes`, `…/copies`, `…/blob-moves` (below), over the blob store in process |
| `activity-kinds` | `POST /activity-kinds` |
| `browser-origins` | `GET` `/browser-origins`, `…/approved`; `POST` `…/approve`, `…/revoke` |
| `bridge-tabs` | `/session-tabs`, `/tab-owners` and its `claim`, `refresh`, `release`, `release-all`; `PUT /tab-images/{name}`, `POST …/{name}/take` |
| `pairings` | `/pairings` and every route under it |
| `mcp-tokens` | `/mcp-tokens` and every route under it |
| `plugins` | `POST` `/plugins/{name}/delete`, `/plugin-oauth/grants/{plugin}/mark`, `…/forget` only: the routes that need no KEK |
| `remote-plugins` † | replaces `plugins` (name one of the two): every route of `crud_agent_plugins.py`: `/plugins` (`GET`, `POST`), `GET /plugins/{name}`, `POST` `/plugins/open-enabled`, `…/{name}/update`, `…/tool-access`, `…/reveal`, `…/delete`; `POST` `/plugin-oauth/flows` and its `…/authorize`, `…/take`; `/plugin-oauth/grants` and its `…/{plugin}/open`, `…/refreshed` (only while the lease `plugin-oauth:<plugin>` holds), `…/mark`, `…/forget`; `/plugin-oauth/stored-secrets` |
| `plugin-packages` | `/plugin-packages` and every route under it |
| `hosted-plugins:read` | `GET /hosted-plugins/{name}`. A version's zip is area `package-mirrors` |
| `package-mirrors` † | `POST /package-mirrors/{name}`, `GET /package-mirrors/{name}/{sha256}`, `GET /hosted-plugins/{name}/versions/{version}.zip`: the routes that need the blob store (below) |
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
| `forget` | that, and `POST /payload-keys/{subject}/drop`, `/payload-key-sweeps`, `/flow-blobs/{subject}`, `/forgotten-subjects/{subject}` (a session's forks take their ranges first, opened and sealed under the KEK; with none, a fork that takes transcript entries is 409 `forks-take-ranges`) |
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
| `order-costs` | `POST /orders/{id}/assistant-cost`: the cost entry, its `order_events` row and, after the commit, `order.changed` and `order_item.changed`. It reads `pricing_usd_rate` and `orders_currency` from `settings`, else `SCADBUDDY_PRICING_USD_RATE` and `SCADBUDDY_ORDERS_CURRENCY` from core's own environment, else `1` and `USD`, so core needs the API's values of those two |
| `unknown-trays:read` | `GET /api/v1/filament/unknown-trays` (public, caller `api`), and the print worker's `GET …/crud/print/filament/printers/{printer_id}/unknown-trays` (caller `print`) |
| `unknown-trays` | those, and `POST /api/v1/filament/unknown-trays/{id}/dismiss` and `POST …/{id}/resolve` (public). Go writes no spool here (#7143): the resolve runs Python's activity `filament_resolve_unknown_tray` on queue `library` ("The unknown-tray resolve") |
| `filament-presets` † | Public, class `api`: `GET /api/v1/filament/presets` (`?source=`, `?type_id=`), `GET …/presets/{preset_id}` and `GET …/presets/{preset_id}/diff` (`?from_version=`, `?to_version=`); the writes `POST …/presets/{preset_id}/copy`, `PATCH …/presets/{preset_id}` and `POST …/presets/{preset_id}/mirror`, each a Python activity on queue `library` ("The preset writes"). `POST …/presets/bambuddy-import` stays Python's. Go writes no preset row (#7204) |

The `unknown-trays` public routes use `internal/auth` (see "Public routes"). Their CRUD route is caller `print`'s, so it has its own `case` in `newRouter` (areaPackages mounts on caller `agent`).

Area `workspace` † (`internal/workspace`, #8131) serves a file's bytes, sealed as Python seals
them: on a backend with a blob store, a file is the blob `workspace-<sha256 of the sealed bytes>`,
sealed under a file key of its own in segments of 1 MiB, segment i in the context
`workspace-file:<sha256>:<i>:<more|end>`; the row keeps the file key wrapped in the context
`workspace-file-key:<workspace>:<path>:<blob key>`, under the session's payload data key (made by
the write while the session stands, no audit row) or, for a principal's file, under the KEK
(`key_kek_id`; the previous KEK still opens one). On `store_backend` `local` the bytes stay in
Postgres, one `ai_session_file_bytes` row per sha256. A write is one transaction: the session's
row first when it logs events, the workspace's advisory lock, the conflict, quota and
directory checks, the row, its blob's `blob_refs` hold (and the old blob's release) and its
`workspace.file` events; on a blob store the checks also run before the upload, so a refused
write stores nothing. A fork's copy wraps every file key again first, then writes the rows
under the parent's lock while its blob rows are the ones wrapped (3 tries, then 409
`conflict`). A read streams a segment at a time and reads the row again between segments: a
file replaced meanwhile ends the stream short. The rotation's re-wrap stays Python's.
`backend/tests/fixtures/workspace-seal-vectors.json`, which `tests/test_workspace_seal_vectors.py`
writes from Python's own functions (`UPDATE_WORKSPACE_SEAL_VECTORS=1`), holds Go to Python's
bytes: Go seals each file with the same IVs to the same bytes, opens Python's, and wraps each
file key alike. Its blob store is `workspace.Store` (`Backend`, `Put`, `Get`): main.go's
`workspaceBlobs` over `internal/blobstore`'s `Backend`, `PutContent` and `Fetch` (below), which
names a refusal by Python's class (`StoreFullError`, `StageLockBusyError`) in a write's 503. Like
`package-mirrors` it needs the KEK, and serves only `store_backend` `s3` or `local`: on any
other, `GET …/workspace/status` answers `store` `refused` with the reason. A write with no
Content-Length is a 411 in `_write_body`'s words (route `OwnLength`).

The `filament-presets` routes use `internal/auth` (see "Public routes"). They have their own
`case` in `newRouter`, because they need the blob store. The area needs the KEK, and it needs
an `s3` store to read documents. It reads each document with `Blobs.OpenKey` (#8129), and it
reads the blob's size from its `store_blobs` row first, so a blob over the cap gets no GET.
The document is the zip member `file`. Its credential keys are stripped, as
`core/credentials.py` strips them. A document that is not read leaves the detail's
`document` null and names why in `document_error`; the diff answers that code as a problem.

## The preset writes

The three writes of `filament-presets` (plan
`docs/superpowers/plans/2026-11-26-filament-presets-view.md`, rulings R7, R11, R12, P21 and
P22) run Python's activities `filament_preset_copy`, `filament_preset_edit` and
`filament_preset_mirror_again` (`filament/preset_writes.py`) on the queue `library`, which the
API process serves. Go writes no row.

- Each write is one run of core's workflow `LibraryRun` (`internal/libraryrun`, #8489; it was
  `FilamentPresetWrite` until then), on the queue
  `SCADBUDDY_TEMPORAL_TASK_QUEUE_FILAMENT_PRESETS`. Core's own worker runs it. Its one step is
  the activity, with one JSON object as its argument, and one attempt. The mirror bound is
  30 s for copy and edit and 60 s for a send again. The activity's start-to-close is that
  bound plus 30 s: 60 s for copy and edit, 90 s for a send again. Its schedule-to-start is
  the route's wait, the start-to-close plus 10 s (plan ruling P23). The route waits for the
  result for that same time, then answers 503 `preset_write_pending`. The run's execution
  timeout is that time too (P26): the schedule-to-start starts only when core's worker runs
  the workflow task, so a run whose workflow task no core worker took in time closes as the
  route answers, and Temporal gives a closed run's activity task to no worker. So a write
  does not start later than a moment after the route answered (the timeout counts from
  the moment Temporal records the start). An activity that started in time can still end
  after that: its write stands, and a copy or an edit keeps its reply in the ledger.
- The run's id is `filament-preset-copy-` or `filament-preset-edit-` and the SHA-256 of the
  actor, the key, the preset id and the JSON body that Go sends the activity, joined by
  `\x1f`, so one key with another body runs apart; a send again's is
  `filament-preset-mirror-<preset_id>`. The start uses the conflict policy `USE_EXISTING`, so
  a send while a run of the id is open joins it. Its reuse policy is `ALLOW_DUPLICATE`: a send
  after the run closed starts a new run, and the activity answers the reply that the
  idempotency ledger keeps (`crud/idempotency.py` `idempotent_write`), or refuses the key with
  another body (`idempotency_key_reused`).
- Copy and edit need an `Idempotency-Key` (1 to 200 characters): without one the answer is
  428 with the type `https://scadbuddy.dev/problems/crud/idempotency-key-required`, and
  nothing starts. Go checks the body before the start as the activity's models do (R7): a
  refused key is 422 `key_refused` with the extension `key`, any other fault 422
  `invalid_input`. The activity checks it again.
- An activity failure's `type` is the problem's `code`, with its status (409 for
  `not_manual`, `version_moved`, `document_missing`, `mirror_off`, `not_configured`,
  `instance_changed` and `not_mirrored`; 404 `preset_not_found`; 422 `key_refused`,
  `invalid_input` and `idempotency_key_reused`). `version_moved` carries `current_version`.
  A timeout, or a start that Temporal refuses, is 503 `preset_write_pending`. Any other type
  is a 500 with no message.
- Copy answers 201 and the new preset's detail; edit answers `{changed, preset}`; a send again
  answers `{outcome, preset}`, `preset` the row. Each is read after the activity's answer.
- Without `SCADBUDDY_TEMPORAL_ADDRESS` core still starts with the area, the reads serve, no
  worker runs, and each write answers 503 `writes_not_configured`.
- The `library` worker is versioned (deployment `scadbuddy-library`). Core's workflow is not,
  so its activity task carries no version. Temporal gives it to the queue's current version: the
  API build that made itself current at its start (`main._make_library_current`). Measured on
  prod on 2026-11-28 (#8428): one copy and one send again, with core and the API on build
  `758cdcdbe`. The history's `ACTIVITY_TASK_STARTED` events name worker build id `758cdcdbe…`
  for both activities (read through the Temporal UI's `/history` API).

## The unknown-tray resolve

`POST /api/v1/filament/unknown-trays/{id}/resolve` (#8489; plan
`docs/superpowers/plans/2026-11-23-unknown-spool-flow.md` Task 3B, rulings G5, G6, U1 to U4)
runs Python's activity `filament_resolve_unknown_tray` (`filament/unknown_resolve.py`) on the
queue `library`. Go writes no spool: the activity links the spool, books its first weight and
deletes the tray's row in one transaction.

- It is one run of `LibraryRun` on core's own worker, as a preset write is. The activity's
  start-to-close is 30 s, with one attempt. The route waits that plus 10 s, then answers 503
  `resolve_pending`. A timeout of the activity, or a start that Temporal refuses, is
  `resolve_pending` too.
- Go checks the body before the start: `{spool_id?: uuid, code?: string, net_weight_g:
  number, seen_at: string}`. Exactly one of `spool_id` and `code`, `net_weight_g` above 0 and
  at most 100000, `seen_at` an RFC 3339 time with a zone, and no other field. Anything else is
  422 `invalid_input`, and nothing starts. A path id that is no UUID is 409 `tray_changed`.
  The activity gets `{tray_id, spool_id, code, net_weight_g, seen_at, actor}`, `seen_at` as
  sent and `actor` from the caller (as a preset write's).
- The run's id is `unknown-tray-resolve-` and the SHA-256 of the actor, the tray id and that
  input. `USE_EXISTING` and `ALLOW_DUPLICATE`, so two equal sends join one run, and another
  body runs apart: the activity's `seen_at` check and the tray's lock then refuse the second
  with `tray_changed`.
- An activity failure's `type` is the problem's `code`: 409 `tray_changed`, `spool_archived`,
  `no_ams_kind` and `tag_clash` (its detail is the activity's message); 404 `code_unknown` and
  `spool_missing`. Any other type is a 500 with no message.
- It answers 200 `{id, number, code, location_id}`, the spool the tray holds now (G6).
- Without `SCADBUDDY_TEMPORAL_ADDRESS` the list and dismiss serve, and a resolve answers 503
  `writes_not_configured`. Area `unknown-trays:read` does not serve the resolve.

Core runs the one `LibraryRun` worker when `SCADBUDDY_TEMPORAL_ADDRESS` is set and
`SCADBUDDY_CORE_AREAS` names `filament-presets` or `unknown-trays`. Its queue is still
`SCADBUDDY_TEMPORAL_TASK_QUEUE_FILAMENT_PRESETS` (default `filament-presets`).

Area `fleet` (`internal/fleet`) is caller `fleet`'s (scope `crud.fleet`), under
`/api/v1/internal/crud/fleet`: `POST /agents/{session_id}/touch`, `GET /agents`, `PUT` and
`DELETE /channels/{name}/members/{session_id}` (`{name}` is a plain name, or `~` and the
unpadded base64url of any name; a GitHub reference must take that form, because its `/` and `#` would need escapes, and the router
leaves a path with an escape in it to Python; a bad `~` segment is a 422), `GET
/agents/{session_id}/channels`, `POST /messages`, `GET /agents/{session_id}/inbox`, `PUT /agents/{session_id}/cursor`. A send publishes
`fleet.message` after its commit. An inbox page holds up to `limit` messages (50 by
default, at most 200) and at most 4 MiB of message text: it stops before the message that
would pass 4 MiB, always holds at least one, and then says `more`; the cursor moves only past
the messages returned. A text over 16 MiB is a 422 whose detail names the limit (reason
`text-too-long`). A cursor `PUT` only moves it back: a `seq` past the cursor is a 422
(reason `seq-ahead`). A channel's name follows fleetchat's rule (#8149,
`tools/go/fleetchat/README.md`), as does the `fleet_channels` CHECK; a join that would make a channel with a name valid
only under the rule before it is a 422 (reason `legacy-channel-name`). Its `GET /changes` is
area `changes` (below).

## The event stream (#7768)

`internal/bus` publishes on the event bus and reads it. One `LISTEN` connection per process,
outside the request pool, hears `scadbuddy_events` and the agent's five channels
(`scadbuddy_input`, `_pending`, `_mcp`, `_warm_evict`, `_settings`) and hands each NOTIFY to
every subscriber's bounded queue. It never waits on a subscriber. When the connection comes
back after a drop, every subscriber gets a gap. Two areas read it; each is off until
`SCADBUDDY_CORE_AREAS` names it:

| Area | What |
|---|---|
| `changes` | `GET /api/v1/internal/crud/<caller>/changes` for the callers `agent`, `fleet`, `camera` and `camera-relay`, as `agent_changes/hub.py` serves them: the same frames (`change`, `ready`, `reconnected`, the keepalive every 15 s), the `Last-Event-ID` replay (at most 1000 events, else `resync`), the gap's replay, and the filters (`fleet.*` kinds only to `fleet`; `printer.changed` and `camera.reconnect` only to the camera callers; the other channels only to `agent`). At most 4 streams per credential and peer address (the TCP peer, never `X-Forwarded-For`, as uvicorn `--no-proxy-headers` gives it to Python) and 64 in all (429); `fleet` and the two camera callers have budgets of their own, 8 each |
| `ws` | `WS /api/v1/ws`, the browser's realtime socket, as `api/realtime.py` serves it: the topic protocol (`subscribe`, `unsubscribe`, `check`; `subscribed` with `last`, `changed`, `event`, `resync`, `ping` every 25 s, `error`), the origin check, `SCADBUDDY_AUTH_MODE=enforce`'s refusal before the handshake, a revoked session's close (4401), and the socket cap. The handshake goes through `internal/auth`'s gate as an `api` route (the caller from a session cookie, an `sbpat_` access token or an `sbsvc_` credential), so `observe` counts a socket that `enforce` would refuse in the gate's `scadbuddy.auth.would_refuse`, as `auth/check.py` counts it |

`ws` reads `SCADBUDDY_PUBLIC_URL` (the setting `public_url`'s stored value comes first, read
at each handshake), `SCADBUDDY_ALLOWED_ORIGINS` and `SCADBUDDY_REALTIME_SOCKETS` (default 256;
the stored setting comes first, read at the start), as the API does. A refused handshake is a
403 with no body, as uvicorn answers a socket closed before it was accepted.

Python's frames and core's are the same bytes: `backend/tests/fixtures/event-stream-vectors.json`
holds each payload's change frame live and in replay, which callers keep it, its topics and its
socket frame, the typed kinds' fields, and the topic rules. The backend's
`tests/test_event_stream_vectors.py` writes it from Python's own code
(`UPDATE_EVENT_STREAM_VECTORS=1`) and checks it; `internal/bus`, `internal/changes` and
`internal/realtime` check core against it. A replay writes a typed event as pydantic's
`model_dump_json` does (`internal/bus/kinds.go` holds each kind's fields) and an opaque one as
`json.dumps` does, both from the log's `jsonb`.

The same area serves the session registry (`internal/fleet/registry.go`, #7985). Caller
`fleet` reads `GET /sessions` (`?limit=`, as the inbox: the most recently seen session
first, by its newest host, and `more`) and `GET /sessions/{session_id}` (404 reason
`unknown-session`). One session id can run on several machines at once, so each session is
its `session_id`, `handle`, `last_seen` (the newest host's) and `hosts`, one per machine,
the most recently seen first: each host with its machine and its credential's `id`,
`name`, `kind` and `last4` only, joined from `ai_credentials`, never a secret column. Caller `fleet-rotator` (scope `crud.fleet-rotator`),
under `/api/v1/internal/crud/fleet-rotator`, writes `PUT /machines/{id}` (the id is 64
lower-case hex: a SHA-256, never the raw machine id) and `PUT
/machines/{machine_id}/sessions/{session_id}` (the session's row on that machine, keyed by
both ids; the whole row: an optional field left out is cleared). A session `PUT` makes the
session's fleet agent as `touch` does (409 `handle-taken`), needs its machine (404
`unknown-machine`), links a new row to the newest row of the same machine, pid and
`proc_start` (`previous_session_id`, the session before a `/clear`), and answers that one
row only, never another machine's. Every field is checked: lengths,
absolute paths, no control characters, a full `git_head`. `git_remote` is an `http(s)://`,
`ssh://` or `git://` URL with no query and no fragment, or the scp-like `user@host:path`
(user `[A-Za-z0-9._-]+`). Only an `ssh://` URL may name a user, with no password; anything
else (a token in the user part, a local path, `file://`, `ext::`) is a 422.

A `fleet-rotator` credential names its machine: the `<machine>` of the bootstrap proof file
`fleet-rotator.<machine>` it was exchanged for (`service_credentials.machine`; `crud.ServiceOf`
gives a handler the verified service). The machine's `id` is the service's own hash, so the
first `PUT /machines/{id}` records the caller's proof name in `fleet_machines.proof_name`. A
`PUT /machines/{id}` of a row another proof registered is a 403 (reason
`machine-of-another-proof`), and so is a session `PUT` whose machine is registered to
another proof. So two machines of different proofs each hold a row of the same session id,
and neither changes the other's. A `fleet-rotator` credential with no machine writes
nothing (403 `no-machine`). Core reads Python's `SCADBUDDY_SERVICE_BOOTSTRAP_DIR` (#8172): a
credential that names a machine passes only while `fleet-rotator.<machine>` is a regular file
there (one stat per resolve, as `exchange.py` `BootstrapVerifier.has_machine`). When the file is
gone, `internal/auth` refuses the credential and revokes its row at once. With the variable
unset, core refuses every such credential, so the core container needs the API's bootstrap
secret directory mounted and the variable set.

The two `fleet-rotator` `PUT`s also take a person's access token of scope `fleet` (#8041), which
`fleetd` sends; `POST /virtual-messages` does not, so a machine's token never posts as a virtual
sender such as `rotator` or `github`: `crud.Router.AddForToken` mounts
them, and `auth.Resolver.Token`, the shared `sbpat_` lookup, reads it. A handler finds the token
with `auth.CallerOf`; `crud.ServiceOf` is nil then. One live token is one machine: the first
`PUT /machines/{id}` records the token's id in `fleet_machines.token_id`, and while that token
lives, every write to that machine from another token, or from a bootstrap proof, is 403
`machine-of-another-proof`. Each row names exactly one owner, its `proof_name` or its
`token_id`. A row moves to a new owner only when its owner is dead, on a `PUT /machines/{id}`
that locks the row (`FOR UPDATE`) while it decides: a token's machine moves to a new `fleet`
token of the same person (`personal_access_tokens.principal`) once the old token is revoked or
expired. A token never takes a machine a bootstrap proof registered (403 `machine-of-a-proof`):
a proof has no person to match, so an admin deletes the row first (#8450). A token of another
person never takes a machine (403 `machine-of-another-person`), and a proof never takes a
token's machine. A row deleted while a `PUT` writes it is inserted again once; a second miss is
409 `machine-changed`. When the old token's row is gone, its person is unknown and every token is refused
(403 `machine-of-unknown-owner`); no route deletes a machine row yet (#8450), so an admin deletes it. A
session `PUT` takes only the row's current owner, so fleetd's machine `PUT` moves the row
first. A revoked, expired or unknown `sbpat_` token on these routes is 401 `no_credential`, so
a person stops a machine's writes by revoking its token in Settings, with no restart; a new
token of that person then takes the machine over.
A `read` or `write` token there is 403 `out_of_scope`. A `fleet` token is 403 `out_of_scope` on
every other route, in every `SCADBUDDY_AUTH_MODE`: every other CRUD route, and every public route
behind `internal/auth`'s gate (`auth.WouldRefuse`, `Gate.Wrap`), as Python's `auth/check.py`
`auth_check` refuses it on every route Python serves (#8179). A `read` or `write` token on
another caller's CRUD route stays 401, as Python's guard answers it. The bootstrap proofs keep
working until #8367 retires them for machines.

Area `fleet-logins` † (`internal/secrets/fleet_logins.go`, #7984; spec
`docs/superpowers/specs/2026-11-26-fleet-session-registry-and-rotator-design.md` §6.2) is the
login rotator's secret path. It needs the KEK, and the machine routes read area `fleet`'s
`fleet_machines`, so name both. Caller `fleet-rotator` (and a `fleet` token, through
`AddForToken`, as the registry `PUT`s) gets these routes under
`/api/v1/internal/crud/fleet-rotator`. Each names a `machine_id` that the caller's token or proof
must own (`fleet.OwnMachine`: 404 `unknown-machine`, 403 `machine-of-another-proof`), so a
machine leases, writes back, records and rotates only for its own machines:

- `POST /leases` `{machine_id}`: the first `claude_login` by priority whose scope is `fleet` or
  `both` and that is not disabled, not cooling down, of no account with a login cooling down,
  leased to no machine, and sealed under the current KEK (`FOR UPDATE SKIP LOCKED`). It sets
  `leased_to`, `leased_at` and `lease_epoch + 1` and answers `{lease: {credential, lease_epoch,
  login}, recovers_at: null}`: `login` is the `claudeAiOauth` object, the only secret these routes
  answer. With none it answers `{lease: null, recovers_at}`, the soonest time one is usable again
  (null when none recovers on its own). A row that does not open is 409 `credential-sealed`.
- `PUT /leases/{credential}` `{machine_id, lease_epoch, login}`: the write-back. It seals the
  login over the row (the AAD of every credential row) while the machine holds the lease at that
  epoch, else 409 `lease-stale`; a login of another account is 409 `another-account`. The row's
  health and `epoch` stay. The 10 s timeout and what fleetd does on a failure are the client's
  (§6.6 step 3).
- `DELETE /leases/{credential}?machine_id=&lease_epoch=`: the release, at that epoch (409
  `lease-stale`); `lease_epoch` moves on.
- `POST /events` `{machine_id, entry_uuid, session_id?, credential_id, epoch, kind, reason?,
  until?, reset_source?}`: `kind` is `rate_limit` (needs `until`), `permanent`, `transient`
  (each needs a `reason`) or `recovery`, written as the agent's `cooling_down`, `disabled`,
  `transient` and `used` health writes, keyed by the row's `epoch`. The row must be a fleet
  login the machine leases (409 `not-leased`). `fleet_credential_events` keeps one row per
  `entry_uuid`: a repeat answers `{duplicate: true}` and writes nothing. A rate limit also cools
  every other `claude_login` of the row's `account_uuid`, by the same later-wins rule, and
  answers their ids as `cooled`.
- `POST /rotations` (201) `{machine_id, config_dir?, from_credential_id?, to_credential_id?,
  reason, rotated_at?}` and `PUT /rotations/{id}` `{sessions: [{session_id, result, at?}],
  verified_at?}`, the check written whole (`result` `pending`, `passed`, `failed`, `no-answer`
  or `unreachable`): `fleet_rotations`. Only the rotation's machine's owner writes it (404
  `unknown-rotation`).

Each lease, reveal, write-back, release, rotation and check writes an `ai_audit` row of kind
`credential` (actions `fleet.lease`, `fleet.reveal`, `fleet.write_back`, `fleet.release`,
`fleet.rotation`, `fleet.rotation_check`; a lease with no login is `refused`), naming the token's
person or the proof's machine, never a secret. The fleet chat server's caller `fleet` gets 403
`wrong_caller` on all of them, a `read` or `write` token 403 `out_of_scope`. No route here
publishes to the event log, so none takes the event-log lock.

The area also serves a person's route, `POST /api/v1/fleet/logins` (class `person`), that adds a
fleet login with the store's `agent=False` path (#8009 refuses the agent's caller every fleet
row): the agent's credential body (`kind`, `secret`, `name`, `scope`, `email`, `base_url`), a
`claude_login` (scope `fleet`) or another kind whose scope is `fleet` or `both` (422 otherwise).
It answers `{credential}`, never the secret, and writes an `ai_audit` row `fleet.add`. Only a
person signed in themselves (a `login` or `break_glass` session cookie with `X-ScadBuddy-Request:
1`) may call it, in every `SCADBUDDY_AUTH_MODE`, not only in `enforce`: a token, a service or no
one is refused with the gate's problem. Settings → Assistant offers it as "Add a Claude login for
the fleet" (`frontend/src/components/assistant/FleetLoginForm.tsx`). The agent's reveal still
opens no `claude_login` and no `fleet` row (`agentsOwn`).

Area `blobs` † (`internal/blobstore`, #7958) is a shared area: any service credential whose scopes
hold `crud.blobs` may call it, under `/api/v1/internal/crud/blobs`; no caller is granted that scope
yet. Python has no such routes. `POST /uploads` with `{kind, size, sha256, media_type}` answers
`{upload_id, url, headers, expires_at}`: the caller PUTs exactly the bytes to `url` with `headers`,
then `POST /uploads/{upload_id}/commit` answers `{key, sha256, size}`, the blob indexed in
`store_blobs` under its kind (again, the same answer). The kinds are an allowlist with a size each
(`blobstore.UploadKinds`): only `tag_dump`, 64 KiB. Core's own `Put` also stores `scan_image`, 12 MiB
(a code scan's photo, #8155: a 12 MP phone JPEG is 3 to 6 MiB and a phone's PNG screenshot under
10 MiB, and `Put` holds the whole blob in memory), and a presigned upload refuses it
(`kind_not_allowed`): `Put` reads a photo's type from its bytes, a presigned PUT reads none. Only `store_backend` `s3` takes uploads: core reads the store's settings as
Python resolves them (the `settings` row, else `SCADBUDDY_STORE_*`, set even when empty, else the
default; the secret key opened with the KEK; path-style URLs, as botocore makes them for either
`store_s3_path_style`) and presigns the PUT itself, with the length, the type, the SHA-256 checksum
and the ACL `private` signed (Signature V4, pinned against botocore's answer in its tests). The PUT
goes to the upload's own staging object, `<prefix>/staging/<32 random hex>` (the storefront's
staging form, `store/s3.py` `staging_key`), never to the content key. The commit holds the
object's stage lock shared (`blob-object:s3:<sha256>`, a session lock on a connection of its own
outside the pool, at most 3 at once, a slot waited for at most 10 s; 6 tries of 5 s, then 503
`store_busy`, as `StageHold`). It reads the content object: one that is there is never written, as
`store/s3.py` `upload` does not, and one that is not the key's bytes is a 409
`store_object_mismatch` (only a repair fixes it; Python's `_verified` answers 403 `ref_mismatch`), with nothing indexed (a row that named it stays as it was) and the staging
object kept, so the commit goes through once the object is repaired. Otherwise it reads the staging
object (at most the kind's limit), checks its size and SHA-256, and PUTs those verified bytes to the
content key, so a later PUT to the staging key changes nothing indexed; a staging object gone (another
commit of the upload took it) sends it back to the content object. Then it writes the row as
Python's `BlobIndex.put` does, with `verified_at`, as `mark_verified_in` sets it (`backend/tests/fixtures/blob-upload-vectors.json`,
which both sides' tests check), and deletes the staging object. One left behind (no commit, or a PUT
sent again after it) goes with the storefront's backstop, `sweep_staged`, a day old; Python's
unrecorded sweep takes content keys only. Any other backend is a 503 `not_configured`. The
upload's id is a ticket MACed under the KEK (HKDF label `scadbuddy:blobs:upload:v1`), so no row
stands for an upload: its URL lasts 15 minutes and its commit an hour more, and another caller's
commit of it is a 404. Refusals carry a `reason`: 422 `kind_not_allowed`, 413 `upload_too_large`,
507 `store_full`, 404 `upload_not_found`, 410 `upload_expired`, 409 `upload_not_received`, 422
`upload_mismatch`, 409 `store_object_mismatch`, 503 `store_unavailable` or `store_busy`.
A Go handler in core reads a blob in-process with `Blobs.OpenKey(ctx, key)` (a `store_blobs` key, the form Python's `blob_key` and `blob_refs` hold) or `Blobs.Open(ctx, kind, sha256)`. It adds no route. The read looks up the row, sends a presigned GET to the row's S3 object, waits at most a minute for its headers, and streams the bytes with their `Ref` for as long as the caller's context allows. The stream checks size and SHA-256 and ends with `ErrBlobMismatch` on a wrong object. A missing row or object is `ErrBlobNotFound`. A store that is not S3, or a row on another backend, is `ErrNotConfigured`.
A Go handler in core that took the bytes itself stores them with `Blobs.Put(ctx, kind, mediaType, body)`:
the kind's limit checked, then the bytes spooled to a temporary file and stored through `put` (below)
with the commit's row (`record`, with `verified_at`), the object PUT typed and private as a commit's is.
Like `PublishFile` and `PutContent`, it checks the store's caps only when it uploads, so the same bytes
stored again are never refused `store_full`.

`put` is core's one in-process write path (#8250): `Put`, `PublishFile` and `PutContent` all go
through it. Its `stage` checks every object it would use, the one a row names and the content
object alike, where Python's `store/s3.py` `upload` trusts one it finds: a GET that declares
another size is a mismatch with no byte read, else the object is read and hashed, within a minute
plus a second per MiB. A row-named object that is not the blob's bytes is logged and passed over,
so the content object is checked or written instead and the row moves to it. Only a content
object that is not the blob's bytes is a 409 `store_object_mismatch`, left as it is, with nothing
indexed, for every caller.

`Blobs.Claim`, `Blobs.PublishFile` and `Blobs.ReadFile` (#8137) are the rest of the store in process, for area `package-mirrors` (above). A publish goes through `put`: the bytes spooled and hashed, the object's stage lock held shared as a commit holds it, the object reused or stored, read and checked first and the caps checked when it uploads (`stage`, above), the row written by the caller's `indexer`, and the object removed again when that writes no row and no other row names it (`removeUnnamed`). It adds no route; the upload routes and their kinds are as above. `Blobs.Backend`, `Blobs.PutContent` and `Blobs.Fetch` (#8131) are ContentStore's backend name, `put` with no key then `touch`, and `stat` then `get`, for area `workspace`: a put goes through `put` too, its row written by `putIn` at `<kind>-<sha256>` in a transaction, and the object the key named before released when it was another of this store's (`release_replaced`); a fetch of a lost object forgets its row. They take any kind, `workspace` among them; the upload routes' kinds stay as above. Area `package-mirrors` answers a stored blob past its read bound (`ErrTooLarge`) as a 500 with reason `blob-too-large`, its key logged.
`internal/blobclient` is its Go client: `New(baseURL, token, tel)`, `Upload(ctx, kind, mediaType, body)`; each call to core may take 2 minutes, longer than a busy commit takes to answer `store_busy`.
Both sides trace their requests with `telemetry`'s `Transport`: each span records the method, the
status and the peer (`s3` for the store), never a URL, so a presigned query is never recorded. A
request to the store carries no trace context (`internal/httpx` `Outside`), as Python's S3 spans
send none.

The `fleet` area also serves caller `fleet-rotator` (scope `crud.fleet-rotator`, #8026):
`POST /api/v1/internal/crud/fleet-rotator/virtual-messages` `{sender, channel | agent, text}`
posts as a virtual sender (a row of `fleet_virtual_senders`, such as `rotator`): to a
channel's members, or as a DM to one agent by its handle. An unknown sender is a 404 (reason
`unknown-sender`). A `POST /messages` DM to a virtual sender's name is a 422 (reason
`virtual-sender`) whose detail is the sender's reject message. An inbox entry from a virtual
sender has its name as `from`.

Areas `inventory` and `inventory:read` (`internal/inventory`, #6949 K1) are caller `inventory`'s
(scope `crud.inventory`, minted in process by the API), under `/api/v1/internal/crud/inventory`.
Both have the same routes, all reads: `GET /operations/one?id=` (the id is a query value, since a
key holds `:`), `/kinds` (`?archived=`), `/kinds/{kind_id}`, `/kinds/{kind_id}/schema`, `/items`
(`?kind=&descendants=&q=&low=&archived=&compatible=&limit=`), `/items/{item_id}`, `…/lots`,
`…/units` (`?status=`), `…/estimate?qty=`, `/lots/{lot_id}`, `/units/{unit_id}`, `/moves`
(`?item_id=&lot_id=&unit_id=&order_item_id=&limit=`). Each answers the public route's JSON. There
is no write route. Area `inventory` also runs the inventory worker.

### The inventory worker

With area `inventory`, core runs a Temporal worker on the task queue
`SCADBUDDY_TEMPORAL_TASK_QUEUE_INVENTORY` beside its HTTP servers, and stops it with them
(`internal/inventory/activities`). Its client is lazy: Temporal out of reach does not stop core.
The worker asks Temporal's `CheckHealth` every 5 s until it answers, then builds a worker and
starts it. The SDK keeps a worker's first `Start` result, so after a failed start the next try
builds a new worker. At shutdown core stops the worker within the drain's 20 s. Each write of the inventory is one activity,
registered by its name: `inventory.KindCreate`, `…KindUpdate`, `…KindArchive`, `…KindRestore`,
`…ItemCreate`, `…ItemUpdate`, `…ItemArchive`, `…ItemRestore`, `…ReceiveLot`, `…ReceiveUnits`,
`…LotUpdate` (cost, place, note), `…LotAdjust`, `…Take` (a use, and an order's take), `…Count`,
`…UnitUpdate` (name, serial, place), `…UnitStatus`, `…GiveBack`, `…Recost`, `…Repoint`.

- The input is `{key, actor, params, body}`: the idempotency key, the Actor object, the public
  route's path parameters, and its public body. The code reads `key`, never the activity id.
  A public write's activity id is `inventory.<Name>:<key>` (`store.ActivityID`, Python's
  `core_client.activity_id`; both test `internal/inventory/store/testdata/activity_ids.json`),
  so a key reused on another write runs as another activity, which the ledger refuses.
- The answer is `{status, body}`: the public route's status and JSON, or its problem. A refusal is
  an answer, so Temporal does not retry it. Only an infrastructure error (the database out of
  reach) fails the activity, and Temporal retries it. A bad actor fails it, not retried.
- Each activity is one `store.Write` under the caller `inventory` with `key`: an activity that runs
  twice writes once, and the same key with another input is 422 `idempotency-key-reused`.
- The record (`inventory_operations`, R-K1-22): before each attempt the worker's interceptor
  writes the row as `running` and sends `inventory_operation.changed` on `scadbuddy_events`
  (`publish.Notify`, a notice with no log row, as the agent's `ai_operation.changed`). The write
  ends the row (`succeeded`, or `failed` with the problem) in its own transaction, so the end
  commits with the stock write or not at all. An attempt that fails keeps the row `running` with
  `last_error`. A record write that fails is logged and counted
  (`scadbuddy_inventory_record_failures`); it never fails the activity. Each start first deletes
  the finished rows (`succeeded` or `failed`) that ended more than 8 days ago
  (`store.pruneOperations`, as `insertOperation` prunes `ai_operations`). A `running` row stays.
  The 8 days are longer than the 7 days that `crud_requests` keeps an `inventory` key, so a replay
  of the key and a read of its operation still find the row while the ledger keeps the answer.
- `GET /operations/one` reads a `running` row older than 60 s with Temporal (the activity's
  Describe, or the workflow's): one that Temporal closed without a result answers `failed` with
  the problem `inventory_activity_failed`. One that completed answers from its answer in
  `crud_requests` (`succeeded` with the result, or `failed` with the problem), or `failed` when
  there is none. The read writes nothing.
- The SDK is `go.temporal.io/sdk` v1.52.0 (Standalone Activities on the client side,
  [`internal/internal_activity_client.go`](https://github.com/temporalio/sdk-go/blob/v1.52.0/internal/internal_activity_client.go)),
  with its OpenTelemetry tracing interceptor (`contrib/opentelemetry` v0.9.0) when traces are on.

Each of `browser-origins`, `bridge-tabs`, `pairings`, `mcp-tokens`, `plugin-packages` and
`plugin-feeds` also has a `:read` form, its GETs only. Area `package-mirrors`
(`internal/packages` `MirrorAreas`, #8137) reads and publishes in process through
`internal/blobstore`'s `Claim`, `PublishFile` and `ReadFile`, which are store/cache.py
`CachedBlobStore`'s `claim`, `publish_fresh` (with `expected=None`) and `read_file` on the S3
store only (any other `store_backend` is 503 `not_configured`); it needs `SCADBUDDY_SECRET_KEY_FILE`,
as area `blobs` does. A published mirror is a `piece` blob, a zip of one entry with Python's
`pack_dir` header, directory, date and mode (`backend/tests/fixtures/pack-dir-vectors.json`);
its deflate stream is Go's `compress/flate`, not zlib's, so Go and Python pack the same file to
different bytes, and each reads the other's. As `api/crud_agent_packages.py` and `agent_data/package_mirrors.py` do, a mirror's `POST` spools
the body to a temporary file and hashes it as it streams, refused 413 past 24 MiB (on a declared
`Content-Length` before the body is read, or as it streams without one), keeps it as the blob
`plugin-mirror-<sha256>` (claimed when the store holds it, else published), then holds it in
`blob_refs` for `<name>/<sha256>` and releases every hold no row names past its day, in one
transaction. A mirror's `GET` serves the bytes only while the package's row names the sha256 in
`bytes_sha256`, `pending_bytes_sha256`, `mirror_sha256` or `pending_mirror_sha256` (404
`mirror-not-found`). A hosted version's zip is the blob `attachment-<zip_sha256>`, checked
against its sha256 (500 `blob-missing`), with its sha256 and content hash in the headers.

An area Go answers in part leaves its other routes to Python: name it only where Python still runs.

`internal/seal` is the agent's sealed format (`backend/scadbuddy/core/secrets.py`), the payload
codec's subjects and payloads, and the canonical JSON of the input and response hashes; its tests
open every vector in `backend/tests/fixtures/`. `internal/gate` is the gate's validator, pinned by
`pending-input-vectors.json` and `response-hash-vectors.json`, and the `gate` area's routes.

`internal/bambutag` decodes a Bambu Lab MIFARE Classic 1K filament tag and derives its sector
keys from the UID, after the [RFID Tag Guide](https://github.com/Bambu-Research-Group/RFID-Tag-Guide/blob/a26cf83d7b296b71c558f8c332d464f4e5b9c314/BambuLabRfid.md)
(#8058). It is pure: no I/O and no SQL, and it never writes a tag. Its `Tag` JSON is what a scan
record stores; `testdata/*.json` pins it, and a change bumps `DecoderVersion`.

Area `filament-tag-reads` † (`internal/tagreads`, #8148) is a public area: `POST
/api/v1/filament/tags/reads`, class `api`, at Python's path. A body with `mifare_blocks` is
core's: it checks the blocks (at most 64, index 0 to 63, 16 bytes each), drops every sector
trailer, decodes the tag with `internal/bambutag`, stores the dump as a `tag_dump` blob
(`blobstore.Blobs.Put`, the upload commit's checks with no presigned PUT), and in one
transaction finds or makes the tag by tray UUID and UID (`filament_tag_uids`), folds a UID-only
tag of the same tray into it, and appends a `filament_tag_reads` row. Then it forwards a UID-only
read to Python, which writes the last-seen, the bind and the answer, and adds `bambu`,
`tag_read_id`, `dump_key` and, for a tag with no spool, `draft`. Any other body goes to Python
unchanged (with `image` and `location` taken off), and an answered tag gets its history row after.
A read of either kind writes no history row (nor, for MIFARE, a `raw_blob` or a hold) when the
body says `tap: false` (a page's lookup of a tag it read before) or binds a spool
(`bind_spool_id`, the page binding a tag it just read). Core's own refusals of a MIFARE read,
422 `mifare_invalid` (its shape) and 422 `mifare_unreadable` (the decode), come before it records
anything. A bound tag that gains a tray UUID (set on it, or by a fold) is queued in
`filament_bb_tag_pending`. A MIFARE read locks the tags of its tray UUID and UID in id order.
A MIFARE read makes its tag `bambu_mifare` only when it decodes to a tray UUID or a filament type.
A read's source is the caller's: an access token or a service credential is `agent`.
`image` (a committed `scan_image` blob) and `location` are checked and stored on the row.
`Reads.RecordMifare(ctx, body, actor, source)` is the same write for a Go caller (#8157). With
the area off, Python answers a MIFARE read 503 `mifare_needs_core`.
A body with a `read_id` that is not null (#8244) names a paired device's read in place of `uid`, `tag_type`,
`records` and `mifare_blocks` (with any of them not null, or a `records` that is not empty, it is a 422 `device_read_invalid`, as is a photo read). Core loads the
read (`devices.Store.OwnRead`: a read of a device the caller does not own is a 404
`read_not_found`, with `code`, as a read that is not there), reads its `tag_dump` blob, and takes the dump's
UID, its tech as `tag_type` (cut to 32 bytes, on a whole character), and its blocks as `mifare_blocks`. A dump
that is no MIFARE Classic 1K read (no block, a UID that is not 4 bytes, or blocks past 63) gives
a UID-only read with the dump's records instead. Then it decodes the read as an inline one. The
history row's source is `app`, and its `device_read_id` names the read. Only the history row is
idempotent: a second post of the same read answers the first row's `tag_read_id` and writes no
second row, but the rest runs again (Python's answer and last-seen mark, the tag's update), and
an `image` sent with it is not kept. With the area off, Python answers a `read_id` 503
`device_read_needs_core`.

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

### Code scans (#8155)

Area `scan` (`internal/scan`) is a public area (below, "Public routes"), class `api`: a person
scans a code from a photo, and core decodes it. The page cannot PUT to the bucket (the bucket's
CORS rule and the app's CSP admit only the storefront), so core takes the photo's bytes:

| Route | Body | Answer |
|---|---|---|
| `POST /api/v1/scan/images` | the photo's bytes (JPEG, PNG or WebP; at most 12 MiB) | `{key, media_type, sha256, size}`: a `scan_image` blob, its type read from its bytes, never from a header. 415 `image_not_supported` for any other bytes; a HEIC, HEIF or AVIF photo is refused with its own words (no decoder in pure Go reads one: the page draws a chosen photo on a canvas and sends a JPEG) |
| `POST /api/v1/scan/codes` | `{key, source?, location?}`, or `{read_id, source?, location?}` (#8244: a paired device's photo read in place of `key`, a null `read_id` as none; its `source` defaults to `app`): `source` `shutter` (the default), `nfc`, `file` or `app`; `location` `{lat, lon, accuracy_m?, at?}`, lat -90 to 90, lon -180 to 180, accuracy 0 to 1,000,000 m | a scan (below), recorded. 404 `image_not_found` for a key that is no `scan_image` blob; 404 `read_not_found` for a read of a device the caller does not own, as for one that is not there; 422 `read_not_a_photo` for a tag read. Each post is a new scan, of a `read_id` as of a `key` |
| `GET /api/v1/scan/scans?limit=&before=` | | `{scans}`, newest first, at most 100 (50 by default); `before` is a scan id. A person sees only their own scans (a scan holds its photo and GPS fix); a service sees all; any other caller (anonymous, as under auth `off` or `observe`) sees only the scans made with no actor. The same holds for every route below: another person's scan is a 404 |
| `GET /api/v1/scan/scans/{id}` | | a scan, or 404 `scan_not_found` |
| `GET /api/v1/scan/scans/{id}/image` | | the photo's bytes with its media type, `nosniff` (scriptless, for an `<img>`) |
| `POST /api/v1/scan/scans/{id}/decode` | | the scan, decoded again |
| `DELETE /api/v1/scan/scans/{id}` | | 204: the row and its photo's hold go; the sweep takes the photo after its grace |

A scan is `{id, at, source, actor, image: {key, media_type}, width, height, codes, error, location,
decoded_at}`. Each of `codes` is `{format, text, resolved}`: `format` as the browser's
`BarcodeDetector` names it (`qr_code`, `data_matrix`, `ean_13`, `ean_8`, `upc_a`, `upc_e`,
`code_128`), `text` at most 4096 bytes of valid UTF-8 with no NUL, and `resolved` the thing it names
in `GET /api/v1/codes/{code}`'s shape, or null: a spool label's `scadbuddy:spool:<id>` names its
spool, a place label's `http(s)://<any host>/x/<code>` or a bare typed code (`L-12`, `S-142`) what
the code names (`resolve.go` keeps `codes/resolve.py` `PREFIXES`: a new prefix there is one here
too). Each read resolves again, so an archive since the scan shows. `error` is `image_unreadable`
or `image_too_large`, else null: the photo's header is read first, and one past 24 MP or 12,000
pixels on a side is never decoded (a decompression bomb). At most 2 photos decode at once (a 24 MP
photo is 96 MiB of RGBA); a scan that waits 10 s for a slot is 503 `scan_busy`.

The decoder is `github.com/makiuchi-d/gozxing` (pure Go): every QR code (its multi reader), one Data
Matrix, one EAN or UPC and one Code 128, at most 16 codes. The 1D readers try the photo turned 90°;
the 2D detectors find a code at any angle. A 1D result counts only when a row beside it reads the
same text (a photo's grain passes an EAN's check digit on about one row in ten). Each scan is a
`code_scans` row that holds its photo in `blob_refs` (`code_scan`, the scan's id) in the same
transaction, success or not, so the photo is kept until the scan is deleted. `Scans.Record(ctx,
body, Input)` is a whole scan from bytes for a Go caller (#8157's result route), and
`Scans.Decode(ctx, key, Input)` one of a stored photo.

### Label printing (#7170)

Area `label-printing` (`internal/labelprint`, design
`docs/superpowers/specs/2026-11-27-cups-label-printing-design.md`) is a public area, class `api`.
It sends a label PDF to a queue of the cluster's CUPS server (`deploy/cups/`) with one IPP
`Print-Job`. It has its own `case` in `newRouter`, because its client uses the process's
telemetry (peer `cups`).

The server is the stored setting `filament_label_server_url` (only a person's own session
writes it). No row, or JSON `null`, is `http://cups.cups.svc:8631`. Core checks the value again
when it reads it: `http` or `https`, a host, no user, no query, no fragment, and a path of `/`
or none. A bad value is no server (502). A request never names a server.

| Route | Body | Answer |
|---|---|---|
| `GET /api/v1/label-printing/queues` | none | `{queues}`, in CUPS's order. A queue is `{name, info, make_and_model, state, state_reasons, state_message, accepting, media, width_mm, height_mm}`; the size comes from `media`, or is null |
| `POST /api/v1/label-printing/queues/{queue}/jobs?media_type=&darkness=&print_rate=` | the PDF's bytes, at most 8 MiB | 202 `{queue, job_id, job_state, job_state_reasons}` when CUPS accepts the job, not when the label prints. `media_type` is `Saved`, `Thermal` or `Direct`; `darkness` `-1` or `1` to `30`; `print_rate` `Default` or `1` to `12`. A missing value or the default sends no attribute |

The queue name must be 1 to 127 of `[A-Za-z0-9_-]` and in the list that CUPS gives, before core
prints. The list times out after 5 s and the print after 30 s. Core follows no redirect, uses no
proxy, and reads at most 1 MiB of a reply. Each job is logged with its queue, job id, size and
the caller's kind, never the document.

| Problem | When |
|---|---|
| 413 `label_too_large` | the body is past 8 MiB |
| 415 `label_not_pdf` | the body does not start with `%PDF-` |
| 422 `label_option_invalid` | an option value is not in the lists above |
| 422 `label_queue_unknown` | the name is malformed or not in CUPS's list, or CUPS answers `client-error-not-found` |
| 502 `label_server_unreachable` | no server (a bad stored value), or CUPS does not answer, times out, or answers bytes that are not IPP |
| 502 `label_print_refused` | CUPS answers another error status, to the queue list too; `detail` is its `status-message` (at most 300 characters) and `ipp_status` the status code |

CUPS owns the size check, the order of the jobs and the retries.

## Public routes

A public area (a route outside `/internal/crud/`, which a browser, an access token or a
service calls) checks its caller with `internal/auth` (#8038), never with a check of its
own. The check is Python's (`backend/scadbuddy/auth/`: `gate.py`, `check.py`, `routes.py`):

An area's package serves them from one function in `main.go` `publicPackages`; `newRouter`
puts each named area's routes behind the one gate, in front of the CRUD router, and every
other path still goes on to Python. An area may have CRUD routes, public routes or both:

```go
func PublicAreas(pool *pgxpool.Pool, _ *seal.Keys) map[string][]auth.Public {
	return map[string][]auth.Public{"examples": {{
		Pattern: "GET /api/v1/examples",
		Route:   auth.Route{Class: "api", Template: "/api/v1/examples"},
		Handler: http.HandlerFunc(list),
	}}}
}
```

An area that needs more than the pool and the KEK has its own `case` in `newRouter` and
mounts its routes on the same gate there. `filament-presets` does this: it reads the blob store.

- `auth.ParseMode(value, set)` reads `SCADBUDDY_AUTH_MODE` as Python does: unset is `off`,
  and a value other than `off`, `observe` or `enforce` stops the start. Never `enforce` on
  prod (the owner's decision of 2026-11-17).
- `Wrap` resolves the caller in every mode, as `AuthGate` does: a bearer `sbsvc_…` (a service
  credential) or `sbpat_…` (a person's access token), else the cookie
  `__Host-scadbuddy_session` (a person's session; its idle deadline slides once a minute).
  A revoked, expired or malformed credential, and a lookup that fails, is anonymous.
- In `observe`, a request that `enforce` would refuse is served, counted as
  `scadbuddy_auth_would_refuse_total{class, reason}` and logged once a minute per class and
  reason with the route's template. In `enforce`, it is refused with Python's problem
  (401 `…/auth/signed-out`, 403 `…/auth/forbidden`), or a WebSocket handshake with a 403.
- `Class` is the route's class in `auth/routes.py`: `api` for most routes, `person` for
  what only a person signed in themselves may do. `Scriptless` marks a GET that a browser
  makes with no script, which carries no `X-ScadBuddy-Request: 1`.
- A handler reads `auth.CallerOf(ctx)` (the kind, the principal, the session kind, a token's
  id and scope, a service's caller and scopes) and `auth.HasScope(ctx, scope)`. A caller
  holds no credential: never log a token, a cookie value or a hash.

`auth.WouldRefuse` and `auth.Refusal` are Python's `would_refuse` and `refusal`. The shared
vectors `backend/tests/fixtures/auth-vectors.json` hold the two ports equal: the backend's
`tests/auth/test_auth_vectors.py` writes them from Python's own functions
(`UPDATE_AUTH_VECTORS=1`), and `internal/auth/vectors_test.go` checks Go against them.

## Devices (#8244)

Area `devices` (`internal/devices`, design `docs/superpowers/specs/2026-11-27-devices-design.md`)
pairs the phone app to a person as a device, and lets pages, flows and the assistant call its four
methods: `nfc.read`, `nfc.write`, `code.scan` and `camera.capture`. There are no others. Python has
no such routes. The tables are `devices`, `device_pairings`, `device_tokens`, `device_calls` and
`device_reads` (`20261127T0803Z_devices`).

| Setting | Default | What |
|---|---|---|
| `SCADBUDDY_DEVICE_IDLE` | `1h` | How long a device token lives unused. Each device request moves its expiry to now + this |
| `SCADBUDDY_DEVICE_HARD_CAP` | `720h` | How long a pairing lasts at most, from the claim. A bad value of either stops the start |
| `SCADBUDDY_DEVICE_DELEGATES` | `agent,projects` | The service callers that may call a device for a person they name in `on_behalf_of`. A credential whose scopes hold `devices.call` may too. Set but empty: no caller by its name. Any other service is a 403 `no_delegation` |

Person routes are class `api` and follow `SCADBUDDY_AUTH_MODE`. With auth off, or a signed-out
request served in `observe`, the person is `anonymous`. Device routes are class `public` to the
gate: each checks the device token (`Authorization: Bearer sbdev_…`) itself, in every mode, and
refuses a missing, bad, revoked or expired one with 401 `device_unauthorized`.

| Route | Who | Body | Answer |
|---|---|---|---|
| `POST /api/v1/devices/pairings` | person | none | `{code_url, expires_at}`: `code_url` is `https://<request host>/api/v1/devices/pairings/<code>/claim` (`http` for a loopback host). The code is 16 random bytes, single-use, 10 minutes; only its SHA-256 is stored. The page shows `scadbuddy://pair?url=<code_url>` |
| `POST /api/v1/devices/pairings/{code}/claim` | the app (the code is its credential) | `{name, model?, app_version?, capabilities}` | `{device_id, token, expires_at, hard_cap_at}`. An unknown code is 401, one spent or expired 410 `code_gone`, a capability not of the four 422; none makes a device. The token is `sbdev_` and 32 random bytes; only its SHA-256 is stored |
| `GET /api/v1/devices` | person | | `{devices: [{id, name, model, app_version, capabilities, created_at, last_seen_at, online, expires_at, hard_cap_at}]}`: the caller's own, not revoked, newest first. `online` is a request in the last 75 s |
| `DELETE /api/v1/devices/{id}` | person | | 204. The device and its tokens are revoked, its open calls end `offline` (their undecided approvals are `cancelled`, and an approved one not yet used is revoked), and `device.revoked` closes its socket on every pod |
| `POST /api/v1/devices/heartbeat` | device | none | 204. The app sends it every 30 s while it is in front |
| `POST /api/v1/devices/{id}/calls[?wait=true]` | person, assistant, flow | `{method, args?, timeout_s?, on_behalf_of?}` | 202 `{call_id}`, or for an assistant's or a flow's `nfc.write` 202 `{call_id, pending_action_id}`. 403 `not_your_device` or `no_delegation`, 409 `offline` (with `"error": "offline"`), 422 `method_not_announced`. `timeout_s` is 60 by default, at most 120. With `wait=true`: 200 with the view (below) when the call ends `done` or `error`, else 504 whose `reason` is the outcome |
| `GET /api/v1/devices/calls/{id}[?on_behalf_of=]` | the device's owner, or the caller | a delegate service names the person in `on_behalf_of`, as in the call | `{call, events, result}` (below). 403 `no_delegation`, 404 `call_not_found` |
| `POST /api/v1/devices/calls/{id}/confirm` | the caller that made it | `{on_behalf_of}` for a service | 202 `{call_id}`: the approved call is sent. 409 `approval_pending`, or `no_approval`. 409 `approval_denied`, `approval_cancelled`, `approval_expired`, `approval_used`, `approval_revoked` or `approval_mismatch` end the call `denied` |
| `POST /api/v1/devices/calls/{id}/events` | the call's device, while the call is open | `{events: [{kind, data?}]}`, at most 20; `kind` `tag_found`, `tag_progress` or `tag_lost`; `data` at most 1 KiB and never `call`, `device`, `id`, `at`, `kind`, `topics` or `retention_seconds` | 204. 409 `call_closed` |
| `POST /api/v1/devices/uploads?call=&kind=` | the call's device, while the call is open | the raw bytes | `{key, media_type}`: `tag_dump` for an `nfc.*` call, `scan_image` for the others (422 `kind_mismatch`), stored with `Blobs.Put` and held in `blob_refs` as (`device_call`, the call's id). Nothing releases the hold yet (#8251). A photo's type is read from its bytes (JPEG, PNG or WebP, else 415 `not_an_image`). A dump is JSON `{uid, tech, blocks: [{index, data_hex}], records}` with no sector trailer (422 `key_trailer`) |
| `POST /api/v1/devices/calls/{id}/result` | the call's device, while the call is open | `{outcome: "error", error?}`, or `{outcome: "done", result: {read: {blob_key, uid?, tech?, complete?, missing_blocks?}}}` | 204 for an error. For done, `{read_id, uid?, complete?}`, a `device_reads` row. A blob this call's device did not upload, or of a kind not the method's, is 422 `foreign_blob`: no row, and the call ends `error` |
| `GET /api/v1/devices/reads/{id}` | as the read's call: the device's owner, or the caller | a delegate service names the person in `?on_behalf_of=` | `{id, device_id, call_id, at, kind, uid, tech, blob_key, media_type, complete, missing_blocks}` |
| `GET /api/v1/devices/reads/{id}/blob` | as the read above | `?on_behalf_of=` for a delegate | the bytes, with their media type and `nosniff` (scriptless) |

A call is `{id, device_id, method, args, caller_kind, outcome, created_at, deadline, sent_at,
ended_at, pending_action_id}`. `caller_kind` is `person` (a session cookie, or no one with auth
off), `assistant` (a person's access token, or a delegate service's credential, which names the
person in `on_behalf_of`) or `flow` (the projects worker's credential, likewise). Only a delegate
(`SCADBUDDY_DEVICE_DELEGATES`, or scope `devices.call`) may name a person; core has no proof yet
that the person asked (the agent's CRUD calls carry no turn principal), so the grant is the
check. By default only
the device's owner, and the owner's assistant and flows, may call it. `outcome` is null while the
call is open, then `done`, `error`, `offline` (the device had gone at the deadline), `timeout` (it
was online at the deadline) or `denied` (its approval never came, or was refused). Core ends a call
at its deadline: a timer on the pod that took it, and every read of the call. `events` are the
call's `device.*` events from the event log, oldest first (`{id, kind, at, data}`), at most 1000.

`nfc.write` takes `{records: [...]}`, 1 to 16 NDEF records, and nothing else: core never asks a
device to write raw blocks, sectors or keys, so it never writes a Bambu tag (the app refuses one
too). The records are the app's wire records (`NdefWire.cs` `Build`), a strict subset (`ndef.go`):
`text` (`text`, and `lang` and `encoding` if given), `url` and `absolute-url` (`text`: `scheme://host[:port]` then a path, query or fragment, with no userinfo and no opaque part, so the host an approval shows is the one it names), `mime` (`mediaType` and exactly one of `text` and `hex`), an external type `<domain>:<type>`
(exactly one of `text` and `hex`) and `empty`. Any other type or key is a 422. Core sends the device
the parsed form, and the approval's summary is built from that same form. An assistant's or a flow's `nfc.write` makes an `ai_approvals` row outside a session (tool
`device_call`, tier `outward`, 10 minutes), which the UI lists with the other pending actions. Its
`input_summary` shows all of what will be written: the record count, and every record's type and
content. A URI shows its scheme and host in full, the host in punycode; only its path, query and fragment are cut, at 80 characters.
A write whose summary would pass 2000 characters is refused (422
`write_too_large_to_approve`), never cut. It needs `SCADBUDDY_SECRET_KEY_FILE` for the input hash
(the HMAC of the device, the method and the args; 503 `not_configured` without it). The caller
confirms it once it is approved. The confirm hashes the call's stored args again (under the KEK
or the one before it) and refuses a mismatch (409 `approval_mismatch`, the call ends `denied`),
then uses the approval once. A person's own call is
sent at once: their click is the approval.

A call and its events go over the bus, as opaque events (Python has no model for them), each kept
7 days (`retention_seconds`). Core publishes `device.call_requested` `{call, device, method, args,
deadline}` on the topic `device:<device id>` and `device.revoked` `{device}` on the same topic;
each event a device posts as `device.<kind>` `{call, device, …data}`, and the end as
`device.call_done` `{call, device, read_id, uid?, complete?}` or `device.call_failed` `{call,
device, outcome, error}`, on the topics `device:<device id>` and `device:call:<call id>`. Each pod's
listener hears every event, so a call reaches the device's socket on whichever pod holds it.

The realtime socket (area `ws`) takes a device token too, checked before the gate in every mode
(401 with no body for a bad one, 403 when `devices` is not named). A device follows only
`device:<its id>`; any other topic is an `unknown topics` error. Its socket closes with 4401 on
`device.revoked` for it, and when its token is no longer live, read at each keepalive (25 s) and at
each resync. The handshake moves the expiry, as every device request does; the keepalive and resync reads do not. A person's socket may follow
`device:call:<id>` of a call whose device it owns, or that it made, at most 8 distinct call topics in one subscribe frame (a topic named more than once is read once). A device event's frame is
`{"type": "event", "id", "kind", "topics", "data"}` with the event's own fields as `data` (all but
`id`, `at`, `kind`, `topics` and `retention_seconds`): the device needs a call's method and args.
Python's socket follows no `device:` topic, so a device needs core's `ws` area.

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
  `process.unix.file_descriptor.count`, `process.uptime`). The event stream's:
  `scadbuddy.events.subscribers` (open, by `transport`: `changes` or `ws`),
  `scadbuddy.events.frames` (by `transport` and `frame`), `scadbuddy.events.resyncs` (by
  `transport` and `reason`: `place`, `gap` or `overflow`) and
  `scadbuddy.events.listener.reconnects`. `scadbuddy.auth.would_refuse` (by `class` and
  `reason`) counts the requests and sockets `observe` serves that `enforce` would refuse
  (the gate's one instrument). Always cumulative.
- **Streams.** A change stream or a socket ends its server span when it starts
  (`telemetry.Streaming`), so no span stays open for the hours a stream lasts; its
  `http.server.request.duration` is the time to its start. A socket's span has status 101.
  What a socket does later (a `check`, a session read) is a root span of its own (`ws check`,
  `ws session`), linked to the socket's span.
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
- A body bound refuses a body with no `Content-Length` (411) on the routes with one, except
  `workspace/file-writes`, which answers it in `_write_body`'s words after its Content-Type
  check (see the workspace area). Python's JSON gate counts a chunked body instead. Any other
  multipart write with no `Content-Length` is 411 before its Content-Type is checked.
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
- Inventory: a service's `use` is 409 `handler_not_built` (its consume is `consume.service`,
  which K1 does not build, R-K1-7); phase 2 answered `item_not_counted`. The other new codes
  replace phase 2's as R-K1-23 says. A problem's extensions are written `code` first, then by
  name.
- Inventory: the shop's currency is the `orders_currency` row when it holds a code, else
  `SCADBUDDY_ORDERS_CURRENCY`, else `USD`. A row that holds JSON `null` reads as no row.
- Inventory: every kind read carries `number` (from `item_kinds_number_seq`) and `code`,
  `K-<number>` (#6955, the owner's decision of 2026-11-27). A kind create with no `id`, and a
  model kind that a `compatible_with` code makes, get the id `k<number>`. A typed id of `k`
  and digits is 422 `bad_input`, as `KindInput` refuses it.
- Inventory: `GET /items/{id}/estimate` is new (Python's `estimate_unit_cost` had no route): it
  answers `{unit_cost_minor, cost_unknown}`, or the refusal a take of `qty` would give.
- Inventory: an item create or save that is not complete is 422 `missing_required_properties`
  (R-K1-33). Each `invalid[].reason` is `does not fit '<keyword>'`, and not the message of
  `jsonschema`'s `best_match`. A sent value that does not fit is 422 `bad_property_value` with
  the detail `property '<key>': does not fit '<keyword>'`. The keys, the keyword and the status
  are Python's; `store/testdata/property_values.json` holds the vectors that both languages
  run. Where a list's string does not fit, the keyword is that string's keyword, as
  `best_match` names it, not `items`. A patch with more than one bad key names the first key
  in key order; Python names the first in the order sent.
- Inventory: `GET /kinds/{kind_id}/schema` also answers `item_required` (R-K1-33): the keys an
  item of the kind must have a value for. They are the kinds' merged `required` keys, ancestor
  first, then the special type's `requires_properties`, each once (`store.Kinds.Schema`).
  `backend/scadbuddy/inventory/kinds.py` `kind_item_schema` gives the same keys;
  `store/testdata/special_requires.json` holds the `requires_properties` that both languages
  test.
- `POST /workspace/file-writes`: a body over 32 MiB is refused (413) by core's body bound in its
  words; Python's body gate refuses it in its own. A store failure's 503 names the store error's
  class (`Class()`), else `Exception`, where Python names the exception's type. A meta that is no
  JSON says `JSON decode error`, and an event that is no JSON names Go's parse error.
- `POST /fetch-grants`: a host that is not ASCII, or one that starts `xn--`, is checked by
  `golang.org/x/net/idna`'s registration profile, not Python's `idna` package. Both apply IDNA
  2008; a rare label may be taken by one and refused by the other. A port that `int()` reads
  with non-ASCII digits counts as one.
- An operation's request, result and error, and a fork's copied events, are written as
  Python's `json.loads` and `json.dumps` write them (`jsonv.Loads`), but `NaN` and `Infinity`
  in the JSON text are refused.
- An operation's activity title capitalises the kind as `str.capitalize` does for the simple
  case mappings, the final sigma and a few full title mappings; a rare character outside them
  may differ.
- `POST /forgotten-subjects/{subject}`: an ancestor whose key went while its fork's range is
  read is a 500, as in Python; a NaN in a copied event is a 500, where Python copied it.
- `filament-presets`' diff compares two values by their JSON text, not by Python's `!=`. So
  `1` and `1.0` are a change in Go. So are two nested objects that differ only in key order,
  which Python's `!=` calls equal. Bambu Studio's values are strings and string lists, and
  there the two agree.
- `filament-presets` reads a document blob of at most 1 MiB, and an unzipped member `file`
  of at most 1 MiB. A larger one is `document_too_large`. Python reads with no cap.
- `filament-presets` gives `document_unreadable` for a document that is JSON but no object.
  Python's `FilamentPresetStore.read` reads it as `{}`.
- A `filament-presets` read of a lost blob changes no row. Python's read forgets the
  `store_blobs` row.
- `package-mirrors`: a mirror past its limit is refused with the CRUD headers (Python's body gate
  answered without them). A mirror's `GET` is the whole body with no `ETag`, `Last-Modified` or
  range answers (Python's `FileResponse`). A version in a zip's path is decimal digits only
  (pydantic's lax mode also took a sign or spaces), and a last segment that does not end `.zip` is
  a 404 from Go.
- The event stream: a replayed typed event takes what pydantic's lax mode takes for its fields
  (an int as a whole float or a text of digits, a bool as `0`, `1` or a word), but not every
  other coercion pydantic makes; an event it does not take is a `resync`, as in Python. A lone
  surrogate escape in a socket frame reads as U+FFFD. The socket offers no compression
  (`permessage-deflate`); the messages are the same. Each process caps its own sockets: with
  core in front, Python's cap no longer counts them.
- The socket reads a message of at most 64 KiB, which holds Python's largest frame (16,384
  characters of up to 4 bytes each). A bigger one closes the socket with 1009; Python read up
  to 8 MiB and answered `frame too large`. Pings and pongs take from the frame rate, as
  messages do, and a pong counts only after a keepalive ping. A session's socket reads the
  session again at each `resync` and closes with 4401 when it was revoked, so a lost
  `auth.session_revoked` does not leave it open.
- A change stream ends when a frame's write waits 30 s for a client that reads nothing, and
  frees its place under the caps. Python waits on such a client.

## Tests

`go test -race ./...` with `SCADBUDDY_TEST_DATABASE_URL` naming a Postgres where the role may
create databases; without it the database tests skip. Each test gets a database migrated by the
backend's own files, in `backend/scadbuddy/migrations` and `migrations_agent`.
