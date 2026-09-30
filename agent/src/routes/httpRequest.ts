import type { Hono } from 'hono'
import { z } from 'zod'
import { httpRequestEnabled, SETTING_HTTP_REQUEST } from '../harness/httpRequest.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'

// /api/v1/ai/settings/http-request (#827): turns the assistant's `http_request`
// tool on or off for session turns. Stored in `ai_settings` under
// `http_request_enabled`, read at the start of every turn by
// sessions/manager.ts. ON by default (#827): anything but a stored `false` is
// on. The same shape and guards as the headless-browser switch
// (routes/headlessBrowser.ts):
//
//   GET /api/v1/ai/settings/http-request   { "enabled": boolean }
//   PUT /api/v1/ai/settings/http-request   body { "enabled": boolean }
//
// The PUT is a settings write (outward, spec §8.1), so it passes guard.ts
// `uiRequestProblem`: from the UI's origin, through the HTTPS ingress, JSON
// only. The store audits the write (credentials.ts SettingsStore.set).

export type HttpRequestRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type HttpRequestSettingView = { enabled: boolean }

const PutBody = z.strictObject({ enabled: z.boolean() })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'HTTP request tool settings'

export const HTTP_REQUEST_SETTING_PATH = '/api/v1/ai/settings/http-request'

export function registerHttpRequestRoutes(app: Hono, deps: HttpRequestRouteDeps): void {
  const base = HTTP_REQUEST_SETTING_PATH

  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const view: HttpRequestSettingView = { enabled: httpRequestEnabled(await settings.get<unknown>(SETTING_HTTP_REQUEST)) }
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
    await settings.set(SETTING_HTTP_REQUEST, body.enabled)
    const view: HttpRequestSettingView = { enabled: body.enabled }
    return c.json(view)
  })
}
