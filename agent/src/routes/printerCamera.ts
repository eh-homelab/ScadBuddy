import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { SETTING_PRINTER_CAMERA, switchOn } from '../tools/switches.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/printer-camera (#1911): turns the `get_printer_camera`
// tool off or on, for session turns, /mcp and durable sessions alike: a call
// to it is refused while it is off (tools/switches.ts, checked in registry.ts
// `runToolWithOutcome`). A printer's camera frame is privacy-sensitive (#251).
// Stored in `ai_settings` under `printer_camera_enabled`; ON by default
// (anything but a stored `false`), as the tool shipped (#957). The same shape
// and guards as the http_request switch (routes/httpRequest.ts):
//
//   GET /api/v1/ai/settings/printer-camera   { "enabled": boolean }
//   PUT /api/v1/ai/settings/printer-camera   body { "enabled": boolean }
//
// The PUT is a settings write (outward, spec §8.1), so it passes guard.ts
// `uiRequestProblem`. The store audits the write as the UI user (credentials.ts SettingsStore.set).

export type PrinterCameraRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
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

  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const view: PrinterCameraSettingView = { enabled: switchOn(await settings.get<unknown>(SETTING_PRINTER_CAMERA)) }
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
    await settings.set(SETTING_PRINTER_CAMERA, body.enabled, { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) })
    const view: PrinterCameraSettingView = { enabled: body.enabled }
    return c.json(view)
  })
}

/** The printer camera tool's switch (#1911). `deps.settings` is declared in routes/headlessBrowser.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerPrinterCameraRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
