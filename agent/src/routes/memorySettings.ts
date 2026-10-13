import type { Hono } from 'hono'
import { z } from 'zod'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import {
  DEFAULT_RECALL_MAX_QUERY_CHARS,
  MAX_RECALL_MAX_QUERY_CHARS,
  recallMaxQueryChars,
  SETTING_RECALL_MAX_QUERY_CHARS,
} from '../memory/hindsight.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import type { SettingsRepo } from './httpRequest.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/memory: the longest automatic Hindsight recall query, in
// characters (memory/hindsight.ts `capRecallQuery`). The Claude Code Hindsight plugin's
// `recallMaxQueryChars`, with its default (800) and its `0` for no cut. Stored in
// `ai_settings` under `memory_recall_max_query_chars`, which both session modes read at
// each turn (sessions/manager.ts `memoryHooks`, temporal/memoryActivities.ts), so a
// change applies to the next turn.
//
//   GET /api/v1/ai/settings/memory   { recall_max_query_chars, default, max }  (the default when unset)
//   PUT /api/v1/ai/settings/memory   body { recall_max_query_chars: 0..MAX, an integer }
//
// The bank's own limit is in tokens (Hindsight's HINDSIGHT_API_RECALL_MAX_QUERY_TOKENS,
// default 500); a query it still refuses is retried once, shorter. Guarded and audited
// like the other AI settings routes. Error bodies use `{ detail }`.

export const MEMORY_SETTINGS_PATH = '/api/v1/ai/settings/memory'

export type MemorySettingsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  settings: SettingsRepo | undefined
  /** Applies migrations; the routes answer 503 until it resolves true. */
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
}

export type MemorySettingsView = { recall_max_query_chars: number; default: number; max: number }

const PutBody = z.strictObject({
  recall_max_query_chars: z.number().int().min(0).max(MAX_RECALL_MAX_QUERY_CHARS),
})

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'assistant memory settings'

function view(chars: number): MemorySettingsView {
  return { recall_max_query_chars: chars, default: DEFAULT_RECALL_MAX_QUERY_CHARS, max: MAX_RECALL_MAX_QUERY_CHARS }
}

export function registerMemorySettingsRoutes(app: Hono, deps: MemorySettingsRouteDeps): void {
  async function repo(): Promise<SettingsRepo | string> {
    if (!deps.settings) return NO_DATABASE
    return (await deps.ready()) ? deps.settings : NOT_READY
  }

  app.get(MEMORY_SETTINGS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const settings = await repo()
    if (typeof settings === 'string') return c.json({ detail: settings }, 503)
    return c.json(view(recallMaxQueryChars(await settings.get<unknown>(SETTING_RECALL_MAX_QUERY_CHARS))))
  })

  app.put(MEMORY_SETTINGS_PATH, async (c) => {
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
    await settings.set(SETTING_RECALL_MAX_QUERY_CHARS, body.recall_max_query_chars, {
      actor: UI_ACTOR,
      surface: 'http',
      clientIp: deps.remoteAddress(c),
    })
    return c.json(view(body.recall_max_query_chars))
  })
}

/** The automatic memory's recall query cap. `deps.settings` is declared in routes/httpRequest.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerMemorySettingsRoutes(app, {
      settings: deps.settings,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
