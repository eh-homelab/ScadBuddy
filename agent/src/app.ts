import { Hono } from 'hono'
import type { UpgradeWebSocket } from 'hono/ws'
import { aiStatus, type AiStatus, type CredentialState } from './aiStatus.js'
import type { AuditRepo } from './audit/log.js'
import { auditWrites, RefusalCoalescer } from './audit/writes.js'
import type { CredentialRepo } from './credentials.js'
import type { Resolver } from './http/egress.js'
import type { OriginPolicy } from './http/origins.js'
import { type McpEndpointDeps, type McpHandle, mountMcp } from './mcp/http.js'
import type { RemoteAddress } from './routes/guard.js'
import { ROUTES } from './routes/index.js'
import type { TabHub } from './bridge/hub.js'
import type { KekStatus } from './secrets.js'
import type { SessionManager } from './sessions/manager.js'

// The HTTP surface. Hono per spec §4.5: web-standard Request/Response and
// direct streaming. /healthz and /mcp (when `mcp` is given; #251, mcp/http.ts) are
// here; every other route group is a file in routes/ that exports `route`
// (routes/module.ts), registered below without an edit to this file: among them
// /api/v1/ai/status (routes/status.ts) and the session routes and the assistant's
// chat socket (#300, #256, routes/sessions.ts, routes/chat.ts).
//
// Every response carries `X-ScadBuddy-Service: agent`, so a request through
// the ingress shows which container answered it (spec §4.2: the agent's paths
// must win over the backend's /api/v1/*; docs/ai/operating.md has the check).

export type Probe = () => Promise<boolean>

/**
 * What the app is built from. Only what `/healthz`, `/mcp` and several route groups
 * share is declared here; a dependency only one route group needs is declared in that
 * group's file, by augmenting this interface (routes/module.ts).
 */
export interface AppDeps {
  /** Undefined when SCADBUDDY_DATABASE_URL is unset. */
  database: { ping: Probe; ready: Probe } | undefined
  backend: Probe
  /** The key-encryption key, or why there is none (secrets.ts `loadKek`). */
  kek: KekStatus
  /** Undefined exactly when `database` is. */
  credentials: CredentialRepo | undefined
  remoteAddress: RemoteAddress
  /** Which origins may write (SCADBUDDY_PUBLIC_URL, SCADBUDDY_AGENT_TRUSTED_PROXIES; src/http/origins.ts). */
  origins: OriginPolicy
  /** Gateway host resolver for the SSRF check; the system resolver when omitted. */
  resolveHost?: Resolver
  /** Upper bound on each database step of /healthz (migrations, credential read). */
  healthTimeoutMs?: number
  /**
   * The external MCP endpoint (src/mcp/http.ts). Left out, there is no /mcp
   * route. It uses the same `origins` policy and `remoteAddress` as the
   * credential routes, so there is one allowlist (src/http/origins.ts).
   */
  mcp?: McpEndpointDeps | undefined
  /**
   * Sessions (#300); the session routes and the chat socket answer 503 without it.
   * Shared by routes/sessions.ts, routes/chat.ts and routes/status.ts.
   */
  sessions?: SessionManager | undefined
  /**
   * The runtime's WebSocket upgrade; without it there is no chat socket (and status
   * says so). Shared by routes/chat.ts and routes/status.ts.
   */
  upgradeWebSocket?: UpgradeWebSocket | undefined
  /**
   * The audit log (#258, audit/log.ts): GET /api/v1/ai/audit reads it (503
   * without it; routes/audit.ts), and credential, MCP token and plugin writes
   * are recorded in it (the middleware in `createApp`). Here because several
   * route groups use it.
   */
  audit?: AuditRepo | undefined
  /**
   * The tabs of the browser bridge (#254, bridge/hub.ts): their socket
   * (routes/bridge.ts), and the chat socket's `tab.bind`. Left out, there is
   * no bridge socket, and the browser_* tools answer "no browser attached".
   */
  tabs?: TabHub | undefined
}

/** Which credential requests are writes, by method (audit/writes.ts). */
function credentialVerb(method: string, path: string): string | undefined {
  const base = '/api/v1/ai/credentials'
  if (path === base) return method === 'PUT' ? 'save' : method === 'DELETE' ? 'delete' : undefined
  if (path === `${base}/entries`) return method === 'POST' ? 'create' : undefined
  if (path === `${base}/order`) return method === 'PUT' ? 'reorder' : undefined
  if (/^\/api\/v1\/ai\/credentials\/entries\/[^/]+\/reset$/.test(path)) return method === 'POST' ? 'reset' : undefined
  if (/^\/api\/v1\/ai\/credentials\/entries\/[^/]+$/.test(path)) {
    return method === 'PUT' ? 'save' : method === 'DELETE' ? 'delete' : undefined
  }
  return undefined
}

/** MCP token mint and revoke (routes/mcpTokens.ts). */
function tokenVerb(method: string, path: string): string | undefined {
  if (method === 'POST' && path === '/api/v1/ai/mcp-tokens') return 'mint'
  if (method === 'DELETE' && path.startsWith('/api/v1/ai/mcp-tokens/')) return 'revoke'
  return undefined
}

