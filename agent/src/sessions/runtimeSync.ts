import type { SettingsRepo } from '../routes/module.js'
import { readRuntimeSettings, resizeOnNotify, RUNTIME_DEFAULTS, type RuntimeSettings, runtimeResizer, SETTINGS_CHANNEL } from './runtimeSettings.js'
import type { WarmSessions } from './warm.js'
import { evictOnNotify, WARM_EVICT_CHANNEL } from './warmEvict.js'

// How the warm cache follows the runtime settings and the evictions (main.ts):
// at start, on NOTIFY, and, since a NOTIFY sent while a replica's listener is
// down is lost (events/pgListener.ts), when the listener is back and every
// RESYNC_MS. A running turn is never touched by any of them.

/** How often the settings are re-read and each idle process is checked against a fresh assembly. */
export const RESYNC_MS = 60_000

/** The settings the cache starts with: the defaults, said so in `log`, when the database is not ready or the read fails; the resync picks the stored values up later. */
export async function readStartSettings(
  deps: { database: { ready(): Promise<boolean> } | undefined; settings: Pick<SettingsRepo, 'get'> | undefined },
  log: (message: string) => void,
): Promise<RuntimeSettings> {
  if (!deps.database || !deps.settings) return RUNTIME_DEFAULTS
  try {
    if (await deps.database.ready()) return await readRuntimeSettings(deps.settings)
    log('warm: the database is not ready; starting with the default runtime settings until the next resync')
  } catch (err) {
    log(`warm: reading the runtime settings failed (${err instanceof Error ? err.message : String(err)}); starting with the defaults until the next resync`)
  }
  return RUNTIME_DEFAULTS
}

export type RuntimeSyncDeps = {
  settings: Pick<SettingsRepo, 'get'>
  warm: Pick<WarmSessions, 'resize' | 'evict' | 'evictIdle'>
  /** Each idle process checked against a fresh assembly (`SessionManager.revalidateWarm`). */
  revalidate: () => Promise<void>
  events: {
    listenAlso(channel: string, onNotify: (payload: string) => void): void
    onReconnect(fn: () => void): void
  }
  log: (message: string) => void
  resyncMs?: number
}

/** Wires the notifications, the reconnect and the resync timer; the returned function stops the timer. */
export function wireRuntimeSync(deps: RuntimeSyncDeps): () => void {
  const resize = runtimeResizer(deps.settings, deps.warm, deps.log)
  deps.events.listenAlso(WARM_EVICT_CHANNEL, evictOnNotify(deps.warm))
  deps.events.listenAlso(SETTINGS_CHANNEL, resizeOnNotify(resize))
  // Anything sent while the listener was down is gone: re-read, and let every idle process go.
  deps.events.onReconnect(() => {
    void resize()
    void deps.warm.evictIdle('the listener reconnected')
  })
  let running = false
  const timer = setInterval(() => {
    if (running) return
    running = true
    void resize()
      .then(() => deps.revalidate())
      .catch((err: unknown) => deps.log(`warm: the resync failed: ${err instanceof Error ? err.message : String(err)}`))
      .finally(() => {
        running = false
      })
  }, deps.resyncMs ?? RESYNC_MS)
  timer.unref()
  return () => clearInterval(timer)
}
