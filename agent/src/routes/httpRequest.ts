import type { Hono } from 'hono'
import { z } from 'zod'
import { BUILTIN_SOURCES } from '../access/model.js'
import { sourceEnabled } from '../access/resolve.js'
import type { AccessReader, AccessWriter } from '../access/store.js'
import type { AuditContext } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import type { OriginPolicy } from '../http/origins.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// /api/v1/ai/settings/http-request (#827): turns the assistant's `http_request`
// tool on or off for session turns. It is the `builtin:http` source's switch in
// `ai_access` (#2171): its `*` row, read at the start of every turn
// (sessions/turnRunner.ts, temporal/turnServers.ts). ON by default (#827): on
// unless that row is `off`.
//
//   GET /api/v1/ai/settings/http-request   { "enabled": boolean }
//   PUT /api/v1/ai/settings/http-request   body { "enabled": boolean }
//
// The PUT is a settings write (outward, spec §8.1), so it passes guard.ts
// `uiRequestProblem`: from the UI's origin, through the HTTPS ingress, JSON
// only. The store audits the write as the UI user (access/store.ts AccessStore).

export type SettingsRepo = {
  get<T>(key: string): Promise<T | undefined>
  /** `context` says who wrote it, for the audit row (credentials.ts SettingsStore.set). */
  set(key: string, value: unknown, context: AuditContext): Promise<void>
}

export type HttpRequestRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  access: (AccessReader & AccessWriter) | undefined
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

  async function repo(): Promise<(AccessReader & AccessWriter) | string> {
    if (!deps.access) return NO_DATABASE
    return (await deps.ready()) ? deps.access : NOT_READY
  }

  app.get(base, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const access = await repo()
    if (typeof access === 'string') return c.json({ detail: access }, 503)
    const view: HttpRequestSettingView = { enabled: sourceEnabled(BUILTIN_SOURCES.http, await access.rows([BUILTIN_SOURCES.http])) }
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
    await access.setEnabled(BUILTIN_SOURCES.http, body.enabled, { actor: UI_ACTOR, surface: 'http', clientIp: deps.remoteAddress(c) })
    const view: HttpRequestSettingView = { enabled: body.enabled }
    return c.json(view)
  })
}

declare module '../app.js' {
  interface AppDeps {
    /** `ai_settings` (credentials.ts SettingsStore); the settings routes (session limits #790, routes/sessionLimits.ts, ...) answer 503 without it. */
    settings?: SettingsRepo | undefined
  }
}

/** The HTTP request tool's switch (#827). `deps.access` is declared in app.ts. */
export const route: RouteModule = {
  register(app, deps) {
    registerHttpRequestRoutes(app, {
      access: deps.access,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  },
}
