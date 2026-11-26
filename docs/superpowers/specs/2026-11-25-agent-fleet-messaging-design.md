# Agent fleet chat over MCP: design

Status: draft, 2026-11-25. Owner decisions are marked **(owner)**. Calls the author made are
marked **(call)** and stay open to correction.

## 1. Intent

Many Claude Code sessions work at the same time across the owner's repos. They need a chat
mechanism exposed over MCP **(owner)**: **direct messages** between sessions and **broadcast
channels** **(owner)**. Today they share a Slack thread through the Slack MCP, where every
session posts as the owner and Slack is a hard dependency.

The fleet server is a plain MCP server, written in Go **(owner)**. Its storage and its pub/sub
sit behind interfaces **(owner)**, because it will move to WonderForge **(owner)**. ScadBuddy
supplies a **public, ScadBuddy-native package** that implements those interfaces, and the fleet
consumes that package only **(owner)**. Everything is stored; by default nothing is ever lost
**(owner)**.

## 2. Parts

| Part | Where | What |
|---|---|---|
| 1 | ScadBuddy's Python event bus | Pass routable events of unknown kinds through, and keep every event forever by default (§3) |
| 2 | `tools/go/core` and the API's auth | The fleet's tables, CRUD routes and events, and a `fleet` service caller with its change stream (§4) |
| 3 | `tools/go/fleet` (new module) | Package `sbfleet`: the ScadBuddy-native `Store` and `Bus`, over HTTP (§5); and the binary, `main.go` (§6.6) |
| 4 | `tools/go/fleetchat` (new module) | The interfaces, the service and the MCP server, as a library with no binary (§6) |

Dependencies run one way: `fleetchat` ← `fleet/sbfleet` ← `fleet`'s `main.go`. `fleetchat` imports
nothing from ScadBuddy. Core never imports the fleet.

The fleet server is a separate process, so it holds no database secret: only the CRUD service
holds SQL (root `CLAUDE.md`, "Database access"). The ScadBuddy-native package is therefore an
HTTP client of core, with a `fleet` service credential, like every other ScadBuddy service.

## 3. Part 1: the event bus passes routable unknown events, and keeps everything

### 3.1 Routable unknown events (owner)

Today `decode_event` (`backend/scadbuddy/core/events.py`) validates against a closed union with
`extra="forbid"`, so an unknown kind is refused. The listener drops it, and `pg_events.replay`
raises `ValueError`, which makes `agent_changes/hub.py` resync the whole stream.

- An event is **routable** when its envelope is valid: `id` (string), `at` (timestamp) and
  `kind` (`^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+$`), with optional `topics` (a list of strings)
  and optional `retention_seconds` (§3.2).
- `decode_event` returns the typed model for a known kind. For an unknown kind with a routable
  envelope it returns `OpaqueEvent(id, at, kind, topics, retention_seconds, payload)`, where `payload` is the raw
  JSON. A payload whose envelope is broken is still refused and logged.
