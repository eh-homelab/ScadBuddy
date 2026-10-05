import type { Hono } from 'hono'
import { z } from 'zod'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { isSessionMode, SESSION_MODES, type SessionMode, SETTING_SESSION_MODE } from '../sessions/manager.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/session-mode (#1056): the mode a new assistant session gets
// when its start chooses none (sessions/manager.ts `defaultMode`, plan ruling 2).
// Stored in `ai_settings` under `session_mode`; `classic` when unset or invalid. A
// change applies to new sessions; a session's mode is fixed when it starts.
//
//   GET /api/v1/ai/settings/session-mode   { mode }  ('classic' when unset)
//   PUT /api/v1/ai/settings/session-mode   body { mode: 'classic' | 'durable' } → { mode }
//
// Guarded and audited like the session limits (routes/sessionLimits.ts): the write
// passes guard.ts `uiRequestProblem` and is a `settings` audit row as the browser
// user, with the client address; the read passes `uiReadProblem`. Error bodies use
// `{ detail }`.

export type SessionModeRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type SessionModeView = { mode: SessionMode }

export const SESSION_MODE_PATH = '/api/v1/ai/settings/session-mode'

const PutBody = z.strictObject({ mode: z.enum(SESSION_MODES) })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'the assistant session mode'

export function registerSessionModeRoutes(app: Hono, deps: SessionModeRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(SESSION_MODE_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const stored = await settings.get<unknown>(SETTING_SESSION_MODE)
    const view: SessionModeView = { mode: isSessionMode(stored) ? stored : 'classic' }
    return c.json(view)
  })

  app.put(SESSION_MODE_PATH, async (c) => {
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
    await settings.set(SETTING_SESSION_MODE, body.mode, context)
    const view: SessionModeView = { mode: body.mode }
    return c.json(view)
  })
}

/** The default session mode in Settings (#1056). */
export const route: RouteModule = {
  register(app, deps) {
    registerSessionModeRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
