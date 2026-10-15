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

/**
 * Whether a write to this Settings key changes what a warm process was spawned
 * with (sessions/turnRunner.ts `FingerprintParts`), so every cache is told
 * (`credentials.ts` SettingsStore): the model, the http_request switch, the
 * recall cap, ScadBuddy's own plugin, and each built-in tool set's raised tiers
 * and disabled tools (`builtin_tools.<set>`, plugins/builtInTools.ts). Named
 * here by their strings, as credentials.ts is below the modules that export
 * them; test/warmEvict.pg.test.ts holds the two together.
 */
export function evictsWarmOnWrite(key: string): boolean {
  return (
    key === 'model' ||
    key === 'http_request_enabled' ||
    key === 'memory_recall_max_query_chars' ||
    key === 'scadbuddy_plugin_enabled' ||
    key.startsWith('builtin_tools.')
  )
}

/** Tells every replica to evict `target`'s warm process (`*`: every one). */
export async function notifyWarmEvict(sql: Sql | TransactionSql, target: string | '*'): Promise<void> {
  await sql`SELECT pg_notify(${WARM_EVICT_CHANNEL}, ${target})`
}

/** The LISTEN consumer (main.ts): a payload that is not a session id or `*` is ignored. */
export function evictOnNotify(warm: Pick<WarmSessions, 'evict'>): (payload: string) => void {
  return (payload) => {
    if (payload === '*' || isUuid(payload)) void warm.evict(payload, 'notified')
  }
}
