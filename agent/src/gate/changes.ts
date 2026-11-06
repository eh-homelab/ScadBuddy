// #815: who is told that the pending-input read may have changed. The tables of calls
// parked on a person NOTIFY `scadbuddy_pending` on every write (migration
// 20261103T1944Z_pending_input_notify.sql), heard on the event bus's own connection
// (events/pgListener.ts `listenAlso`). A NOTIFY sent while that connection is down is lost,
// so a reconnect counts as a change too. The tick carries nothing: a subscriber re-reads.

/** The channel the pending-input tables' triggers NOTIFY on. */
export const PENDING_CHANNEL = 'scadbuddy_pending'

/** What PendingChanges needs of the event bus (events/pgListener.ts `PgEventListener`). */
export type ChannelListener = {
  listenAlso(channel: string, onNotify: (payload: string) => void): void
  onReconnect(fn: () => void): void
}

export class PendingChanges {
  readonly #subscribers = new Set<() => void>()

  constructor(listener: ChannelListener) {
    listener.listenAlso(PENDING_CHANNEL, () => this.#tell())
    listener.onReconnect(() => this.#tell())
  }

  /** Calls `fn` on every change; the returned function stops it. `fn` must not block. */
  subscribe(fn: () => void): () => void {
    this.#subscribers.add(fn)
    return () => this.#subscribers.delete(fn)
  }

  #tell(): void {
    for (const fn of [...this.#subscribers]) {
      try {
        fn()
      } catch {
        // One subscriber's failure is its own; the others are still told.
      }
    }
  }
}
