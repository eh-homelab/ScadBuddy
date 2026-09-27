import postgres from 'postgres'

// The database is optional. With SCADBUDDY_DATABASE_URL unset the service still
// starts and answers /healthz, but reports AI as disabled (spec §9, "No
// database"). Tables and migrations arrive with the stories that own them
// (#255, #300); this module only opens the pool and can ping it.

export type Database = {
  /** Resolves true when `select 1` answers within the timeout. Never throws. */
  ping(timeoutMs?: number): Promise<boolean>
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

export function connectDatabase(url: string): Database {
  const sql = postgres(url, {
    // Small on purpose: the scaffold issues nothing but health pings, and
    // makePing keeps those to one connection at a time.
    max: 4,
    // Lazily connects on first query, so a database that is down at start-up
    // shows as unreachable in /healthz instead of crashing the process.
    connect_timeout: 5,
    onnotice: () => {},
  })
  const ping = makePing(() => {
    const query = sql`select 1`
    // A cancelled query rejects; the race above has already settled by then,
    // so swallow it here rather than leave an unhandled rejection.
    query.catch(() => {})
    return query
  })
  return {
    ping,
    async close() {
      await sql.end({ timeout: 5 })
    },
  }
}
