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

export function connectDatabase(url: string): Database {
  const sql = postgres(url, {
    // Small on purpose: the scaffold issues nothing but health pings.
    max: 4,
    // Lazily connects on first query, so a database that is down at start-up
    // shows as unreachable in /healthz instead of crashing the process.
    connect_timeout: 5,
    onnotice: () => {},
  })
  return {
    async ping(timeoutMs = 2000) {
      let timer: NodeJS.Timeout | undefined
      const timeout = new Promise<false>((resolve) => {
        timer = setTimeout(() => resolve(false), timeoutMs)
      })
      try {
        return await Promise.race([sql`select 1`.then(() => true), timeout])
      } catch {
        return false
      } finally {
        clearTimeout(timer)
      }
    },
    async close() {
      await sql.end({ timeout: 5 })
    },
  }
}
