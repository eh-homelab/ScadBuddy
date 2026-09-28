import { z } from 'zod'

// The agent's side of the event bus (spec §7; #264, #374).
//
// The backend publishes every state change as a small event — its `kind` plus
// the ids of what changed, never content — into its `events` log and, in the
// same transaction, `pg_notify('scadbuddy_events', <the event as JSON>)`
// (backend/scadbuddy/core/events.py, pg_events.py). The agent LISTENs on the
// same channel on a connection of its own (pgListener.ts) and hands each event
// to whoever follows the bus here: today the MCP resource hub
// (src/resources/hub.ts), later plugin hooks (#297).
//
// Events are ids only, so a consumer re-reads what changed through the API it
// already has. An event can therefore be dropped, repeated or reordered
// without anyone acting on stale data, and a consumer that may have missed
// some is told to resync (`onResync`) rather than handed a best guess.

/** The channel the backend NOTIFYs on (backend/scadbuddy/core/events.py `PG_CHANNEL`). */
export const PG_CHANNEL = 'scadbuddy_events'

/**
 * The wire form, as backend/scadbuddy/core/events.py `encode_event` writes it:
 * `id`, `at` and `kind` on every event, plus the kind's own ids. Parsed
 * loosely on purpose: a kind this agent does not know yet (a newer backend)
 * still decodes, and the resource mapping ignores it.
 */
export const BusEventSchema = z
  .object({
    id: z.string().min(1),
    kind: z.string().min(1),
    at: z.string().optional(),
    slug: z.string().optional(),
    job_id: z.string().optional(),
    output_id: z.string().optional(),
    commit: z.string().optional(),
    upstream: z.string().optional(),
    name: z.string().optional(),
    family: z.string().optional(),
    section: z.string().optional(),
  })
  .loose()

export type BusEvent = z.infer<typeof BusEventSchema>

/** The event in a NOTIFY payload or an `events.payload` row, or undefined when it is not one. */
export function decodeEvent(payload: unknown): BusEvent | undefined {
  let value = payload
  if (typeof payload === 'string') {
    try {
      value = JSON.parse(payload)
    } catch {
      return undefined
    }
  }
  const parsed = BusEventSchema.safeParse(value)
  return parsed.success ? parsed.data : undefined
}

export type EventListener = {
  /** One event, in the order this process heard it. Must not throw or block. */
  onEvent(event: BusEvent): void
  /**
   * Events may have been missed (the LISTEN connection dropped and the gap
   * could not be replayed from the log): re-read everything followed.
   */
  onResync(): void
}

/** What the resource hub follows: the Postgres listener, or a test's in-memory source. */
export interface EventSource {
  /** Starts delivering to `listener`; returns the function that stops it. */
  follow(listener: EventListener): () => void
}

/** Fan-out to followers, shared by every EventSource. A follower that throws is isolated. */
export class Followers {
  readonly #listeners = new Set<EventListener>()

  follow(listener: EventListener): () => void {
    this.#listeners.add(listener)
    return () => {
      this.#listeners.delete(listener)
    }
  }

  get size(): number {
    return this.#listeners.size
  }

  event(event: BusEvent): void {
    for (const l of [...this.#listeners]) {
      try {
        l.onEvent(event)
      } catch (err) {
        console.error(`event follower failed on ${event.kind}:`, (err as Error).message)
      }
    }
  }

  resync(): void {
    for (const l of [...this.#listeners]) {
      try {
        l.onResync()
      } catch (err) {
        console.error('event follower failed on resync:', (err as Error).message)
      }
    }
  }
}

/** An EventSource fed by hand: tests, and a deployment with no database (it never emits). */
export class MemoryEventSource implements EventSource {
  readonly #followers = new Followers()

  follow(listener: EventListener): () => void {
    return this.#followers.follow(listener)
  }

  emit(event: BusEvent): void {
    this.#followers.event(event)
  }

  resync(): void {
    this.#followers.resync()
  }
}
