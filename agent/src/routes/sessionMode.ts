import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { SESSION_MODES, type SessionMode, sessionModeOf, SETTING_SESSION_MODE } from '../sessions/manager.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/session-mode (spec 2026-10-01 §6.1, plan 5d): the mode a new
// assistant session gets when whoever starts it names none. Stored in `ai_settings`
// under `session_mode`, which SessionManager.start reads; unset means durable (the
// owner's default, plan 5d Ruling 1). A session that got durable from here, on an
// agent service that cannot run one now, runs classic and says why (Ruling 2).
//
//   GET /api/v1/ai/settings/session-mode   { mode, durable_available, durable_unavailable_reason? }
//   PUT /api/v1/ai/settings/session-mode   body { mode: 'classic' | 'durable' }
//
// `durable_available` is whether a durable session could start now (Temporal, the
// key, and an agent-durable worker polling, Ruling 2b), so Settings can say the
// default will fall back. The PUT stores durable either way.
//
// Guarded and audited like the other AI settings routes: the write passes
// guard.ts `uiRequestProblem` and is a `settings` audit row as the browser user,
// with the client address (credentials.ts SettingsStore.set). The read passes
// `uiReadProblem`. Error bodies use `{ detail }`.

export const SESSION_MODE_PATH = '/api/v1/ai/settings/session-mode'

export type SessionModeRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  /** Why a durable session could not start now, or undefined (SessionManager.durableUnready). */
  durableUnready: () => Promise<string | undefined>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type SessionModeView = { mode: SessionMode; durable_available: boolean; durable_unavailable_reason?: string }

const PutBody = z.strictObject({ mode: z.enum(SESSION_MODES) })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'the default assistant session mode'

export function registerSessionModeRoutes(app: Hono, deps: SessionModeRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  async function view(mode: SessionMode): Promise<SessionModeView> {
    const why = await deps.durableUnready()
    return why === undefined
      ? { mode, durable_available: true }
      : { mode, durable_available: false, durable_unavailable_reason: why }
  }

  app.get(SESSION_MODE_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    return c.json(await view(sessionModeOf(await settings.get<unknown>(SETTING_SESSION_MODE))))
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
    await settings.set(SETTING_SESSION_MODE, body.mode, {
      actor: UI_ACTOR,
      surface: 'http',
      clientIp: deps.remoteAddress(c),
    })
    return c.json(await view(body.mode))
  })
}

const NO_SESSIONS = 'durable sessions need the database and Temporal; this agent service has neither'

/** Settings → Assistant's default session mode. `deps.settings` is declared in routes/headlessBrowser.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerSessionModeRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      durableUnready: () => (deps.sessions ? deps.sessions.durableUnready() : Promise.resolve(NO_SESSIONS)),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
