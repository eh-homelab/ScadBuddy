import { setTimeout as sleep } from 'node:timers/promises'
import postgres, { type Sql } from 'postgres'
import { type BusEvent, decodeEvent, type EventListener, type EventSource, Followers, PG_CHANNEL } from './bus.js'

// LISTEN on `scadbuddy_events` (spec §7), on a connection of the agent's own.
//
// Postgres delivers a NOTIFY to every session listening on the channel
// (https://www.postgresql.org/docs/current/sql-notify.html), so the backend's
// own listener and this one each get every event; neither relays for the
// other. A NOTIFY is delivered only when its transaction commits ("if a NOTIFY
// is executed inside a transaction, the notify events are not delivered until
// and unless the transaction is committed", same page), and the backend
// appends the event to its `events` log in that transaction
// (backend/scadbuddy/core/pg_events.py), so any event heard here is already
// readable in the log.
//
// THE GAP. A NOTIFY sent while this connection is down reaches nobody here
// (https://www.postgresql.org/docs/current/sql-listen.html: a session receives
// notifications only while it is listening). So the listener keeps a place in
// the backend's `events` log (its `seq`), and when LISTEN is re-established it
// replays the rows after that place before carrying on, skipping ids it has
// already delivered. If the gap is larger than `replayLimit`, or the log
// cannot be read (pruned, not migrated yet, a different schema), followers are
// told to resync instead: re-read what they follow.
//
// The place is advanced lazily: every `checkIntervalMs` the listener reads
// max(seq) and adopts the value it read on the PREVIOUS check. An event that
// old committed at least one interval ago, so a live connection has heard it;
// the lag costs a few duplicate deliveries after a reconnect, which the seen
// set and the ids-only events make harmless.
//
// ASSUMPTION: no transaction that inserts into `events` stays open longer
// than `checkIntervalMs` (30 s). `seq` is handed out at INSERT but becomes
// visible at COMMIT, so a transaction open longer than that could commit a
// row BELOW the adopted place. If the listening connection were down when
// that row's NOTIFY went out, the replay (`seq > place`) would never read it
// and the event would be lost for good. The seen set cannot catch this: it
// stops repeats, not skips. The backend keeps well inside the bound: every
// event is one short INSERT + pg_notify transaction
// (backend/scadbuddy/core/pg_events.py, "Publishing"), and the render queue's
// in-transaction events commit with a single job update.
//
// GUARD: a replayed row whose `logged_at` (its transaction's start, the
// column's `DEFAULT now()`) is more than `checkIntervalMs` before the time
// the adopted place was read came from a transaction open longer than the
// bound. The assumption has been broken, and some other event may have been
// skipped this way, so followers are told to resync after the replay.
//
// Why not postgres.js `sql.listen()`: it re-listens after a drop once, and a
// failed attempt is swallowed (postgres@3.4.9 src/index.js `listen`,
// `onclose`), after which nothing is heard until a restart. Here one pool of
// one connection carries LISTEN and the log reads, and a drop (`onclose`)
// restarts a retry loop with backoff.

export type PgListenerOptions = {
  /** Postgres `search_path` (tests use a throwaway schema). */
  searchPath?: string
  /** Reconnect backoff bounds (default 500 ms → 30 s). */
  retryMinMs?: number
  retryMaxMs?: number
  /** How often the connection is checked and the log place advanced (default 30 s). */
  checkIntervalMs?: number
  /** Most events one reconnect replays before it resyncs instead (default 1000). */
  replayLimit?: number
  /** Event ids remembered for de-duplication (default 5000). */
  seenMax?: number
  log?: (message: string) => void
}

type Row = { seq: string; event_id: string; payload: unknown; logged_at: Date; read_at: Date }

export class PgEventListener implements EventSource {
  readonly #sql: Sql
  readonly #followers = new Followers()
  readonly #seen = new Set<string>()
  readonly #retryMin: number
  readonly #retryMax: number
  readonly #checkMs: number
  readonly #replayLimit: number
  readonly #seenMax: number
  readonly #log: (message: string) => void
  #timer: NodeJS.Timeout | undefined
  #closed = false
  #listening = false
  #connecting: Promise<void> | undefined
  #everListened = false
  /** The last `events.seq` known to be delivered; undefined while the log is unreadable. */
  #position: bigint | undefined
  /** The database clock when `#position` was read, for the guard above. */
  #positionAt: Date | undefined
  /** max(seq) as the previous check read it; becomes `#position` on the next. */
  #candidate: { seq: bigint; at: Date } | undefined
  /** Replays that found a transaction open longer than `checkIntervalMs` (tests, metrics). */
  longTransactions = 0
  /** How many events came from the replay log rather than NOTIFY (tests, metrics). */
  replayed = 0
  /** How many resyncs were sent (tests, metrics). */
  resyncs = 0

  constructor(url: string, options: PgListenerOptions = {}) {
    this.#retryMin = options.retryMinMs ?? 500
    this.#retryMax = options.retryMaxMs ?? 30_000
    this.#checkMs = options.checkIntervalMs ?? 30_000
    this.#replayLimit = options.replayLimit ?? 1000
    this.#seenMax = options.seenMax ?? 5000
    this.#log = options.log ?? ((m) => console.error(m))
    const connectionOptions = {
      max: 1,
      // Keep the one connection for as long as it lives: LISTEN is per session.
      idle_timeout: 0,
      max_lifetime: null,
      connect_timeout: 5,
      onnotice: () => {},
      onnotify: (channel: string, payload: string) => {
        if (channel === PG_CHANNEL) this.#heard(payload)
      },
      onclose: () => this.#dropped(),
      ...(options.searchPath === undefined ? {} : { connection: { search_path: options.searchPath } }),
    }
    // `onnotify` is a postgres.js connection option (src/connection.js
    // NotificationResponse) that its types do not declare.
    this.#sql = postgres(url, connectionOptions as postgres.Options<Record<string, never>>)
  }

