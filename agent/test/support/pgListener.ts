import { setTimeout as sleep } from 'node:timers/promises'
import postgres, { type Sql } from 'postgres'
import { decodeEvent, type EventListener, type EventSource, Followers, PG_CHANNEL } from '../../src/events/bus.js'

// The Postgres tests' stand-in for the API's change stream (src/events/changeStream.ts, #5299 D4):
// a LISTEN connection of the test's own on a throwaway database, which hears what the agent's
// SQL and the test NOTIFY, with the stream's interface (`follow`, `listenAlso`, `onReconnect`).
// The agent itself opens no LISTEN connection; the API's stream (backend agent_changes/hub.py,
// its replay included) is tested by backend tests/crud/test_crud_agent_changes.py, and the
// agent's client of it by test/changeStream.test.ts. After a drop this stand-in replays
// nothing: it tells followers to resync, as the stream does when it cannot replay.
//
// Not postgres.js `sql.listen()`: it re-listens after a drop once, and a failed attempt is
// swallowed (postgres@3.4.9 src/index.js `listen`, `onclose`). Here one pool of one connection
// carries LISTEN, and a drop (`onclose`) restarts a retry loop with backoff.

export type PgListenerOptions = {
  /** Postgres `search_path` (tests use a throwaway schema). */
  searchPath?: string
  /** Reconnect backoff bounds (default 500 ms → 30 s). */
  retryMinMs?: number
  retryMaxMs?: number
  log?: (message: string) => void
}

export class PgEventListener implements EventSource {
  readonly #sql: Sql
  readonly #followers = new Followers()
  readonly #channels = new Map<string, (payload: string) => void>()
  readonly #reconnects: (() => void)[] = []
  readonly #retryMin: number
  readonly #retryMax: number
  readonly #log: (message: string) => void
  #closed = false
  #listening = false
  #connecting: Promise<void> | undefined
  #everListened = false

  constructor(url: string, options: PgListenerOptions = {}) {
    this.#retryMin = options.retryMinMs ?? 500
    this.#retryMax = options.retryMaxMs ?? 30_000
    this.#log = options.log ?? ((m) => console.error(m))
    const connectionOptions = {
      max: 1,
      idle_timeout: 0,
      max_lifetime: null,
      connect_timeout: 5,
      onnotice: () => {},
      onnotify: (channel: string, payload: string) => {
        if (channel === PG_CHANNEL) {
          const event = decodeEvent(payload)
          if (event) this.#followers.event(event)
        } else this.#channels.get(channel)?.(payload)
      },
      onclose: () => this.#dropped(),
      connection: {
        application_name: 'scadbuddy-agent-test-listener',
        ...(options.searchPath === undefined ? {} : { search_path: options.searchPath }),
      },
    }
    // `onnotify` is a postgres.js connection option (src/connection.js NotificationResponse) that its types do not declare.
    this.#sql = postgres(url, connectionOptions as postgres.Options<Record<string, never>>)
  }

  start(): void {
    if (this.#closed) return
    void this.#connect()
  }

  get listening(): boolean {
    return this.#listening
  }

  follow(listener: EventListener): () => void {
    return this.#followers.follow(listener)
  }

  listenAlso(channel: string, onNotify: (payload: string) => void): void {
    this.#channels.set(channel, onNotify)
    if (this.#listening) {
      void this.#sql.unsafe(`LISTEN "${channel}"`).catch((err: unknown) => {
        this.#log(`test listener: LISTEN ${channel} failed (${(err as Error).message})`)
      })
    }
  }

  onReconnect(fn: () => void): void {
    this.#reconnects.push(fn)
  }

  async close(): Promise<void> {
    this.#closed = true
    await this.#sql.end({ timeout: 5 }).catch(() => {})
  }

  async ready(timeoutMs = 10_000): Promise<void> {
    const deadline = performance.now() + timeoutMs
    while (!this.#listening) {
      if (performance.now() > deadline) throw new Error('the test listener did not connect in time')
      await (this.#connecting ?? sleep(20))
    }
  }

  /** Ends the listening connection as a network drop would. */
  async dropConnectionForTest(): Promise<void> {
    await this.#sql`SELECT pg_terminate_backend(pg_backend_pid())`.catch(() => {})
  }

  #connect(): Promise<void> {
    this.#connecting ??= this.#connectLoop().finally(() => {
      this.#connecting = undefined
    })
    return this.#connecting
  }

  async #connectLoop(): Promise<void> {
    let delay = this.#retryMin
    while (!this.#closed) {
      try {
        await this.#sql.unsafe(`LISTEN "${PG_CHANNEL}"`)
        for (const channel of this.#channels.keys()) await this.#sql.unsafe(`LISTEN "${channel}"`)
        this.#listening = true
        if (this.#everListened) {
          this.#followers.resync()
          this.#followers.reconnected()
          for (const fn of this.#reconnects) fn()
        }
        this.#everListened = true
        return
      } catch (err) {
        this.#listening = false
        if (this.#closed) return
        this.#log(`test listener: LISTEN failed (${(err as Error).message}); retrying in ${delay} ms`)
        await sleep(delay)
        delay = Math.min(delay * 2, this.#retryMax)
      }
    }
  }

  #dropped(): void {
    if (this.#closed) return
    const was = this.#listening
    this.#listening = false
    if (was && this.#connecting === undefined) void this.#connect()
  }
}
