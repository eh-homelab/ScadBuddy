import { z } from 'zod'
import type { Sql, TransactionSql } from 'postgres'
import type { SettingsRepo } from '../routes/httpRequest.js'
import type { WarmSessions } from './warm.js'

// The session-runtime settings (unified runtime spec 2026-10-13 §13, plan Task
// 2.5): stored in `ai_settings` under their own names. `warm_*` size the warm
// process cache (warm.ts `resize`), applied at start and on each change on any
// replica; `park_hold_seconds` and `turn_max_seconds` are only stored and served
// here, and later phases read them through `readRuntimeSettings`. None of them
// is a fingerprint input (warmEvict.ts), so a write never evicts a process.

export type RuntimeSettings = {
  park_hold_seconds: number
  warm_max_per_replica: number
  warm_idle_seconds: number
  turn_max_seconds: number
}

export const RUNTIME_DEFAULTS: RuntimeSettings = {
  park_hold_seconds: 300,
  warm_max_per_replica: 8,
  warm_idle_seconds: 600,
  turn_max_seconds: 7200,
}

/** Inclusive, spec §13. `warm_max_per_replica` 0 means no warm processes: every turn is cold. */
export const RUNTIME_BOUNDS: { [K in keyof RuntimeSettings]: { min: number; max: number } } = {
  park_hold_seconds: { min: 30, max: 3600 },
  warm_max_per_replica: { min: 0, max: 32 },
  warm_idle_seconds: { min: 60, max: 86400 },
  turn_max_seconds: { min: 300, max: 86400 },
}

export const RUNTIME_KEYS = Object.keys(RUNTIME_DEFAULTS) as (keyof RuntimeSettings)[]

const field = (key: keyof RuntimeSettings) => z.number().int().min(RUNTIME_BOUNDS[key].min).max(RUNTIME_BOUNDS[key].max)

/** A partial body: only the keys present are written. */
export const RuntimePutBody = z.strictObject({
  park_hold_seconds: field('park_hold_seconds').optional(),
  warm_max_per_replica: field('warm_max_per_replica').optional(),
  warm_idle_seconds: field('warm_idle_seconds').optional(),
  turn_max_seconds: field('turn_max_seconds').optional(),
})

export const isRuntimeKey = (key: string): key is keyof RuntimeSettings => (RUNTIME_KEYS as string[]).includes(key)

/** Each setting as stored; the default for a missing one or one out of range (written by hand). */
export async function readRuntimeSettings(settings: Pick<SettingsRepo, 'get'>): Promise<RuntimeSettings> {
  const values = await Promise.all(RUNTIME_KEYS.map((key) => settings.get<unknown>(key)))
  const out = { ...RUNTIME_DEFAULTS }
  RUNTIME_KEYS.forEach((key, i) => {
    const parsed = field(key).safeParse(values[i])
    if (parsed.success) out[key] = parsed.data
  })
  return out
}

export const SETTINGS_CHANNEL = 'scadbuddy_settings'
export const RUNTIME_PAYLOAD = 'session-runtime'

/** Tells every replica to re-read the runtime settings; inside the writer's transaction, so it goes out on commit. */
export async function notifyRuntimeSettings(sql: Sql | TransactionSql): Promise<void> {
  await sql`SELECT pg_notify(${SETTINGS_CHANNEL}, ${RUNTIME_PAYLOAD})`
}

/** The LISTEN consumer (main.ts): re-reads on `session-runtime` and ignores any other payload. */
export function resizeOnNotify(
  settings: Pick<SettingsRepo, 'get'>,
  warm: Pick<WarmSessions, 'resize'>,
  log: (message: string) => void,
): (payload: string) => void {
  return (payload) => {
    if (payload !== RUNTIME_PAYLOAD) return
    void readRuntimeSettings(settings).then(
      (s) => warm.resize(s.warm_max_per_replica, s.warm_idle_seconds * 1000),
      (err: unknown) => log(`warm: re-reading the runtime settings failed: ${err instanceof Error ? err.message : String(err)}`),
    )
  }
}
