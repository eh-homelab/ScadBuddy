import type { EventStore } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import type { JSONRPCMessage } from '@modelcontextprotocol/sdk/types.js'

// `Last-Event-ID` resumability for `/mcp` (Streamable HTTP, "Resumability and
// Redelivery", https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
//
// In memory, one store per MCP session, bounded, and deliberately so (#264):
// it lives exactly as long as the session it replays for. A session is itself
// in memory, on the replica that opened it (src/mcp/http.ts), so after a
// restart the session id answers 404 and the client starts a new session and
// re-subscribes, as the transport spec requires ("When a client receives HTTP
// 404 in response to a request containing an MCP-Session-Id, it MUST start a
// new session", Session Management). A Postgres copy of this log would replay
// into a session that no longer exists.
//
// What IS durable is the layer below: the backend's `events` table. When the
// agent's LISTEN connection drops, src/events/pgListener.ts replays the gap
// from it by `seq`, so notifications this store then carries are not missing
// events the bus sent meanwhile.

type Stored = { id: string; streamId: string; message: JSONRPCMessage }

export class BoundedEventStore implements EventStore {
  readonly #events: Stored[] = []
  readonly #max: number
  #seq = 0

  constructor(max = 1000) {
    this.#max = max
  }

  async storeEvent(streamId: string, message: JSONRPCMessage): Promise<string> {
    // The sequence number makes ids unique and ordered; the stream id lets a
    // replay find its stream without a lookup table.
    const id = `${streamId}_${(++this.#seq).toString().padStart(10, '0')}`
    this.#events.push({ id, streamId, message })
    if (this.#events.length > this.#max) this.#events.shift()
    return id
  }

  async getStreamIdForEventId(eventId: string): Promise<string | undefined> {
    return this.#events.find((e) => e.id === eventId)?.streamId
  }

  async replayEventsAfter(
    lastEventId: string,
    { send }: { send: (eventId: string, message: JSONRPCMessage) => Promise<void> },
  ): Promise<string> {
    const index = this.#events.findIndex((e) => e.id === lastEventId)
    if (index < 0) return ''
    const streamId = this.#events[index]!.streamId
    for (const event of this.#events.slice(index + 1)) {
      if (event.streamId === streamId) await send(event.id, event.message)
    }
    return streamId
  }
}
