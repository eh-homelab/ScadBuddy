import type { Hono } from 'hono'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { readRuntimeSettings, type RuntimeSettings, RuntimePutBody } from '../sessions/runtimeSettings.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './module.js'
import type { SettingsWriter } from './mcpAuthMode.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/session-runtime (unified runtime spec 2026-10-13 §13,
// plan Task 2.5): how long a parked call is held, the warm process cache's size
// and idle time, and the most a turn may run. Stored in `ai_settings` under the
// four names in sessions/runtimeSettings.ts.
//
//   GET /api/v1/ai/settings/session-runtime   RuntimeSettings  (the defaults when unset)
//   PUT /api/v1/ai/settings/session-runtime   a partial RuntimeSettings; 422 { detail } out of range
//
// Guarded and audited like PUT /api/v1/ai/settings/workspace: the keys are
// written in one transaction (credentials.ts SettingsStore.setMany), which sends
// `NOTIFY scadbuddy_settings` so every replica resizes its warm cache.

export type SessionRuntimeRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Undefined when there is no database. */
  writer: SettingsWriter | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export const SESSION_RUNTIME_PATH = '/api/v1/ai/settings/session-runtime'

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'assistant session runtime settings'

export function registerSessionRuntimeRoutes(app: Hono, deps: SessionRuntimeRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }
  async function writer(): Promise<SettingsWriter | string> {
    if (!deps.writer) return NO_DATABASE
    return (await deps.ready()) ? deps.writer : NOT_READY
  }

  app.get(SESSION_RUNTIME_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    return c.json(await readRuntimeSettings(settings))
  })

  app.put(SESSION_RUNTIME_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const store = await writer()
    if (typeof store === 'string') return c.json({ detail: store }, 503)
    let json: unknown
    try {
      json = await c.req.json()
    } catch {
      return c.json({ detail: 'body is not valid JSON' }, 400)
    }
    const parsed = RuntimePutBody.safeParse(json)
    if (!parsed.success) {
      const detail = parsed.error.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
      return c.json({ detail }, 422)
    }
    const values: Partial<RuntimeSettings> = parsed.data
    if (Object.keys(values).length > 0) {
      const context = { actor: UI_ACTOR, surface: 'http' as const, clientIp: deps.remoteAddress(c) }
      await store.setMany(values, undefined, context)
    }
    return c.json(await readRuntimeSettings(settings))
  })
}

/** The session runtime settings. `deps.settings` is declared in routes/module.ts, `deps.aiSettings` in routes/mcpAuthMode.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerSessionRuntimeRoutes(app, {
      settings: deps.settings,
      writer: deps.database ? deps.aiSettings : undefined,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
