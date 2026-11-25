import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from '../credentials.js'
import { ready, type RouteModule } from './module.js'
import {
  MAX_TOOL_IMAGE_INLINE_MAX,
  MIN_TOOL_IMAGE_INLINE_MAX,
  SETTING_TOOL_IMAGE_INLINE_MAX,
  ToolImageInlineMax,
  toolImageInlineMax,
} from '../tools/imageLimits.js'

// /api/v1/ai/settings/images: the long edge, in pixels, the assistant panel
// scales an attached image down to before sending it (frontend
// src/agent/chat/images.ts `prepareImage`). Stored in `ai_settings` under
// `image_long_edge` (D4: AI settings live in the database).
//
//   GET /api/v1/ai/settings/images   { long_edge, min, max, tool_inline_max_bytes,
//                                      tool_inline_min, tool_inline_max }  (defaults when unset)
//   PUT /api/v1/ai/settings/images   body { long_edge?: MIN..MAX, tool_inline_max_bytes?:
//                                      tool_inline_min..tool_inline_max }, integers, at least one
//
// `tool_inline_max_bytes` (#7745, `ai_settings` key `tool_image_inline_max_bytes`) is the
// largest image a tool shows the model inline; a larger one goes to the session's
// workspace as a pointer (tools/imageResult.ts, limits in tools/imageLimits.ts).
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
  /** Undefined when this process was not given it (tests). */
  settings: SettingsRepo | undefined
  /** True once the API serves the agent's CRUD areas; the routes answer 503 until then. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type ImageSettingsView = {
  long_edge: number
  min: number
  max: number
  tool_inline_max_bytes: number
  tool_inline_min: number
  tool_inline_max: number
}

const LongEdge = z.number().int().min(MIN_IMAGE_LONG_EDGE).max(MAX_IMAGE_LONG_EDGE)
const PutBody = z
  .strictObject({ long_edge: LongEdge.optional(), tool_inline_max_bytes: ToolImageInlineMax.optional() })
  .refine((b) => b.long_edge !== undefined || b.tool_inline_max_bytes !== undefined, 'give long_edge or tool_inline_max_bytes')

const NOT_CONFIGURED = 'AI features are not set up in this process'
const NOT_READY = "AI unavailable: the API does not serve the agent's CRUD areas yet"
const WHAT = 'assistant image settings'

/** The stored long edge, or the default for anything the PUT could not have written. */
export function imageLongEdge(stored: unknown): number {
  const parsed = LongEdge.safeParse(stored)
  return parsed.success ? parsed.data : DEFAULT_IMAGE_LONG_EDGE
}

function view(longEdge: number, inlineMax: number): ImageSettingsView {
  return {
    long_edge: longEdge,
    min: MIN_IMAGE_LONG_EDGE,
    max: MAX_IMAGE_LONG_EDGE,
    tool_inline_max_bytes: inlineMax,
    tool_inline_min: MIN_TOOL_IMAGE_INLINE_MAX,
    tool_inline_max: MAX_TOOL_IMAGE_INLINE_MAX,
  }
}

async function stored(settings: SettingsRepo): Promise<ImageSettingsView> {
  const [longEdge, inlineMax] = await Promise.all([
    settings.get<unknown>(SETTING_IMAGE_LONG_EDGE),
    settings.get<unknown>(SETTING_TOOL_IMAGE_INLINE_MAX),
  ])
  return view(imageLongEdge(longEdge), toolImageInlineMax(inlineMax))
}

export function registerImageSettingsRoutes(app: Hono, deps: ImageSettingsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NOT_CONFIGURED
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(IMAGE_SETTINGS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    return c.json(await stored(settings))
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
    const by = { actor: UI_ACTOR, surface: 'http' as const, clientIp: deps.remoteAddress(c) }
    // Two independent settings, so two writes: when the second fails the first stays saved.
    if (body.long_edge !== undefined) await settings.set(SETTING_IMAGE_LONG_EDGE, body.long_edge, by)
    if (body.tool_inline_max_bytes !== undefined) await settings.set(SETTING_TOOL_IMAGE_INLINE_MAX, body.tool_inline_max_bytes, by)
    return c.json(await stored(settings))
  })
}

/** The assistant panel's image long edge. `deps.settings` is declared in app.ts. */
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
