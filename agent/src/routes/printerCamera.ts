import type { Hono } from 'hono'
import { z } from 'zod'
import { BUILTIN_SOURCES } from '../access/model.js'
import type { AccessReader, AccessWriter } from '../access/store.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/printer-camera (#1911): turns the `get_printer_camera`
// tool off or on, for session turns, /mcp and durable sessions alike. It is that
// tool's access in `ai_access` (#2171): `off` under ('builtin:scadbuddy',
// 'get_printer_camera') while the camera is off, no row while it is on, so every
// registry gate sees it through the overrides (registry.ts `effectiveTool`). A
// printer's camera frame is privacy-sensitive (#251).
//
//   GET /api/v1/ai/settings/printer-camera   { "enabled": boolean }
//   PUT /api/v1/ai/settings/printer-camera   body { "enabled": boolean }
//
// The PUT is a settings write (outward, spec §8.1), so it passes guard.ts
// `uiRequestProblem`. The store audits the write.

const SOURCE = BUILTIN_SOURCES.scadbuddy
const ITEM = 'get_printer_camera'

export type PrinterCameraRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  access: (AccessReader & AccessWriter) | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type PrinterCameraSettingView = { enabled: boolean }

const PutBody = z.strictObject({ enabled: z.boolean() })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'the printer camera tool setting'

export const PRINTER_CAMERA_SETTING_PATH = '/api/v1/ai/settings/printer-camera'

export function registerPrinterCameraRoutes(app: Hono, deps: PrinterCameraRouteDeps): void {
  const base = PRINTER_CAMERA_SETTING_PATH

  async function repo(): Promise<(AccessReader & AccessWriter) | string> {
    if (!deps.access) return NO_DATABASE
    return (await deps.ready()) ? deps.access : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const access = await repo()
    if (typeof access === 'string') return c.json({ detail: access }, 503)
    const row = (await access.rows([SOURCE])).find((r) => r.item === ITEM)
    const view: PrinterCameraSettingView = { enabled: row?.access !== 'off' }
    return c.json(view)
  })

  app.put(base, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const access = await repo()
    if (typeof access === 'string') return c.json({ detail: access }, 503)
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
    const context = { actor: UI_ACTOR, surface: 'http' as const, clientIp: deps.remoteAddress(c) }
    if (body.enabled) await access.resetItem(SOURCE, ITEM, context)
    else await access.setItem(SOURCE, ITEM, 'off', context)
    const view: PrinterCameraSettingView = { enabled: body.enabled }
    return c.json(view)
  })
}

/** The printer camera tool's switch (#1911). `deps.access` is declared in app.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerPrinterCameraRoutes(app, {
      access: deps.access,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