  /** Starts listening, retrying in the background until it succeeds. Never throws. */
  start(): void {
    if (this.#timer !== undefined || this.#closed) return
    void this.#connect()
    this.#timer = setInterval(() => void this.#check(), this.#checkMs)
    this.#timer.unref()
  }

  get listening(): boolean {
    return this.#listening
  }

  follow(listener: EventListener): () => void {
    return this.#followers.follow(listener)
  }

  async close(): Promise<void> {
    this.#closed = true
    clearInterval(this.#timer)
    await this.#sql.end({ timeout: 5 }).catch(() => {})
  }

  /** Waits until LISTEN is in place (tests). */
  async ready(timeoutMs = 10_000): Promise<void> {
    const deadline = Date.now() + timeoutMs
    while (!this.#listening) {
      if (Date.now() > deadline) throw new Error('the event listener did not connect in time')
      await (this.#connecting ?? sleep(20))
    }
  }

  /** Ends the listening connection as a network drop would (tests). */
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
        this.#listening = true
        const reconnected = this.#everListened
        if (reconnected) await this.#replayGap()
        else this.#adopt(await this.#maxSeq())
        this.#everListened = true
        // After the replay: NOTIFY-only kinds (bus.ts `onReconnect`) are re-read now.
        if (reconnected) this.#followers.reconnected()
        return
      } catch (err) {
        this.#listening = false
        if (this.#closed) return
        this.#log(`event bus: LISTEN ${PG_CHANNEL} failed (${(err as Error).message}); retrying in ${delay} ms`)
        await sleep(delay)
        delay = Math.min(delay * 2, this.#retryMax)
      }
    }
  }

  #dropped(): void {
    if (this.#closed) return
    const was = this.#listening
    this.#listening = false
    // A drop while connecting is the loop's own failure; it retries itself.
    if (was && this.#connecting === undefined) void this.#connect()
  }

  async #check(): Promise<void> {
    if (!this.#listening || this.#closed) return
    const max = await this.#maxSeq()
    if (max === undefined) return
    if (this.#candidate !== undefined && (this.#position === undefined || this.#candidate.seq > this.#position)) {
      this.#adopt(this.#candidate)
    }
    this.#candidate = max
  }

  #adopt(place: { seq: bigint; at: Date } | undefined): void {
    this.#position = place?.seq
    this.#positionAt = place?.at
  }

  /** max(seq) of the log and the database clock, or undefined when the log cannot be read. */
  async #maxSeq(): Promise<{ seq: bigint; at: Date } | undefined> {
    try {
      const [row] = await this.#sql<{ seq: string; at: Date }[]>`
        SELECT coalesce(max(seq), 0)::text AS seq, now() AS at FROM events`
      return { seq: BigInt(row?.seq ?? '0'), at: row?.at ?? new Date() }
    } catch {
      return undefined
    }
  }

  async #replayGap(): Promise<void> {
    const from = this.#position
    if (from === undefined) {
      this.#adopt(await this.#maxSeq())
      this.#resync('the event log could not be read before the drop')
      return
    }
    let rows: Row[]
    try {
      rows = await this.#sql<Row[]>`
        SELECT seq::text AS seq, event_id, payload, logged_at, now() AS read_at FROM events
        WHERE seq > ${from.toString()}::bigint
        ORDER BY seq DESC LIMIT ${this.#replayLimit + 1}`
    } catch (err) {
      this.#adopt(undefined)
      this.#resync(`the event log could not be read (${(err as Error).message})`)
      return
    }
    if (rows.length > this.#replayLimit) {
      this.#adopt({ seq: BigInt(rows[0]!.seq), at: rows[0]!.read_at })
      this.#resync(`more than ${this.#replayLimit} events were missed`)
      return
    }
    const placeAt = this.#positionAt
    let longTransaction = false
    for (const row of rows.reverse()) {
      if (placeAt !== undefined && row.logged_at.getTime() < placeAt.getTime() - this.#checkMs) longTransaction = true
      const seq = BigInt(row.seq)
      if (!this.#seen.has(row.event_id)) {
        const event = decodeEvent(row.payload)
        if (event) {
          this.replayed += 1
          this.#deliver(event)
        }
      }
      if (seq > (this.#position ?? 0n)) this.#adopt({ seq, at: row.read_at })
    }
    this.#candidate = undefined
    if (longTransaction) {
      this.longTransactions += 1
      this.#resync(
        `a replayed event's transaction was open more than ${this.#checkMs} ms, so an event may have been ` +
          'skipped (see the ASSUMPTION in agent/src/events/pgListener.ts)',
      )
    }
  }

  #resync(reason: string): void {
    this.resyncs += 1
    this.#log(`event bus: resyncing followers: ${reason}`)
    this.#followers.resync()
  }

  #heard(payload: string): void {
    const event = decodeEvent(payload)
    if (!event) return
    this.#deliver(event)
  }

  #deliver(event: BusEvent): void {
    if (this.#seen.has(event.id)) return
    this.#seen.add(event.id)
    if (this.#seen.size > this.#seenMax) {
      const oldest = this.#seen.values().next().value
      if (oldest !== undefined) this.#seen.delete(oldest)
    }
    this.#followers.event(event)
  }
}
