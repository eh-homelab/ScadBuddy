import type { EventStore } from '@modelcontextprotocol/sdk/server/webStandardStreamableHttp.js'
import type { JSONRPCMessage } from '@modelcontextprotocol/sdk/types.js'

// `Last-Event-ID` resumability for `/mcp` (Streamable HTTP, "Resumability and
// Redelivery", https://modelcontextprotocol.io/specification/2025-06-18/basic/transports).
//
// IN MEMORY FOR NOW: one store per MCP session, bounded, gone when the session
// ends or the process restarts, and not shared between replicas. Issue #251
// and spec §9 put the event log in Postgres (`ai_*` tables); that lands with
// #255's migration framework and the event bus (#264).

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
