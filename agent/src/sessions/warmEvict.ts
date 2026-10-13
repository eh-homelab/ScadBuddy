import type { Sql, TransactionSql } from 'postgres'
import { isUuid } from '../harness/stateDirs.js'
import type { WarmSessions } from './warm.js'

// Eviction notifications for the warm process cache (unified runtime spec
// 2026-10-13 §5.3 "Explicit" eviction, plan Task 2.3). A warm Claude Code holds
// its credential, plugin routes and packages for its life, on one replica; a
// change to any of them must reach every replica's cache, and a session that
// changed hands or ended must lose its process wherever it is warm. The payload
// is what to evict: a session id, or `*` for every entry. Sent inside the
// writer's transaction where there is one, so it goes out on commit and never
// for a write that rolled back. A NOTIFY sent while a replica's listener is down
// is lost (events/pgListener.ts): the fingerprint and the transcript tail rules
// still keep such a process from serving a turn it no longer matches; what is
// lost is only its eviction, until it goes idle.

export const WARM_EVICT_CHANNEL = 'scadbuddy_warm_evict'

/** Tells every replica to evict `target`'s warm process (`*`: every one). */
export async function notifyWarmEvict(sql: Sql | TransactionSql, target: string | '*'): Promise<void> {
  await sql`SELECT pg_notify(${WARM_EVICT_CHANNEL}, ${target})`
}

/** The LISTEN consumer (main.ts): a payload that is not a session id or `*` is ignored. */
export function evictOnNotify(warm: Pick<WarmSessions, 'evict'>): (payload: string) => void {
  return (payload) => {
    if (payload === '*' || isUuid(payload)) warm.evict(payload, 'notified')
  }
}
