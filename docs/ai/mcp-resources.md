# MCP resources and subscriptions

What `/mcp` offers besides tools: ScadBuddy state as read-only `scadbuddy://`
resources, and subscriptions that notify a client when that state changes, driven by
the Postgres event bus. Issue [#264](https://github.com/eh-homelab/ScadBuddy/issues/264);
spec §5.4, §7 and §9 of
[`docs/superpowers/specs/2026-09-27-ai-integration-design.md`](../superpowers/specs/2026-09-27-ai-integration-design.md).
The code is [`agent/src/resources/`](../../agent/src/resources/) and
[`agent/src/events/`](../../agent/src/events/).

Protocol references are the MCP 2025-11-25 pages, the latest version the pinned
`@modelcontextprotocol/sdk` 1.30.1 implements (`LATEST_PROTOCOL_VERSION` in its
`types.js`): [Resources](https://modelcontextprotocol.io/specification/2025-11-25/server/resources),
[Completion](https://modelcontextprotocol.io/specification/2025-11-25/server/utilities/completion),
[Streamable HTTP transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

## 1. For MCP clients

### Capabilities

The server declares
`resources: { subscribe: true, listChanged: true }` and `completions: {}`
(`installResources()` in [`server.ts`](../../agent/src/resources/server.ts)).

### Resources

Every resource is read by a `read` tool of the registry (`RESOURCES` in
[`catalog.ts`](../../agent/src/resources/catalog.ts)), so its content is exactly what
that tool returns, in the MIME type below.

| URI | MIME type | Tool |
|---|---|---|
| `scadbuddy://models` | `application/json` | `list_models` |
| `scadbuddy://models/{slug}` | `application/json` | `get_model` |
| `scadbuddy://models/{slug}/source` | `text/x-openscad` | `get_source` |
| `scadbuddy://models/{slug}/schema` | `application/json` | `get_schema` |
| `scadbuddy://models/{slug}/readme` | `text/markdown` | `get_readme` |
| `scadbuddy://models/{slug}/thumbnail` | `image/png` (blob) | `get_model_thumbnail` |
| `scadbuddy://models/{slug}/diagnostics` | `application/json` | `get_render_diagnostics` |
| `scadbuddy://models/{slug}/outputs` | `application/json` | `list_outputs` |
| `scadbuddy://models/{slug}/versions` | `application/json` | `list_versions` |
| `scadbuddy://models/{slug}/versions/{commit}` | `text/x-openscad` | `get_version_source` |
| `scadbuddy://models/{slug}/versions/{commit}/diff` | `application/json` | `diff_version` |
| `scadbuddy://models/{slug}/versions/{commit}/schema` | `application/json` | `get_schema` (`version`) |
| `scadbuddy://models/{slug}/upstream` | `application/json` | `get_upstream` |
| `scadbuddy://jobs/{job_id}` | `application/json` | `get_render_job` |
| `scadbuddy://outputs/{output_id}` | `application/json` | `get_output` |
| `scadbuddy://outputs/{output_id}/plates` | `application/json` | `get_output_plates` |
| `scadbuddy://outputs/{output_id}/thumbnail` | `image/png` (blob) | `get_output_image` |
| `scadbuddy://outputs/{output_id}/plates/{plate}/thumbnail` | `image/png` (blob) | `get_output_image` (`plate`) |
| `scadbuddy://outputs/{output_id}/model.3mf` | `model/3mf` (blob) | `download_3mf` |
| `scadbuddy://print/outputs/{output_id}/progress` | `application/json` | `get_print_progress` |
| `scadbuddy://plates` | `application/json` | `list_plates` |
| `scadbuddy://libraries` | `application/json` | `list_libraries` |
| `scadbuddy://fonts` | `application/json` | `list_fonts` |
| `scadbuddy://settings` | `application/json` | `get_settings` (secrets redacted) |

- `resources/list` returns the five fixed resources and one `scadbuddy://models/{slug}`
  per model; everything else is reached through `resources/templates/list`.
- **URIs.** Templates are RFC 6570 level 1
  ([§3.2.2](https://datatracker.ietf.org/doc/html/rfc6570#section-3.2.2)), one path
  segment per variable, percent-encoded. A bundled template's slug `builtin:x` is
  `scadbuddy://models/builtin%3Ax`; the unencoded spelling is accepted too, and
  notifications always use the encoded one.
- **Binary content** is a base64 `blob`. Above the 8 MiB inline limit
  (`DEFAULT_MAX_INLINE_BYTES`, [`agent/src/tools/binary.ts`](../../agent/src/tools/binary.ts))
  the content is instead an `application/json` note naming the backend route to fetch.
- **Errors** follow the resources page's "Error Handling": `-32002` when the URI names
  no resource or the backend answers 404; `-32602` when a variable fails the tool's
  validation (a malformed slug); `-32603` otherwise.
- **Completion.** `completion/complete` with `ref/resource` offers slugs, and commits
  and output ids for the `slug` given in `context.arguments`.

### Subscriptions

`resources/subscribe { uri }` then, whenever that resource changes,
`notifications/resources/updated { uri }`; re-read it with `resources/read`.
`resources/unsubscribe` stops it. `notifications/resources/list_changed` is sent when a
model is created or deleted. What each backend event updates is `affectedBy()` in
[`events.ts`](../../agent/src/resources/events.ts):

| Event | Resources updated |
|---|---|
| `job.pending`, `job.running`, `job.superseded` | `jobs/{job_id}` |
| `job.done`, `job.failed` | `jobs/{job_id}`, `models/{slug}/diagnostics` |
| `model.created`, `model.deleted` | `models`, `models/{slug}` and its source, schema, readme, thumbnail, versions; **list_changed** |
| `model.updated` | `models`, `models/{slug}`, readme, thumbnail, upstream |
| `source.changed` | `models/{slug}/source`, `…/schema` |
| `version.committed` | `models/{slug}`, `…/versions` |
| `upstream.available` | `models/{slug}`, `…/upstream` |
| `output.created`, `output.deleted` | `models/{slug}/outputs`, `…/thumbnail`, `outputs/{output_id}`, `…/plates` |
| `print.progress`, `print.settled` | `print/outputs/{output_id}/progress` |
| `library.changed` | `libraries`, `models/{slug}` |
| `library.removed` | `libraries` |
| `font.installed` | `fonts` |
| `settings.changed` | `settings` |

- **Coalescing.** Per session and per URI, at most one notification per 250 ms
  (`DEFAULT_MIN_INTERVAL_MS`, [`hub.ts`](../../agent/src/resources/hub.ts)): the first
  at once, then one at the end of the window for everything inside it.
- **Limit.** 500 subscriptions per session (`DEFAULT_MAX_SUBSCRIPTIONS`).
- **Delivery.** Notifications go on the session's GET SSE stream. Open it (the SDK
  clients do so after `initialize`) before relying on notifications: one sent while no
  GET stream has ever been open is stored but only reaches a client that resumes.
- **Resuming.** Every notification is stored with an SSE event id
  ([`agent/src/mcp/eventStore.ts`](../../agent/src/mcp/eventStore.ts), 1000 per
  session). A client that reconnects with `Last-Event-ID` gets what it missed, as the
  transport's "Resumability and Redelivery" describes.
- **After an agent restart** the session id answers 404 and, per the transport's
  "Session Management", the client "MUST start a new session", then subscribes again.
  Subscriptions are not persisted (spec §9 records why).

### Tiers

The same `authenticate → principal` check as tools (spec §8.1), re-read on every
request. Every resource needs `read`, except `scadbuddy://settings`, which needs
`write`: even redacted settings reveal the environment (issue #264). A caller never
sees a resource it may not read in either list, and `resources/read` and
`resources/subscribe` refuse it. Resources are read-only: `installResources()` refuses
to start if any resource is backed by a tool that is not `read`.

## 2. For operators

### The event source

The agent LISTENs on `scadbuddy_events`, the channel the backend NOTIFYs on
(`PG_CHANNEL`, [`backend/scadbuddy/core/events.py`](../../backend/scadbuddy/core/events.py)),
on one dedicated connection of its own (`PgEventListener`,
[`agent/src/events/pgListener.ts`](../../agent/src/events/pgListener.ts)). It needs
`SCADBUDDY_DATABASE_URL` and nothing else; without a database `/mcp` is not served at
all (`createApp()`, [`agent/src/app.ts`](../../agent/src/app.ts)).

- **Point the URL at the primary.** On a hot standby `LISTEN` and `NOTIFY` are refused
  ([PostgreSQL: Hot Standby](https://www.postgresql.org/docs/current/hot-standby.html)).
  With CloudNativePG use the `-rw` service, which "Points to the primary instance of the
  cluster" ([CloudNativePG: Service management](https://cloudnative-pg.io/docs/devel/service_management)).
- **Replicas need nothing extra.** Postgres delivers a NOTIFY to every session
  listening on the channel ([NOTIFY](https://www.postgresql.org/docs/current/sql-notify.html)),
  so every agent and backend replica hears every event. An MCP session lives on the
  replica that opened it, so each replica notifies only its own sessions.
- **No transaction-mode pooler in between.** `LISTEN` belongs to a server session; a
  pooler that hands the connection to someone else between transactions would drop it.
  **Unverified** for any particular pooler.
- **Reconnects.** When the listening connection drops, the listener retries with
  backoff (500 ms to 30 s) and logs each failure. On reconnect it replays the backend's
  `events` log after the last `seq` it knows (backend
  [`pg_events.py`](../../backend/scadbuddy/core/pg_events.py); pruned by
  `SCADBUDDY_EVENT_LOG_RETENTION_*`), skipping ids it already delivered. More than 1000
  missed events, or a log it cannot read, makes it log
  `event bus: resyncing followers: …` and send every subscription an update plus one
  `list_changed`, so clients re-read.
- **The replay rests on short event transactions.** The listener's place in the log
  trails the newest `seq` by one 30 s check, because a `seq` is handed out at INSERT
  but becomes visible only at COMMIT. That is safe only while no transaction that
  writes to `events` stays open longer than 30 s. One that did could commit an event
  behind the place, and if the listener were reconnecting at the time, that event would
  be lost for good; the duplicate filter stops repeats, not skips. The backend keeps
  well inside this: each event is one short INSERT + `pg_notify` transaction
  ([`pg_events.py`](../../backend/scadbuddy/core/pg_events.py), "Publishing"). As a
  guard, a replayed event whose `logged_at` (its transaction's start) is more than 30 s
  older than the place shows the bound was broken; the listener then logs
  `event bus: resyncing followers: a replayed event's transaction was open more than …`
  and resyncs every subscriber (the ASSUMPTION and GUARD comments in `pgListener.ts`).
- **Schemas.** The listener reads `events` through the connection's `search_path`, so
  the agent and the backend must share a schema; channels are per database, so two
  deployments sharing a database would hear each other (the same caveat the backend's
  `pg_events.py` states).

### What is not here yet

Issue #264 also lists resources that need a backend route or event source not on
`main`: Bambuddy printers, queue, inventory, print history and stats (print watcher,
#268), `scadbuddy://browser/{tab}/snapshot` (#254), `scadbuddy://docs/authoring` and
LSP diagnostics (#252), and sessions (#300). The agent does not yet publish
`session.*` on the bus, so the session event log still polls
([`agent/src/sessions/eventLog.ts`](../../agent/src/sessions/eventLog.ts) `wake()`).
