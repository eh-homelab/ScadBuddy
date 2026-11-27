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
| `workspace:sql` | those, and `POST /workspace/file-deletes`, `…/sweeps` and `…/reconciles`. The status, the bytes, a write, a copy and the move to blobs stay Python's: they need the blob store or the KEK |
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
| `unknown-trays` | those, and `POST /api/v1/filament/unknown-trays/{id}/dismiss` (public). Go writes no spool here (#7143). The resolve route (`POST …/{id}/resolve`, which would start Python's activity `filament_resolve_unknown_tray` on queue `library`) is not built yet: it waits for `internal/temporalx`, so a resolve gets a 404 |
| `filament-presets` † | Public, class `api`: `GET /api/v1/filament/presets` (`?source=`, `?type_id=`), `GET …/presets/{preset_id}` and `GET …/presets/{preset_id}/diff` (`?from_version=`, `?to_version=`). `POST …/presets/bambuddy-import` stays Python's. Go writes no preset row (#7204) |

The `unknown-trays` public routes use `internal/auth` (see "Public routes"). Their CRUD route is caller `print`'s, so it has its own `case` in `newRouter` (areaPackages mounts on caller `agent`).

The `filament-presets` routes use `internal/auth` (see "Public routes"). They have their own
`case` in `newRouter`, because they need the blob store. The area needs the KEK, and it needs
an `s3` store to read documents. It reads each document with `Blobs.OpenKey` (#8129), and it
reads the blob's size from its `store_blobs` row first, so a blob over the cap gets no GET.
The document is the zip member `file`. Its credential keys are stripped, as
`core/credentials.py` strips them. A document that is not read leaves the detail's
`document` null and names why in `document_error`; the diff answers that code as a problem.

Area `fleet` (`internal/fleet`) is caller `fleet`'s (scope `crud.fleet`), under
`/api/v1/internal/crud/fleet`: `POST /agents/{session_id}/touch`, `GET /agents`, `PUT` and
`DELETE /channels/{name}/members/{session_id}`, `GET /agents/{session_id}/channels`, `POST
/messages`, `GET /agents/{session_id}/inbox`, `PUT /agents/{session_id}/cursor`. A send publishes
`fleet.message` after its commit. An inbox page holds up to `limit` messages (50 by
default, at most 200) and at most 4 MiB of message text: it stops before the message that
would pass 4 MiB, always holds at least one, and then says `more`; the cursor moves only past
the messages returned. A text over 16 MiB is a 422 whose detail names the limit (reason
`text-too-long`). A cursor `PUT` only moves it back: a `seq` past the cursor is a 422
(reason `seq-ahead`). Its `GET /changes` is area `changes` (below).

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
secret directory mounted and the variable set. `fleetd` sends a
`fleet` PAT today, which these routes refuse until #8041.

Area `blobs` † (`internal/blobstore`, #7958) is a shared area: any service credential whose scopes
hold `crud.blobs` may call it, under `/api/v1/internal/crud/blobs`; no caller is granted that scope
yet. Python has no such routes. `POST /uploads` with `{kind, size, sha256, media_type}` answers
`{upload_id, url, headers, expires_at}`: the caller PUTs exactly the bytes to `url` with `headers`,
then `POST /uploads/{upload_id}/commit` answers `{key, sha256, size}`, the blob indexed in
`store_blobs` under its kind (again, the same answer). The kinds are an allowlist with a size each:
`tag_dump`, 64 KiB. Only `store_backend` `s3` takes uploads: core reads the store's settings as
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
`Blobs.Claim`, `Blobs.PublishFile` and `Blobs.ReadFile` (#8137) are the rest of the store in process, for area `package-mirrors` (above). A publish goes through `put`: the bytes spooled and hashed, the object's stage lock held shared as a commit holds it, the object reused or stored with the caps checked (`stage`), the row written by the caller's `indexer`, and the object removed again when that writes no row and no other row names it (`removeUnnamed`). It adds no route; the upload routes and their kinds are as above.
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
