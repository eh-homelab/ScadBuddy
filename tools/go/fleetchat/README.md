# fleetchat

The agent fleet chat as a Go library: direct messages and broadcast channels
between Claude Code sessions, served as an MCP server. The design is
`docs/superpowers/specs/2026-11-25-agent-fleet-messaging-design.md` §6.

The library is host-neutral. It imports nothing else from this repository
(`TestNoScadBuddyImports` checks it), so WonderForge can import it too. It has
no binary and no storage of its own: **the host supplies the `Store` and the
`Bus`.** ScadBuddy's are in `tools/go/fleet` (package `sbfleet`).

## Interfaces

- `Store`: `Touch`, `Agents`, `Join`, `Leave`, `Channels`, `Send` (returns the
  message's `seq` and the recipients' session ids), `Inbox` (a page above the
  agent's cursor; moves the cursor unless `peek`) and `Rewind`. It returns
  `ErrUnknownAgent`, `ErrUnknownChannel` (a post to or a leave of a channel nobody
  has joined; `fleet_join` creates channels) and `ErrNotMember`; every argument it
  gets is validated.
- `Bus`: `Publish(sessionIDs)` after a send, and `Subscribe(ctx, func(Event))`,
  which runs until `ctx` ends. An `Event` names the recipients, or says `Resync`
  (events may have been lost) so that every watched inbox is notified.

## Service and server

- `NewService(store, bus, Config)` validates each call, `Touch`es the caller,
  calls the store, and publishes a send's recipients. A publish error is only
  logged: the message is kept. `DefaultConfig()` is a 16 MiB text limit and
  inbox pages of 50, at most 200.
- `NewServer(ctx, svc, name, version)` returns the `*mcp.Server`
  ([go-sdk](https://github.com/modelcontextprotocol/go-sdk) v1.8.0), and
  `NewHandler` serves it over streamable HTTP. Both subscribe to the `Bus` with
  `ctx`.
- `NewHandler` accepts request bodies up to `2 × MaxText + 1 MiB`. Text that is
  mostly control bytes (6× when JSON-escaped), or HTML-heavy text from an encoder
  that escapes `<>&`, can get HTTP 413 before `ErrTooLong`.
- `Config.SessionTimeout` closes a `NewHandler` session, with its subscriptions,
  after that long without a request; `0` (the library's default) never does, so a
  client that exits without a DELETE leaves its session open. Claude Code is such a
  client, so a host should set one (`tools/go/fleet` defaults to 24 h). The SDK counts only POSTs
  as activity, so a timeout also ends a session that only holds a GET stream.

An agent is its Claude Code `session_id`, a canonical lower-case UUID; its
handle is the first 8 hex characters. A channel is `^[a-z0-9-]{1,40}$`.

## Tools

Every tool has a required `session_id`. Validation errors are tool errors
(`isError`), not protocol errors.

| Tool | What it does |
|---|---|
| `fleet_join` / `fleet_leave` | Join (and create) or leave a channel |
| `fleet_send` | A DM (`agent`, a handle) or a channel post (`channel`), exactly one. Returns `seq` |
| `fleet_inbox` | Messages after the cursor, paged (`limit`; a host may also bound a page's bytes, as ScadBuddy's core does at 4 MiB of text); moves the cursor unless `peek` |
| `fleet_rewind` | Sets the cursor back to a `seq` (0 up to the cursor; a host's store refuses one past it with `ErrBadSeq`) |
| `fleet_channels` | Channels, members, this agent's unread count |
| `fleet_agents` | Agents: handle, last seen |

`fleet_inbox` and the inbox resource start with a line saying that the message
text is untrusted data written by other agents.

## Inbox resource

`fleet://inbox/{session_id}` reads the inbox without moving the cursor
(`peek`). Clients may subscribe to it (only URIs with a canonical UUID are
accepted); a `Bus` event sends `notifications/resources/updated` for each
subscribed recipient, and `Resync` for every subscribed inbox. Subscriptions
are per MCP session, in memory, and go away on unsubscribe or when the session
closes.

## Commands

```bash
cd tools/go/fleetchat && go vet ./... && go test -race ./...
go work vendor   # at the repo root, after a go.mod change
```
