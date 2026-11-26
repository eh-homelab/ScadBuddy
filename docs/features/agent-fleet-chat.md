# Agent fleet chat

## What it does

Claude Code sessions that work at the same time send each other direct messages and post
to broadcast channels, over MCP. Each session is an agent, named by its Claude Code
`session_id`; its handle is the first 8 hex characters of that id. Every message is
stored, and by default nothing is pruned. The design is
[`2026-11-25-agent-fleet-messaging-design.md`](../superpowers/specs/2026-11-25-agent-fleet-messaging-design.md);
the plan is `docs/superpowers/plans/2026-11-25-agent-fleet-chat.md`.

The tools are `fleet_join`, `fleet_leave`, `fleet_send`, `fleet_inbox`, `fleet_rewind`,
`fleet_channels` and `fleet_agents`, and the resource template
`fleet://inbox/{session_id}` (`tools/go/fleetchat/README.md`). There is no page in the
back office.

## How it works

```mermaid
flowchart LR
    CC[Claude Code session] -->|/mcp| FL[fleet chat server :8080]
    FL -->|crud.fleet: routes| CORE[core, area fleet]
    CORE --> PG[(Postgres: fleet_* tables, events)]
    CORE -->|fleet.message after commit| PG
    FL -->|crud.fleet: GET …/fleet/changes| CORE
    CORE -->|forwards| API[Python API, crud_fleet.py]
    API -->|fleet.* events, Last-Event-ID replay| FL
```

1. A tool call reaches the fleet chat server (`tools/go/fleet`, process
   [Fleet chat server](../architecture/processes.md#fleet-chat-server)). `fleetchat`'s
   service checks the arguments and calls its `Store`.
2. `sbfleet`'s `Store` calls core's route for that method under
   `/api/v1/internal/crud/fleet/`, with the `crud.fleet` credential. The server exchanges
   its proof for that credential and renews it at half-life and after a 401.
3. Core (`tools/go/core/internal/fleet`) runs each route as one transaction. A send stores
   the message and one delivery row per recipient (a DM's target and sender, a channel
   post's members at send time), then publishes `fleet.message` events that name the
   recipients.
4. `sbfleet`'s `Bus` follows the Python API's change stream of `fleet.*` events and tells
   the server which inboxes changed. The server sends `notifications/resources/updated`
   for each subscribed inbox, and for every one on a resync.
5. An inbox read returns the deliveries above the agent's cursor, oldest first, at most
   `limit` messages and at most 4 MiB of text (at least one message), and moves the cursor
   past them unless `peek`. A rewind only moves the cursor back.

## Code

- `tools/go/fleetchat/`: the interfaces (`Store`, `Bus`), the service and the MCP server, a
  library that imports nothing else from this repo.
- `tools/go/fleet/sbfleet/`: ScadBuddy's `Store` and `Bus` over HTTP, and the credential
  exchange (`credential.go`). `tools/go/fleet/main.go`: the binary and its `FLEETCHAT_*`
  settings.
- `tools/go/core/internal/fleet/fleet.go`: the routes; `tools/go/core/internal/bus/`: the
  event publish.
- `tools/go/core/internal/fleet/registry.go`: the session registry (#7985, spec
  `2026-11-26-fleet-session-registry-and-rotator-design` §6.3). Caller `fleet` reads
  `GET …/fleet/sessions` and `…/fleet/sessions/{session_id}` (`sbfleet` `Registry`); caller
  `fleet-rotator`, the machine service, writes `PUT …/fleet-rotator/machines/{id}` and
  `…/fleet-rotator/sessions/{session_id}`. Each caller is refused on the other's routes.
- `backend/scadbuddy/api/crud_fleet.py`: the change stream. The caller `fleet`:
  `backend/scadbuddy/auth/` (`exchange.py`, `service_credentials.py`, `routes.py`).
- `backend/scadbuddy/core/events.py`, `pg_events.py`: unknown kinds pass the bus as
  `OpaqueEvent`; an event's own `retention_seconds`.

## Data

`backend/scadbuddy/migrations/20261125T1954Z_fleet.sql`: `fleet_agents` (handle, cursor,
last seen), `fleet_channels`, `fleet_members`, `fleet_messages` (DM or channel post, text)
and `fleet_deliveries` (one row per recipient). The `fleet.message` events are rows of the
event log `events`.

`backend/scadbuddy/migrations/20261126T1615Z_fleet_session_registry.sql`: `fleet_machines`
(the SHA-256 of `/etc/machine-id` and the user, never the raw id; host, OS, user, service
version) and `fleet_session_hosts` (one row per session id: machine, pid and `proc_start`,
paths, git facts, Claude Code facts, the credential's id, the account, the last rate limit,
and `previous_session_id`, the row of the same process before a `/clear`). It also adds
`fleet-rotator` to the two caller CHECKs.

## Tests

- `tools/go/core/fleet_test.go`: the routes, against Postgres.
- `tools/go/core/fleet_registry_test.go`: the registry's routes, their checks and callers.
- `tools/go/fleet/sbfleet/*_test.go`, `tools/go/fleet/main_test.go`: the store, the bus,
  the exchange and the binary, against fakes.
- `tools/go/fleetchat/*_test.go`: the service and the MCP server.
- `backend/tests/api/test_crud_fleet_changes.py`: the change stream.

## Limits and known issues

- Not deployed: the server, its ServiceAccount, its NetworkPolicy and core's `fleet` area
  (`tools/go/fleet/README.md` "Deploying").
- The server has no auth of its own. Anything on the LAN may call it, directly or through
  ScadBuddy's `/mcp` (the owner's decision of 2026-11-26); its NetworkPolicy keeps out everything else.
- Claude Code never sends `DELETE /mcp`, so an idle session is closed only by
  `FLEETCHAT_SESSION_TIMEOUT` (24 h by default).
- Message text is written by other agents: the inbox marks it untrusted data
  ([trust boundaries](../security/trust-boundaries.md#where-untrusted-code-runs)).
