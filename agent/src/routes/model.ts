import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { SETTING_MODEL } from '../sessions/manager.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './headlessBrowser.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/model (#1917, #255): the Claude model every assistant
// turn uses (sessions/manager.ts reads `ai_settings.model` at the start of each
// turn) and the connection test uses (main.ts `testConnection`). `null` leaves
// the choice to Claude Code, whose default it then is.
//
//   GET /api/v1/ai/settings/model   { model: string | null }
//   PUT /api/v1/ai/settings/model   body { model: string | null }
//
// The name is handed to Claude Code as `--model`, so it takes what Claude Code
// takes (an alias such as `opus`, a full id, a `[1m]` suffix, a Bedrock or
// Vertex id) and nothing that could read as another option or carry a space.
// Whether the model exists is the connection test's to say.
//
// Guarded and audited like routes/sessionLimits.ts: the write passes guard.ts
// `uiRequestProblem` and is a `settings` audit row as the browser user; the
// read passes `uiReadProblem`. Error bodies use `{ detail }`.

export type ModelRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type ModelSettingView = { model: string | null }

export const MODEL_SETTING_PATH = '/api/v1/ai/settings/model'

/** A model name Claude Code's `--model` takes: starts with a letter or digit, no spaces. */
const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:@/[\]-]{0,127}$/

const PutBody = z.strictObject({
  model: z
    .string()
    .trim()
    .regex(MODEL_NAME, 'must be a model name or alias, such as opus or claude-sonnet-5 (letters, digits and . _ : @ / [ ] -)')
    .nullable(),
})

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'the assistant model'

/** The model a stored value names, or undefined for Claude Code's default. */
export function storedModel(value: unknown): string | undefined {
  return typeof value === 'string' && value ? value : undefined
}

export function registerModelRoutes(app: Hono, deps: ModelRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(MODEL_SETTING_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    const view: ModelSettingView = { model: storedModel(await settings.get<unknown>(SETTING_MODEL)) ?? null }
    return c.json(view)
  })

  app.put(MODEL_SETTING_PATH, async (c) => {
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
    await settings.set(SETTING_MODEL, body.model, { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) })
    const view: ModelSettingView = { model: body.model }
    return c.json(view)
  })
}

/** The assistant's model in Settings (#1917). `deps.settings` is declared in routes/headlessBrowser.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerModelRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
