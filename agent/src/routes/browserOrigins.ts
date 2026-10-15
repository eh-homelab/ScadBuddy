import type { Context, Hono } from 'hono'
import { z } from 'zod'
import type { AuditRepo } from '../audit/log.js'
import { UI_ACTOR } from '../audit/writes.js'
import { type BrowserLists, DEFAULT_ALLOW, DEFAULT_DENY, MAX_RULE_LENGTH, MAX_RULES, originOf, RuleError } from '../browser/origins.js'
import type { ApprovedOrigin, BrowserOrigins } from '../browser/store.js'
import type { OriginPolicy } from '../http/origins.js'
import { archivedError, type SessionManager, SessionError } from '../sessions/manager.js'
import { sameOwner } from '../sessions/protocol.js'
import { BROWSER_USER } from './approvals.js'
import { type RemoteAddress, uiReadProblem, uiRequestProblem } from './guard.js'
import { ready, type RouteModule } from './module.js'

// The headless browser's lists and approvals (#982, browser/origins.ts). Settings →
// Assistant edits the lists; the panel's approval card for a browser navigation calls
// the two POSTs before it approves ("Always allow", "Allow for this chat"), and
// Settings lists and revokes what was approved in each chat.
//
//   GET    /api/v1/ai/settings/browser-origins          { allow, deny, defaults: { allow, deny }, approved: [...] }
//   PUT    /api/v1/ai/settings/browser-origins          body { allow: string[], deny: string[] }
//   POST   /api/v1/ai/settings/browser-origins/allow    body { rule }: one rule added to the allow list
//   GET    /api/v1/ai/sessions/:id/browser-origins      { origins: [...] }
//   POST   /api/v1/ai/sessions/:id/browser-origins      body { origin }: approved for that session
//   DELETE /api/v1/ai/sessions/:id/browser-origins?origin=<origin>
//
// Only the user writes them: every write passes guard.ts `uiRequestProblem` (the UI's
// origin, through the HTTPS ingress, JSON only) and is audited as the UI user (a list
// change by SettingsStore.set; an approval or revocation here). A session's approvals
// are written as its other writes are (routes/sessionFiles.ts): only a session the user
// controls, and never an archived one. No agent tool reaches
// them: an agent must not widen where its own browser may go (spec §8.1).

export type BrowserOriginsRouteDeps = {
  /** Undefined when there is no database (spec §9). */
  store: BrowserOrigins | undefined
  /** The sessions an approval is written to; undefined exactly when `store` is. */
  sessions: Pick<SessionManager, 'get'> | undefined
  ready: () => Promise<boolean>
  remoteAddress: RemoteAddress
  origins: OriginPolicy
  audit?: AuditRepo | undefined
}

export type BrowserOriginsView = BrowserLists & { defaults: BrowserLists; approved: ApprovedOrigin[] }

export const BROWSER_ORIGINS_PATH = '/api/v1/ai/settings/browser-origins'

const rule = z.string().max(MAX_RULE_LENGTH)
const PutBody = z.strictObject({ allow: z.array(rule).max(MAX_RULES), deny: z.array(rule).max(MAX_RULES) })
const AllowBody = z.strictObject({ rule })
const ApproveBody = z.strictObject({ origin: z.string().max(MAX_RULE_LENGTH) })

const NO_DATABASE = 'AI features need the database: SCADBUDDY_DATABASE_URL is not set (spec §9)'
const NOT_READY = 'the AI database is unreachable or its migrations have not applied; see /healthz'
const WHAT = 'the headless browser lists'

/** `text` as an http(s) origin (scheme://host[:port]), or undefined when it is not exactly one. */
function asOrigin(text: string): string | undefined {
  try {
    const url = new URL(text)
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return undefined
    if (url.username || url.password || url.pathname !== '/' || url.search || url.hash) return undefined
    return originOf(url)
  } catch {
    return undefined
  }
}

async function body<T>(c: Context, schema: z.ZodType<T>): Promise<T | string> {
  try {
    return schema.parse(await c.req.json())
  } catch (err) {
    return err instanceof z.ZodError
      ? err.issues.map((i) => `${i.path.join('.') || 'body'}: ${i.message}`).join('; ')
      : 'body is not valid JSON'
  }
}

