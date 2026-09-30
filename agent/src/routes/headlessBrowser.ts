import type { Hono } from 'hono'
import { z } from 'zod'
import type { AuditContext } from '../audit/log.js'
import { SETTING_HEADLESS_BROWSER } from '../harness/headlessBrowser.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/headless-browser (#349): turns the headless browser on or
// off for session turns (spec §5.3, "Off unless enabled"). Stored in
// `ai_settings` under `headless_browser_enabled` (D4: AI settings live in the
// database), read at the start of every turn by sessions/manager.ts.
//
//   GET /api/v1/ai/settings/headless-browser   { "enabled": boolean }
//   PUT /api/v1/ai/settings/headless-browser   body { "enabled": boolean }
//
// A settings write is outward (spec §8.1), so the PUT passes guard.ts
// `uiRequestProblem`, as the credential and plugin writes do: from the UI's
// origin, through the HTTPS ingress, JSON only. A request that passes is the
// browser user, who approves outward actions in the UI (§8.2). The GET passes
// `uiReadProblem`. Error bodies use `{ detail }`, the backend's FastAPI shape.

export type SettingsRepo = {
  get<T>(key: string): Promise<T | undefined>
  /** `context` says who wrote it, for the audit row (credentials.ts SettingsStore.set). */
  set(key: string, value: unknown, context?: AuditContext): Promise<void>
}

export type HeadlessBrowserRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type HeadlessBrowserSettingView = { enabled: boolean }

const PutBody = z.strictObject({ enabled: z.boolean() })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'headless browser settings'

export const HEADLESS_BROWSER_SETTING_PATH = '/api/v1/ai/settings/headless-browser'

export function registerHeadlessBrowserRoutes(app: Hono, deps: HeadlessBrowserRouteDeps): void {
  const base = HEADLESS_BROWSER_SETTING_PATH

  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    // Anything but a stored `true` is off.
    const view: HeadlessBrowserSettingView = { enabled: (await settings.get<unknown>(SETTING_HEADLESS_BROWSER)) === true }
    return c.json(view)
  })

  app.put(base, async (c) => {
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
    await settings.set(SETTING_HEADLESS_BROWSER, body.enabled)
    const view: HeadlessBrowserSettingView = { enabled: body.enabled }
    return c.json(view)
  })
}

declare module '../app.js' {
  interface AppDeps {
    /** `ai_settings` (credentials.ts SettingsStore); the headless-browser setting (#349) answers 503 without it. */
    settings?: SettingsRepo | undefined
  }
}

/** The headless-browser setting (#349). */
export const route: RouteModule = {
  register(app, deps) {
    registerHeadlessBrowserRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
