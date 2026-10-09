import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/images: the long edge, in pixels, the assistant panel
// scales an attached image down to before sending it (frontend
// src/agent/chat/images.ts `prepareImage`). Stored in `ai_settings` under
// `image_long_edge` (D4: AI settings live in the database).
//
//   GET /api/v1/ai/settings/images   { long_edge, min, max }  (the default when unset)
//   PUT /api/v1/ai/settings/images   body { long_edge: MIN..MAX, an integer }
//
// The default, 1568 px, is the Messages API's standard-tier long edge: a larger
// image is downscaled to it anyway on every model before Claude 4.7. Claude 4.7
// and later read up to 2576 px (the high-resolution tier), at up to about three
// times the image tokens, so MAX is that. MIN is 200 px, below which the vision
// docs say accuracy suffers. The agent never decodes an image, so it does not
// check dimensions itself (sessions/images.ts checks type, signature and size);
// the setting only tells the panel how far to scale.
//
// Guarded and audited like the other AI settings routes: the write passes
// guard.ts `uiRequestProblem` and is a `settings` audit row as the browser
// user, with the client address (credentials.ts SettingsStore.set). The read
// passes `uiReadProblem`. Error bodies use `{ detail }`.

export const SETTING_IMAGE_LONG_EDGE = 'image_long_edge'
export const DEFAULT_IMAGE_LONG_EDGE = 1568
export const MIN_IMAGE_LONG_EDGE = 200
export const MAX_IMAGE_LONG_EDGE = 2576

export const IMAGE_SETTINGS_PATH = '/api/v1/ai/settings/images'

export type ImageSettingsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type ImageSettingsView = { long_edge: number; min: number; max: number }

const LongEdge = z.number().int().min(MIN_IMAGE_LONG_EDGE).max(MAX_IMAGE_LONG_EDGE)
const PutBody = z.strictObject({ long_edge: LongEdge })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'assistant image settings'

/** The stored long edge, or the default for anything the PUT could not have written. */
export function imageLongEdge(stored: unknown): number {
  const parsed = LongEdge.safeParse(stored)
  return parsed.success ? parsed.data : DEFAULT_IMAGE_LONG_EDGE
}

function view(longEdge: number): ImageSettingsView {
  return { long_edge: longEdge, min: MIN_IMAGE_LONG_EDGE, max: MAX_IMAGE_LONG_EDGE }
}

export function registerImageSettingsRoutes(app: Hono, deps: ImageSettingsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(IMAGE_SETTINGS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    return c.json(view(imageLongEdge(await settings.get<unknown>(SETTING_IMAGE_LONG_EDGE))))
  })

  app.put(IMAGE_SETTINGS_PATH, async (c) => {
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
    await settings.set(SETTING_IMAGE_LONG_EDGE, body.long_edge, {
      actor: UI_ACTOR,
      surface: 'http',
      clientIp: deps.remoteAddress(c),
    })
    return c.json(view(body.long_edge))
  })
}

/** The assistant panel's image long edge. `deps.settings` is declared in routes/headlessBrowser.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerImageSettingsRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