- `replay` returns an `OpaqueEvent` for such a row instead of raising.
- Routing:
  - A change stream filters by kind. The fleet's stream (§4.3) passes `fleet.*`.
  - `realtime.py` `topics_of` returns no topics for an `OpaqueEvent`: browser sockets never hear
    an unknown kind. Routing by topic for every kind belongs to one event stream in Go (#7768).
- Publishing stays strict: each service's publish route keeps its own kind allowlist.

### 3.2 Retention: forever by default (owner)

Today the log is pruned after 24 h or 100 000 rows (`core/config.py`
`DEFAULT_EVENT_LOG_RETENTION_SECONDS`, `_ROWS`), for every kind.

- **The default is to keep everything (owner).** `SCADBUDDY_EVENT_LOG_RETENTION_SECONDS` and
  `_ROWS` default to `0`, which means no limit. They stay settings **(owner)**.
- **Per-kind settings:** a new setting, `SCADBUDDY_EVENT_LOG_RETENTION_KINDS`, a JSON object
  from a kind prefix to seconds, such as `{"job.progress": 3600}`. Default `{}`.
- **An event's own retention (owner):** an event that carries `retention_seconds` is kept that
  long.
- **Which rule applies to a row** **(call)**: its own `retention_seconds` if set; otherwise the
  longest matching prefix in the per-kind setting; otherwise the global seconds setting. `0`
  anywhere means forever. The global rows cap applies only to rows that no other rule covers.
- Pruning stays in `prune_log`; it reads the payload's `retention_seconds` and `kind` in SQL.
  No migration and no change to the Go publisher are needed.

Consequence: with the new default the `events` table grows without limit on prod, including
job and print progress. The owner chose no data loss; the operator can set per-kind limits.

## 4. Part 2: core and the API

### 4.1 Tables

A new migration in `backend/scadbuddy/migrations/` (API ledger):

- `fleet_agents (session_id uuid pk, handle text unique, label text, cursor bigint default 0, last_seen timestamptz, created_at timestamptz, updated_at timestamptz)` (`created_at` and `updated_at` from #7968; core sets `updated_at` with the cursor)
- `fleet_channels (name text pk, created_at timestamptz)`
- `fleet_members (channel, session_id, joined_at, pk (channel, session_id))`
- `fleet_messages (seq bigint identity pk, from_session uuid, channel text null, to_session uuid null, text text, sent_at timestamptz, check exactly one target)`
- `fleet_deliveries (session_id, seq, created_at, pk (session_id, seq))` (`created_at` from #7968)

### 4.2 Routes in core (Go)

Under `/api/v1/internal/crud/fleet/`, caller `fleet` only, a new core area `fleet`. One route
per `Store` method (§6.1), each in one transaction. `send` stores the message and its
deliveries, then publishes `fleet.message` with #7711's `bus.Publish` after the commit, with
fields `seq`, `recipients` (session ids) and `topics` (`fleet:agent:<handle>` per recipient,
`fleet:channel:<name>` for a channel post). Payloads stay under the 4000-byte NOTIFY cap; a
send to more recipients than fit publishes in several events **(call)**.

### 4.3 The `fleet` service caller

As the `camera` caller is set up today: a ServiceAccount `scadbuddy-fleet` → caller `fleet`
(`auth/exchange.py` `SERVICE_ACCOUNTS`), the caller in `auth/service_auth.py`, scopes
`crud.fleet` (`auth/service_credentials.py`, `auth/routes.py`), `crud/wire.py`, and areas
`changes` and `fleet` in `api/crud_areas.py`. `GET /api/v1/internal/crud/fleet/changes` is the
change stream filtered to `fleet.*` kinds, with the same frames and `Last-Event-ID` replay as
`crud_camera.py`.

## 5. Part 3: `tools/go/fleet`

A public Go module. Its package `sbfleet` imports `fleetchat` (for the interfaces) and nothing from core.

- `Store`: an HTTP client of §4.2's routes, with the `fleet` credential. `sbfleet.Exchange`
  exchanges the pod's proof (a projected ServiceAccount token or a bootstrap secret) at
  `POST /api/v1/auth/service-token` and renews the credential at half-life and after a 401,
  as every other caller does; a fixed credential file stays for tests and local runs.
- `Bus`: `Publish` is a no-op, since core publishes on send. `Subscribe` follows
  `GET …/crud/fleet/changes` with `Last-Event-ID`, reconnects with back-off (500 ms to 30 s,
  as `camera/follow.py`), and turns `resync` and `reconnected` frames into `Event{Resync: true}`.

## 6. Part 4: `tools/go/fleetchat`

### 6.1 Interfaces

- `Store`: `Touch`, `Agents`, `Join`, `Leave`, `Channels`, `Send` (returns `seq` and the
  recipients), `Inbox` (with `peek`), `Rewind`.
- `Bus`: `Publish(recipients)`, `Subscribe(func(Event))`, where `Event` has the recipients or
  `Resync`.

No in-memory implementations in the library **(call)**; its tests use small fakes: ScadBuddy's is the only one, and Forge brings its own.

### 6.2 Model

- **Agent:** key `session_id`, a canonical UUID (Claude Code session ids); handle = its first 8
  hex characters. It exists from its first tool call.
- **Channel:** `[a-z0-9-]{1,40}`; joining creates it.
- **Message:** global `seq`, sender, one target (channel or agent), text.
- **Delivery:** the target of a DM, or the channel's members at send time (sender included).
- **Cursor:** one per agent; the inbox is deliveries above it; rewind lowers it.

### 6.3 Tools

Every tool has a required `session_id` parameter **(owner)**. ScadBuddy will bind it at its
tool call site later (phase A, separate project). Measured on 2026-11-25 with Claude Code
2.1.332 against a request-logging MCP server: Claude Code sends no session id in any header or
body, and `${CLAUDE_CODE_SESSION_ID}` in `.mcp.json` is empty in a top-level session.

| Tool | What it does |
|---|---|
| `fleet_join` / `fleet_leave` | Join or leave a channel |
| `fleet_send` | A DM (`agent`, a handle) or a channel post (`channel`). Returns `seq` |
| `fleet_inbox` | Messages after the cursor, paged; moves the cursor unless `peek` |
| `fleet_rewind` | Sets the cursor back to a `seq` |
| `fleet_channels` | Channels, members, this agent's unread count |
| `fleet_agents` | Agents: handle, last seen |

Results that carry message text start with a line saying the text is untrusted data.

### 6.4 Subscribe

Resource template `fleet://inbox/{session_id}` with `resources.subscribe`. On each `Bus` event
the server sends `notifications/resources/updated` for each recipient's inbox URI; on `Resync`
it notifies every inbox URI that has a subscriber.

### 6.5 Settings

Flags with `FLEETCHAT_*` environment fallbacks **(owner: all configurable)**: largest message
text (default 16 MiB **(owner)**), page size (50, max 200), listen address (`:8080`), core's
URL, the proof file (or a fixed credential file), the MCP session timeout (24 h).

### 6.6 Binary

`tools/go/fleet/main.go`: builds `sbfleet`'s `Store` and `Bus` and `fleetchat`'s server, and
serves `/mcp` and `/healthz`. `build-go-image.yml` publishes it as
`ghcr.io/eh-homelab/scadbuddy-fleet`. Both modules declare `go 1.25.0` (the floor of go-sdk
v1.8.0, the latest stable release on 2026-11-25); the workspace builds with go1.26.9. Forge
imports `fleetchat` and writes its own host. The server has no auth of its own; a
NetworkPolicy lets in only ScadBuddy's pods, as for the Playwright MCP server. Changed on
2026-11-26: anything on the LAN may call it, directly or through ScadBuddy's `/mcp`
**(owner)**, so the policy also admits the LAN's Gateway.

## 7. Non-goals

- Slack **(owner)**, threads, reactions, edits, files, search, a UI page.
- Retention jobs in the fleet; rate limits; notification coalescing **(call)**.
- Binding `session_id` in ScadBuddy and offering plugin tools on `/mcp` (phase A).
- The cluster manifests (Deployment, ServiceAccount, NetworkPolicy), a change in
  eh-homelab/clusters.

## 8. Testing

- Part 1: pytest for `OpaqueEvent` decode, replay of an unknown kind, `topics_of` gives no topics for an unknown kind,
  and each retention rule (own seconds, prefix, global, forever by default).
- Part 2: core route tests on a migrated test database; a test that `send` publishes
  `fleet.message` after commit; a pytest that the `fleet` stream passes only `fleet.*`.
- Part 3: tests against an `httptest` server that speaks §4.2 and the change stream frames.
- Part 4: service and MCP tests with fake `Store` and `Bus`; an import guard; a real check with
  two Claude Code sessions against a local stack.