export function registerBrowserOriginsRoutes(app: Hono, deps: BrowserOriginsRouteDeps): void {
  async function store(): Promise<BrowserOrigins | string> {
    if (!deps.store) return NO_DATABASE
    return (await deps.ready()) ? deps.store : NOT_READY
  }
  const view = async (s: BrowserOrigins, lists?: BrowserLists): Promise<BrowserOriginsView> => ({
    ...(lists ?? (await s.lists())),
    defaults: { allow: [...DEFAULT_ALLOW], deny: [...DEFAULT_DENY] },
    approved: await s.listApproved(),
  })
  /** As routes/sessionFiles.ts `writable`: the user's own session only, and never an archived one; a Response when refused. */
  async function refusedSession(c: Context, id: string): Promise<Response | undefined> {
    if (!deps.sessions) return c.json({ detail: NO_DATABASE }, 503)
    try {
      const session = await deps.sessions.get(id, BROWSER_USER)
      if (!sameOwner(BROWSER_USER, session.owner)) {
        throw new SessionError('forbidden', `session ${session.id} is controlled by ${session.owner.label}; take it over first`)
      }
      if (session.archivedAt !== null) throw archivedError(session.id)
      return undefined
    } catch (err) {
      if (err instanceof SessionError) return c.json({ detail: err.message }, err.status)
      throw err
    }
  }
  const context = (c: Context) => ({ actor: UI_ACTOR, surface: 'http' as const, clientIp: deps.remoteAddress(c) })

  app.get(BROWSER_ORIGINS_PATH, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, WHAT)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    return c.json(await view(s))
  })

  app.put(BROWSER_ORIGINS_PATH, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    const parsed = await body(c, PutBody)
    if (typeof parsed === 'string') return c.json({ detail: parsed }, 400)
    try {
      return c.json(await view(s, await s.setLists(parsed, context(c))))
    } catch (err) {
      if (err instanceof RuleError) return c.json({ detail: err.message }, 400)
      throw err
    }
  })

  app.post(`${BROWSER_ORIGINS_PATH}/allow`, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, `changes to ${WHAT}`)
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    const parsed = await body(c, AllowBody)
    if (typeof parsed === 'string') return c.json({ detail: parsed }, 400)
    try {
      return c.json(await view(s, await s.allow(parsed.rule, context(c))))
    } catch (err) {
      if (err instanceof RuleError) return c.json({ detail: err.message }, 400)
      throw err
    }
  })

  const sessionPath = '/api/v1/ai/sessions/:id/browser-origins'

  app.get(sessionPath, async (c) => {
    const problem = uiReadProblem(c, deps.origins, deps.remoteAddress, 'approved browser origins')
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    return c.json({ origins: await s.listApproved(c.req.param('id')) })
  })

  const audited = async (c: Context, action: string, detail: string, ok: boolean) => {
    await deps.audit?.record({
      kind: 'settings',
      action,
      surface: 'http',
      actor: UI_ACTOR,
      clientIp: deps.remoteAddress(c),
      sessionId: c.req.param('id'),
      outcome: ok ? 'ok' : 'error',
      detail,
    })
  }

  app.post(sessionPath, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'browser approvals')
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    const parsed = await body(c, ApproveBody)
    if (typeof parsed === 'string') return c.json({ detail: parsed }, 400)
    const origin = asOrigin(parsed.origin)
    if (!origin) return c.json({ detail: `origin: "${parsed.origin}" is not an http or https origin (scheme://host[:port])` }, 400)
    const id = c.req.param('id')
    const refused = await refusedSession(c, id)
    if (refused) return refused
    const ok = await s.approve(id, origin, 'session')
    if (!ok) return c.json({ detail: `no session ${id}` }, 404)
    await audited(c, 'browser_session_origin', `${origin} approved for the session`, true)
    return c.json({ origin, scope: 'session' })
  })

  app.delete(sessionPath, async (c) => {
    const problem = uiRequestProblem(c, deps.origins, deps.remoteAddress, 'browser approvals')
    if (problem) return c.json({ detail: problem }, 403)
    const s = await store()
    if (typeof s === 'string') return c.json({ detail: s }, 503)
    const asked = c.req.query('origin') ?? ''
    const origin = asOrigin(asked)
    if (!origin) return c.json({ detail: `origin: "${asked}" is not an http or https origin (scheme://host[:port])` }, 400)
    const refused = await refusedSession(c, c.req.param('id'))
    if (refused) return refused
    const ok = await s.revoke(c.req.param('id'), origin)
    if (!ok) return c.json({ detail: `${origin} is not approved in session ${c.req.param('id')}` }, 404)
    await audited(c, 'browser_session_origin', `${origin} revoked for the session`, true)
    return c.body(null, 204)
  })
}

declare module '../app.js' {
  interface AppDeps {
    /** The headless browser's lists and approvals (#982, browser/store.ts); the routes answer 503 without it. */
    browserOrigins?: BrowserOrigins | undefined
  }
}

/** The headless browser's allow and deny lists, and the origins approved per session (#982). */
export const route: RouteModule = {
  register(app, deps) {
    registerBrowserOriginsRoutes(app, {
      store: deps.browserOrigins,
      sessions: deps.sessions,
      ready: ready(deps),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      audit: deps.audit,
    })
  },
}
