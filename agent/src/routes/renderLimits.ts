import type { Hono } from 'hono'
import { z } from 'zod'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import {
  MAX_RENDER_CONCURRENT,
  MAX_RENDER_GLOBAL,
  MAX_RENDER_OVERRIDES,
  MAX_RENDER_PER_WINDOW,
  renderLimitsFrom,
  renderLimitsSetting,
  type RenderLimitsSetting,
  SETTING_RENDER_LIMITS,
} from '../tools/renderLimits.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from '../credentials.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/render-limits: how many of the agent's renders run at
// once, per principal and in all, and the optional cap per 10 minutes
// (tools/renderLimits.ts). Stored in `ai_settings` under `render_limits`, which
// the render limiter reads at every render it starts and settles and while any
// render waits, so a change applies at runtime, to renders already waiting too.
//
//   GET /api/v1/ai/settings/render-limits   { per_principal, global, per_window, overrides }
//                                           (the defaults when unset)
//   PUT /api/v1/ai/settings/render-limits   body the same: per_principal 1..64,
//                                           global 0..256 (0: none), per_window
//                                           0..10000 (0: none), overrides
//                                           [{principal, concurrent 1..64}], at most 200
//
// Guarded and audited like PUT /api/v1/ai/settings/session-limits. Error bodies use `{ detail }`.

export type RenderLimitsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export const RENDER_LIMITS_PATH = '/api/v1/ai/settings/render-limits'

const PutBody = z
  .strictObject({
    per_principal: z.number().int().min(1).max(MAX_RENDER_CONCURRENT),
    global: z.number().int().min(0).max(MAX_RENDER_GLOBAL),
    per_window: z.number().int().min(0).max(MAX_RENDER_PER_WINDOW),
    overrides: z
      .array(
        z.strictObject({
          principal: z.string().trim().min(1).max(512),
          concurrent: z.number().int().min(1).max(MAX_RENDER_CONCURRENT),
        }),
      )
      .max(MAX_RENDER_OVERRIDES),
  })
  .refine((body) => new Set(body.overrides.map((o) => o.principal)).size === body.overrides.length, {
    message: 'each principal may have one override',
    path: ['overrides'],
  })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'assistant render limits'

export function registerRenderLimitsRoutes(app: Hono, deps: RenderLimitsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(RENDER_LIMITS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const view: RenderLimitsSetting = renderLimitsSetting(
      renderLimitsFrom(await settings.get<unknown>(SETTING_RENDER_LIMITS)),
    )
    return c.json(view)
  })

  app.put(RENDER_LIMITS_PATH, async (c) => {
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
    const view: RenderLimitsSetting = {
      per_principal: body.per_principal,
      global: body.global,
      per_window: body.per_window,
      overrides: body.overrides,
    }
    await settings.set(SETTING_RENDER_LIMITS, view, context)
    return c.json(view)
  })
}

/** The render limits in Settings. */
export const route: RouteModule = {
  register(app, deps) {
    registerRenderLimitsRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