/** A raise of one session's budget (routes/sessions.ts); no other session route is recorded here. */
function budgetVerb(method: string, path: string): string | undefined {
  return method === 'POST' && /^\/api\/v1\/ai\/sessions\/[^/]+\/budget$/.test(path) ? 'session_budget_usd' : undefined
}

/** Which plugin requests are writes; connection tests are not. */
function pluginVerb(method: string, path: string): string | undefined {
  if (path.endsWith('/test')) return undefined
  const one = path.startsWith('/api/v1/ai/plugins/')
  if (method === 'POST' && !one) return 'create'
  if (method === 'PATCH' && one) return 'update'
  if (method === 'DELETE' && one) return 'delete'
  return undefined
}

export type { AiStatus } from './aiStatus.js'

/** The response header naming the service (see the module comment). */
export const SERVICE_HEADER = 'X-ScadBuddy-Service'

export type Health = {
  status: 'ok'
  ai: AiStatus
  database: 'ok' | 'unreachable' | 'not configured'
  backend: 'ok' | 'unreachable'
  secret_key: 'ok' | 'not configured'
  credential: CredentialState
}

/** The app, plus `close()` for graceful shutdown: it ends every open `/mcp` session and its sweep, and every session event stream. */
export type AgentApp = Hono & { close: () => Promise<void> }

export function createApp(deps: AppDeps): AgentApp {
  const app = new Hono()
  let mcp: McpHandle | undefined

  app.use('*', async (c, next) => {
    await next()
    try {
      c.res.headers.set(SERVICE_HEADER, 'agent')
    } catch {
      // An immutable response (a WebSocket upgrade's): it goes without.
    }
  })

  // Liveness: always 200 while the process serves HTTP. A missing or
  // unreachable database or backend is REPORTED, not failed on, so a Postgres
  // blip does not restart the container.
  app.get('/healthz', async (c) => {
    const [dbOk, backendOk] = await Promise.all([
      deps.database ? deps.database.ping() : Promise.resolve(undefined),
      deps.backend(),
    ])
    const { ai, credential } = await aiStatus(deps, dbOk)
    const body: Health = {
      status: 'ok',
      ai,
      database: dbOk === undefined ? 'not configured' : dbOk ? 'ok' : 'unreachable',
      backend: backendOk ? 'ok' : 'unreachable',
      secret_key: deps.kek.ok ? 'ok' : 'not configured',
      credential,
    }
    return c.json(body)
  })

  // Credential, MCP token and plugin writes, refused attempts included, go in the
  // audit log (#258). App-wide, like the service header, and mounted before the
  // route groups so it runs around them.
  if (deps.audit) {
    const audit = deps.audit
    // Refusals need no authentication, so they are coalesced per peer and
    // action rather than written one row per request (audit/writes.ts).
    const refusals = new RefusalCoalescer(audit)
    const writes = { audit, remoteAddress: deps.remoteAddress, refusals }
    // `/*` matches the bare path too (Hono), so this covers both sets of routes.
    app.use('/api/v1/ai/credentials/*', auditWrites({ ...writes, kind: 'credential', verb: credentialVerb }))
    // Refused or failed token writes; successful ones are recorded by the
    // token store itself (audit/writes.ts auditedTokenStore), with the token's id.
    app.use('/api/v1/ai/mcp-tokens/*', auditWrites({ ...writes, kind: 'token', verb: tokenVerb, failuresOnly: true }))
    // Hono's `/*` also matches the bare prefix, so this covers POST /api/v1/ai/plugins too.
    app.use('/api/v1/ai/plugins/*', auditWrites({ ...writes, kind: 'plugin', verb: pluginVerb }))
    // Refused or failed raises of a session's budget (#790); a raise that
    // lands is recorded by the manager (sessions/manager.ts raiseBudget).
    app.use('/api/v1/ai/sessions/*', auditWrites({ ...writes, kind: 'settings', verb: budgetVerb, failuresOnly: true }))
  }

  const shutdown = new AbortController()
  for (const { route } of ROUTES) route.register(app, deps, shutdown.signal)

  if (deps.mcp) {
    const database = deps.database
    if (database) {
      // /mcp serves only once the agent's migrations have applied (db.ts
      // `ready()`), so its database-backed stores never see a half-made schema.
      app.use('/mcp', async (c, next) => {
        if (!(await database.ready())) {
          return c.json({ error: 'AI unavailable: database migrations have not applied' }, 503)
        }
        await next()
      })
      mcp = mountMcp(app, deps.mcp, { origins: deps.origins, remoteAddress: deps.remoteAddress })
    } else {
      // Spec §9, "No database": AI features are disabled. /mcp answers why
      // instead of 404, so an MCP client's error names the fix.
      app.all('/mcp', (c) =>
        c.json({ error: 'AI disabled: no database (SCADBUDDY_DATABASE_URL is not set)' }, 503),
      )
    }
  }

  return Object.assign(app, {
    close: async () => {
      shutdown.abort()
      await mcp?.close()
    },
  })
}
