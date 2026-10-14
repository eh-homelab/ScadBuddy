import postgres, { type Sql } from 'postgres'
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
  close(): Promise<void>
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
  const sql = postgres(url, {
    // Small on purpose: health pings hold at most one connection (makePing),
    // and the credential and settings stores issue short single queries.
    max: 4,
    // Lazily connects on first query, so a database that is down at start-up
    // shows as unreachable in /healthz instead of crashing the process.
    connect_timeout: 5,
    onnotice: () => {},
    ...(options.searchPath === undefined ? {} : { connection: { search_path: options.searchPath } }),
  })
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
    async close() {
      await sql.end({ timeout: 5 })
    },
  }
}
