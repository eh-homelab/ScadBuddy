import postgres, { type ReservedSql, type Sql } from 'postgres'
import { grantBackendRoles } from './db/grants.js'
import { type AwaitMigrationsOptions, awaitMigrations } from './db/schema.js'

// The database is optional. With SCADBUDDY_DATABASE_URL unset the service still
// starts and answers /healthz, but reports AI as disabled (spec §9, "No
// database"). The `ai_*` tables are created by the API, which applies the agent's
// migrations at its start (#5299 D6); `ready()` waits for them (db/schema.ts) rather
// than before listening, so a database that is down at start-up is reported by
// /healthz instead of crashing the pod.

export type Database = {
  /** The pool, for the stores that own `ai_*` tables. Query only after `ready()` resolved true. */
  sql: Sql
  /** Resolves true when `select 1` answers within the timeout. Never throws. */
  ping(timeoutMs?: number): Promise<boolean>
  /**
   * Waits, bounded, until the API has applied the migrations this build expects
   * (db/schema.ts `awaitMigrations`); resolves true when the schema is current.
   * Single-flight and memoised on success; a failure is retried by the next
   * call. Never throws.
   */
  ready(): Promise<boolean>
  /**
   * Resolves at the first `ready()` that is true, calling it again with back-off until then
   * (`makeUntilReady`). For work a process does once at start: the API may take longer than one
   * `ready()` to apply the agent's migrations (#5299), and such work must not be dropped on that
   * first false. Never rejects.
   */
  untilReady(): Promise<void>
  /** Whether the last read of `ai_migrations` found files this build expects missing: the API has not migrated yet (#5299). */
  waitingForMigrations(): boolean
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
  /** `IDLE_TIMEOUT_SECONDS` unless set; tests shorten it. */
  idleTimeoutSeconds?: number
  /** Where to report a failed `ready()` attempt (the error, never the URL). */
  onMigrationError?: (err: unknown) => void
  /** How long `ready()` waits for the API's migrations (db/schema.ts). */
  awaitMigrations?: AwaitMigrationsOptions
  /**
   * Brings the schema in place of waiting for the API's migrations. Only the tests pass
   * it, to migrate a schema of their own (test/support/migrations.ts): the agent never
   * migrates (#5299 D6).
   */
  schema?: (sql: Sql) => Promise<void>
  /**
   * Runs once the schema is current, as part of the same `ready()` attempt: key
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

/** `ready()` again, `firstDelayMs` after a false and doubling to `maxDelayMs`, until it is true; one wait shared by every caller. */
export function makeUntilReady(
  ready: () => Promise<boolean>,
  { firstDelayMs = 1_000, maxDelayMs = 30_000 }: { firstDelayMs?: number; maxDelayMs?: number } = {},
): () => Promise<void> {
  let waiting: Promise<void> | undefined
  return () =>
    (waiting ??= (async () => {
      let delay = firstDelayMs
      while (!(await ready())) {
        // Unref'd: a process stopping does not wait for its database to come up.
        await new Promise((resolve) => setTimeout(resolve, delay).unref())
        delay = Math.min(delay * 2, maxDelayMs)
      }
    })())
}

/** The stores' pool. Small on purpose: health pings hold at most one connection (makePing), and the credential and settings stores issue short single queries. */
export const SQL_CONNECTIONS = 4

/**
 * Seconds a connection may sit unused before postgres.js closes it (#5226). Its default, 0,
 * kept every connection open for good: each agent replica held its pool's 4 idle all day.
 */
export const IDLE_TIMEOUT_SECONDS = 60

/** How the stores' and the lock client's connections show in `pg_stat_activity`. */
export const APPLICATION_NAME = 'scadbuddy-agent'

/**
 * The most Postgres connections one agent process holds: the stores' pool, the lock client
 * and the LISTEN connection (events/pgListener.ts, always one); briefly more while a lock
 * client retired by an unhealthy lease still has leases out (`createLockClient`). The backend's
 * `tests/test_pg_pools.py` sums the API process's the same way; README "Running it" gives both.
 */
export const AGENT_DB_CONNECTIONS = SQL_CONNECTIONS + TEST_LOCK_CONNECTIONS + 1

/** The postgres.js options of one of `connectDatabase`'s clients. */
export function clientOptions(max: number, searchPath?: string, idleTimeout = IDLE_TIMEOUT_SECONDS): postgres.Options<Record<string, never>> {
  return {
    max,
    idle_timeout: idleTimeout,
    // Lazily connects on first query, so a database that is down at start-up
    // shows as unreachable in /healthz instead of crashing the process.
    connect_timeout: 5,
    onnotice: () => {},
    connection: { application_name: APPLICATION_NAME, ...(searchPath === undefined ? {} : { search_path: searchPath }) },
  }
}

export function connectDatabase(url: string, options: ConnectOptions = {}): Database {
  const client = (max: number): Sql => postgres(url, clientOptions(max, options.searchPath, options.idleTimeoutSeconds))
  const sql = client(SQL_CONNECTIONS)
  const locks = createLockClient(client)
  const ping = makePing(() => {
    const query = sql`select 1`
    // A cancelled query rejects; the race above has already settled by then,
    // so swallow it here rather than leave an unhandled rejection.
    query.catch(() => {})
    return query
  })
  let waiting = false
  const ready = makeReady(async () => {
    await (options.schema
      ? options.schema(sql)
      : awaitMigrations(sql, undefined, { ...options.awaitMigrations, onWaiting: (now) => (waiting = now) }))
    // #5210: the backend's roles their `ai_*` tables. Never fatal: a role left ungranted
    // waits or fails its own calls, and says which.
    await grantBackendRoles(sql).catch((error: unknown) => {
      console.error(`could not grant the backend's roles the ai_* tables: ${error instanceof Error ? error.message : String(error)}`)
    })
    await options.afterMigrate?.(sql)
  }, options.onMigrationError)
  return {
    sql,
    ping,
    ready,
    untilReady: makeUntilReady(ready),
    waitingForMigrations: () => waiting,
    locks,
    async close() {
      await Promise.all([sql.end({ timeout: 5 }), locks.end()])
    },
  }
}
