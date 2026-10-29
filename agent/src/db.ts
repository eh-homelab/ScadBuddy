import postgres, { type ReservedSql, type Sql } from 'postgres'
import { type MigrateOptions, migrate, MIGRATIONS } from './db/migrations.js'

// The database is optional. With SCADBUDDY_DATABASE_URL unset the service still
// starts and answers /healthz, but reports AI as disabled (spec §9, "No
// database"). The `ai_*` tables are created by db/migrations.ts, applied by
// `ready()` rather than before listening, so a database that is down at
// start-up is reported by /healthz instead of crashing the pod.

export type Database = {
  /** The pool, for the stores that own `ai_*` tables. Query only after `ready()` resolved true. */
  sql: Sql
  /** Resolves true when `select 1` answers within the timeout. Never throws. */
  ping(timeoutMs?: number): Promise<boolean>
  /**
   * Applies pending migrations once; resolves true when the schema is current.
   * Single-flight and memoised on success; a failure is retried by the next
   * call. Never throws.
   */
  ready(): Promise<boolean>
  /**
   * Connections of their own for session advisory locks held across slow work (a plugin's
   * connection test, #4994), so a held lock never takes one of `sql`'s connections. `close` ends them.
   */
  locks: LockClient
  close(): Promise<void>
}

/** The lock client's connections: also how many plugin tests one replica runs at once. */
export const TEST_LOCK_CONNECTIONS = 4

/** One reserved lock connection. `done(true)` returns it; `done(false)`: it may still hold a lock, so it is never reused. */
export type LockLease = { sql: ReservedSql; done(healthy: boolean): void }

export type LockClient = {
  /** A reserved connection, or undefined when TEST_LOCK_CONNECTIONS are out: it never waits for one. */
  take(): Promise<LockLease | undefined>
  end(): Promise<void>
}

/**
 * The lock client over `make`, its postgres.js client made on the first `take`. The cap is
 * counted here, so every user of one client shares it. A lease done unhealthy retires its
 * client: later leases come from a new one, and the old one ends, closing the connection and
 * any lock it still holds, once its last lease is done.
 */
export function createLockClient(make: (max: number) => Sql): LockClient {
  type Generation = { sql: Sql; out: number; retired: boolean }
  let held = 0
  let current: Generation | undefined
  const all = new Set<Generation>()
  const settle = (gen: Generation): void => {
    gen.out--
    if (gen.retired && gen.out === 0) {
      all.delete(gen)
      // No lease is out: nothing to wait for, and the retired connection must not linger.
      void gen.sql.end({ timeout: 0 }).catch(() => undefined)
    }
  }
  return {
    async take() {
      if (held >= TEST_LOCK_CONNECTIONS) return undefined
      held++
      // `held` caps the leases at TEST_LOCK_CONNECTIONS. While a retired client still has leases out, its
      // pool and this new one both exist, so the replica's lock connections may briefly pass that number.
      const gen = current ?? { sql: make(TEST_LOCK_CONNECTIONS), out: 0, retired: false }
      current = gen
      all.add(gen)
      gen.out++
      let conn: ReservedSql
      try {
        conn = await gen.sql.reserve()
      } catch (err) {
        held--
        settle(gen)
        throw err
      }
      let settled = false
      return {
        sql: conn,
        done(healthy) {
          if (settled) return
          settled = true
          held--
          if (healthy) conn.release()
          else {
            gen.retired = true
            if (current === gen) current = undefined
          }
          settle(gen)
        },
      }
    },
    async end() {
      await Promise.all([...all].map((gen) => gen.sql.end({ timeout: 5 })))
      all.clear()
      current = undefined
    },
  }
}

/** The slice of a postgres.js PendingQuery the ping needs (types/index.d.ts: `cancel(): void`). */
export type CancellableQuery = PromiseLike<unknown> & { cancel(): void }

/**
 * A health ping that cannot pile up on the pool during an outage:
 *
 * - single-flight: while one ping is outstanding, concurrent callers share it,
 *   so however often /healthz is polled, health holds at most one connection;
 * - on timeout the query is CANCELLED (postgres.js sends a cancel request to
 *   the server for a running query, or drops it from the queue if it never
 *   got a connection), so a timed-out ping releases its connection instead
 *   of holding it until the server eventually answers.
 */
export function makePing(runQuery: () => CancellableQuery): (timeoutMs?: number) => Promise<boolean> {
  let inflight: Promise<boolean> | undefined

  const once = async (timeoutMs: number): Promise<boolean> => {
    const query = runQuery()
    let timer: NodeJS.Timeout | undefined
    const timeout = new Promise<false>((resolve) => {
      timer = setTimeout(() => {
        query.cancel()
        resolve(false)
      }, timeoutMs)
    })
    try {
      return await Promise.race([Promise.resolve(query).then(() => true), timeout])
    } catch {
      return false
    } finally {
      clearTimeout(timer)
    }
  }

  return (timeoutMs = 2000) => {
    inflight ??= once(timeoutMs).finally(() => {
      inflight = undefined
    })
    return inflight
  }
}

export type ConnectOptions = {
  /** Postgres `search_path`; tests give each run a schema of its own. */
  searchPath?: string
  /** Where to report a failed migration (the error, never the URL). */
  onMigrationError?: (err: unknown) => void
  /** lock_timeout / statement_timeout for migrations (db/migrations.ts). */
  migrate?: MigrateOptions
  /**
   * Runs after migrations succeed, as part of the same `ready()` attempt: key
   * rotation (main.ts). A throw fails the attempt and the next `ready()` retries.
   */
  afterMigrate?: (sql: Sql) => Promise<void>
}

/** Memoises the first success of `attempt`; concurrent callers share one attempt. */
export function makeReady(attempt: () => Promise<unknown>, onError?: (err: unknown) => void): () => Promise<boolean> {
  let done = false
  let inflight: Promise<boolean> | undefined
  return () => {
    if (done) return Promise.resolve(true)
    inflight ??= attempt()
      .then(() => (done = true))
      .catch((err: unknown) => {
        onError?.(err)
        return false
      })
      .finally(() => {
        inflight = undefined
      })
    return inflight
  }
}

export function connectDatabase(url: string, options: ConnectOptions = {}): Database {
  const client = (max: number): Sql =>
    postgres(url, {
      max,
      // Lazily connects on first query, so a database that is down at start-up
      // shows as unreachable in /healthz instead of crashing the process.
      connect_timeout: 5,
      onnotice: () => {},
      ...(options.searchPath === undefined ? {} : { connection: { search_path: options.searchPath } }),
    })
  // Small on purpose: health pings hold at most one connection (makePing),
  // and the credential and settings stores issue short single queries.
  const sql = client(4)
  const locks = createLockClient(client)
  const ping = makePing(() => {
    const query = sql`select 1`
    // A cancelled query rejects; the race above has already settled by then,
    // so swallow it here rather than leave an unhandled rejection.
    query.catch(() => {})
    return query
  })
  return {
    sql,
    ping,
    ready: makeReady(async () => {
      await migrate(sql, MIGRATIONS, options.migrate)
      await options.afterMigrate?.(sql)
    }, options.onMigrationError),
    locks,
    async close() {
      await Promise.all([sql.end({ timeout: 5 }), locks.end()])
    },
  }
}
