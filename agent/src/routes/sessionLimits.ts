import type { Hono } from 'hono'
import { z } from 'zod'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import { DEFAULT_MAX_BUDGET_USD, DEFAULT_MAX_TURNS } from '../harness/run.js'
import type { OriginPolicy } from '../http/origins.js'
import {
  cents,
  MAX_SESSION_BUDGET_USD,
  MAX_SESSION_MAX_TURNS,
  SETTING_SESSION_BUDGET_USD,
  SETTING_SESSION_MAX_TURNS,
} from '../sessions/manager.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'

// /api/v1/ai/settings/session-limits (#790): what a new assistant session may
// spend in all, and how many turns one reply may take. Stored in `ai_settings`
// under `session_max_budget_usd` and `session_max_turns`, which the session
// manager reads when a session is created (sessions/manager.ts `limits()`), so
// a change applies to new sessions; existing ones keep theirs (raise one with
// POST /api/v1/ai/sessions/:id/budget).
//
//   GET /api/v1/ai/settings/session-limits   { budget_usd, max_turns }  (the defaults when unset)
//   PUT /api/v1/ai/settings/session-limits   body { budget_usd: 0.01..100, max_turns: 1..200 }
//
// Guarded and audited like PUT /api/v1/ai/audit/settings: the write passes
// guard.ts `uiRequestProblem` and each key is a `settings` audit row as the
// browser user, with the client address (credentials.ts SettingsStore.set).
// The read passes `uiReadProblem`. Error bodies use `{ detail }`.

export type SessionLimitsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type SessionLimitsView = { budget_usd: number; max_turns: number }

export const SESSION_LIMITS_PATH = '/api/v1/ai/settings/session-limits'

const PutBody = z.strictObject({
  budget_usd: z.number().min(0.01).max(MAX_SESSION_BUDGET_USD),
  max_turns: z.number().int().min(1).max(MAX_SESSION_MAX_TURNS),
})

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'assistant session limits'

/** What limits() would give a new session: the stored value when it is a positive number. */
function stored(value: unknown, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback
}

export function registerSessionLimitsRoutes(app: Hono, deps: SessionLimitsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(SESSION_LIMITS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const [budget, turns] = await Promise.all([
      settings.get<unknown>(SETTING_SESSION_BUDGET_USD),
      settings.get<unknown>(SETTING_SESSION_MAX_TURNS),
    ])
    const view: SessionLimitsView = {
      budget_usd: stored(budget, DEFAULT_MAX_BUDGET_USD),
      max_turns: Math.floor(stored(turns, DEFAULT_MAX_TURNS)),
    }
    return c.json(view)
  })

  app.put(SESSION_LIMITS_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    let body: z.infer<typeof PutBody>
    try {
      body = PutBody.parse(await c.req.json())
    } catch (err) {
      const detail =
        err instanceof z.ZodError
          ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
          : 'body is not valid JSON'
      return c.json({ detail }, 400)
    }
    const context: AuditContext = { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) }
    const view: SessionLimitsView = { budget_usd: cents(body.budget_usd), max_turns: body.max_turns }
    await settings.set(SETTING_SESSION_BUDGET_USD, view.budget_usd, context)
    await settings.set(SETTING_SESSION_MAX_TURNS, view.max_turns, context)
    return c.json(view)
  })
}
