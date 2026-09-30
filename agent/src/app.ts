import { Hono } from 'hono'
import type { UpgradeWebSocket } from 'hono/ws'
import type { ApprovalService } from './approvals/service.js'
import { DEFAULT_MCP_AUTH } from './auth/authenticate.js'
import type { TokenStore } from './auth/tokens.js'
import type { Credential, CredentialRepo } from './credentials.js'
import type { ConnectionTest } from './harness/testConnection.js'
import type { Resolver } from './http/egress.js'
import type { OriginPolicy } from './http/origins.js'
import { type McpEndpointDeps, type McpHandle, mountMcp } from './mcp/http.js'
import type { PluginForwarder } from './plugins/forwarder.js'
import type { PackageInstaller } from './plugins/packages/install.js'
import type { PackageRepo } from './plugins/packages/store.js'
import type { PluginRepo, RemotePlugin } from './plugins/registry.js'
import { type PluginTest, testPlugin } from './plugins/testConnection.js'
import type { AuditRepo } from './audit/log.js'
import type { TabHub } from './bridge/hub.js'
import { auditWrites, RefusalCoalescer } from './audit/writes.js'
import { registerApprovalRoutes } from './routes/approvals.js'
import { registerAuditRoutes } from './routes/audit.js'
import { registerBridgeRoute } from './routes/bridge.js'
import { registerChatRoute } from './routes/chat.js'
import { registerCredentialRoutes } from './routes/credentials.js'
import { registerPluginPackageRoutes } from './routes/pluginPackages.js'
import { type McpAuthRouteDeps, registerMcpAuthRoutes } from './routes/mcpAuth.js'
import { registerHeadlessBrowserRoutes, type SettingsRepo } from './routes/headlessBrowser.js'
import { registerPluginRoutes } from './routes/plugins.js'
import { registerMcpAuthModeRoutes, type SettingsWriter } from './routes/mcpAuthMode.js'
import { registerMcpTokenRoutes } from './routes/mcpTokens.js'
import { registerSessionLimitsRoutes } from './routes/sessionLimits.js'
import { registerSessionRoutes } from './routes/sessions.js'
import { type RemoteAddress, uiReadProblem } from './routes/guard.js'
import type { KekStatus } from './secrets.js'
import type { SessionManager } from './sessions/manager.js'

// The HTTP surface. Hono per spec §4.5: web-standard Request/Response and
// direct streaming. /healthz, /api/v1/ai/status (below), the Claude credential
// routes (#255, routes/credentials.ts), the approval routes (#258,
// routes/approvals.ts), the plugin registry routes (#297, routes/plugins.ts),
// the plugin package routes (#297, routes/pluginPackages.ts), the MCP token and
// auth-mode routes (#251, routes/mcpTokens.ts, routes/mcpAuthMode.ts), the
// headless-browser setting (#349, routes/headlessBrowser.ts), the session routes
// and the assistant's chat socket (#300, #256, routes/sessions.ts,
// routes/chat.ts), the browser bridge's tab socket (#254, routes/bridge.ts),
// and /mcp when `mcp` is given (#251, mcp/http.ts).
//
// Every response carries `X-ScadBuddy-Service: agent`, so a request through
// the ingress shows which container answered it (spec §4.2: the agent's paths
// must win over the backend's /api/v1/*; docs/ai/operating.md has the check).

export type Probe = () => Promise<boolean>

export type AppDeps = {
  /** Undefined when SCADBUDDY_DATABASE_URL is unset. */
  database: { ping: Probe; ready: Probe } | undefined
  backend: Probe
  /** The key-encryption key, or why there is none (secrets.ts `loadKek`). */
  kek: KekStatus
  /** Undefined exactly when `database` is. */
  credentials: CredentialRepo | undefined
  /** The plugin registry (#297); undefined when there is no database. */
  plugins?: PluginRepo | undefined
  /** The plugin connection test; src/plugins/testConnection.ts when omitted. */
  testPlugin?: (plugin: RemotePlugin, address: string) => Promise<PluginTest>
  /** The loopback forwarder plugin traffic goes through (plugins/forwarder.ts); needed by the default test. */
  pluginForwarder?: PluginForwarder
  /** Installed plugin packages (#297, plugins/packages/); undefined when there is no database. */
  pluginPackages?: PackageRepo | undefined
  /** Fetches and caches plugin packages; undefined disables installing. */
  packageInstaller?: Pick<PackageInstaller, 'prepare' | 'evict'> | undefined
  testConnection: (credential: Credential) => Promise<ConnectionTest>
  /**
   * The MCP bearer-token store Settings manages (routes/mcpTokens.ts). Pass the
   * same instance as `mcp.tokens`. Undefined (or left out) when there is no
   * database: the routes then answer 503.
   */
  tokens?: TokenStore | undefined
  /**
   * Where Settings writes the /mcp auth mode and anonymous cap
   * (routes/mcpAuthMode.ts; credentials.ts `SettingsStore`). The routes read
   * them back through `mcp.authSettings`. Undefined (or left out) when there is
   * no database: the routes then answer 503.
   */
  aiSettings?: SettingsWriter | undefined
  remoteAddress: RemoteAddress
  /** Which origins may write (SCADBUDDY_PUBLIC_URL, SCADBUDDY_AGENT_TRUSTED_PROXIES; src/http/origins.ts). */
  origins: OriginPolicy
  /** Gateway host resolver for the SSRF check; the system resolver when omitted. */
  resolveHost?: Resolver
  /** Connection-test cooldown; routes/credentials.ts's default when omitted. */
  testCooldownMs?: number
  /** Upper bound on each database step of /healthz (migrations, credential read). */
  healthTimeoutMs?: number
  /** Clock for the connection-test cooldown; Date.now when omitted. */
  now?: () => number
  /** Approvals of outward tool calls (#258); the routes answer 503 without it. */
  approvals?: ApprovalService
  /**
   * `ai_settings` (credentials.ts SettingsStore); the headless-browser setting (#349) and the session
   * limits (#790) answer 503 without it.
   */
  settings?: SettingsRepo | undefined
  /**
   * The external MCP endpoint (src/mcp/http.ts). Left out, there is no /mcp
   * route. It uses the same `origins` policy and `remoteAddress` as the
   * credential routes, so there is one allowlist (src/http/origins.ts).
   */
  mcp?: McpEndpointDeps | undefined
  /** Sessions (#300); the session routes and the chat socket answer 503 without it. */
  sessions?: SessionManager | undefined
  /** The runtime's WebSocket upgrade; without it there is no chat socket (and status says so). */
  upgradeWebSocket?: UpgradeWebSocket | undefined
  /** How often the chat socket re-reads the session list (routes/chat.ts SNAPSHOT_MS when omitted). */
  chatSnapshotMs?: number
  /**
   * The OIDC settings routes for /mcp (#262, routes/mcpAuth.ts). Left out,
   * there are none. `repo` is undefined exactly when `database` is.
   */
  mcpOidc?: Pick<McpAuthRouteDeps, 'repo' | 'provider' | 'publicUrl'> | undefined
  /**
   * The audit log (#258, audit/log.ts): GET /api/v1/ai/audit reads it (503
   * without it), and credential and plugin writes are recorded in it.
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
  if (path !== '/api/v1/ai/credentials') return undefined
  return method === 'PUT' ? 'save' : method === 'DELETE' ? 'delete' : undefined
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

export const DEFAULT_HEALTH_TIMEOUT_MS = 2000

/** The response header naming the service (see the module comment). */
export const SERVICE_HEADER = 'X-ScadBuddy-Service'

/**
 * `ai` is `enabled` only when every prerequisite holds; otherwise it names the
 * first one missing, in the order an operator has to fix them. The CI smoke
 * test reads it (.github/workflows/ci.yml asserts the no-database string), and
 * so does the UI's gate, through GET /api/v1/ai/status (`AiStatusView`;
 * frontend src/agent/chat/availability.ts). Keep the strings stable for both.
 */
export type AiStatus =
  | 'enabled'
  | 'disabled (no database)'
  | 'unavailable (database unreachable)'
  | 'unavailable (database migrations failed)'
  | 'unavailable (database timed out)'
  | `disabled (no key-encryption key: ${string})`
  | 'disabled (no Claude credential)'
  | 'unavailable (stored credential was sealed with a different key-encryption key)'
  | 'unavailable (stored credential is in an outdated format; save it again)'

export type Health = {
  status: 'ok'
  ai: AiStatus
  database: 'ok' | 'unreachable' | 'not configured'
  backend: 'ok' | 'unreachable'
  secret_key: 'ok' | 'not configured'
  credential: 'configured' | 'not configured' | 'unknown'
}

const TIMED_OUT = Symbol('timed out')

/** `promise`, or TIMED_OUT after `ms`. The promise keeps running; its result is dropped. */
async function within<T>(promise: Promise<T>, ms: number): Promise<T | typeof TIMED_OUT> {
  let timer: NodeJS.Timeout | undefined
  const deadline = new Promise<typeof TIMED_OUT>((resolve) => {
    timer = setTimeout(() => resolve(TIMED_OUT), ms)
  })
  try {
    return await Promise.race([promise, deadline])
  } finally {
    clearTimeout(timer)
  }
}

async function aiStatus(deps: AppDeps, dbOk: boolean | undefined): Promise<Pick<Health, 'ai' | 'credential'>> {
  if (dbOk === undefined || !deps.database || !deps.credentials) {
    return { ai: 'disabled (no database)', credential: 'unknown' }
  }
  if (!dbOk) return { ai: 'unavailable (database unreachable)', credential: 'unknown' }
  // Both steps are bounded: ready() may be waiting on the migration advisory
  // lock (bounded itself by lock_timeout, db/migrations.ts), and a liveness
  // probe must answer well inside its own timeout regardless.
  const timeoutMs = deps.healthTimeoutMs ?? DEFAULT_HEALTH_TIMEOUT_MS
  const ready = await within(deps.database.ready(), timeoutMs)
  if (ready === TIMED_OUT) return { ai: 'unavailable (database timed out)', credential: 'unknown' }
  if (!ready) return { ai: 'unavailable (database migrations failed)', credential: 'unknown' }
  let stored
  try {
    stored = await within(deps.credentials.get(), timeoutMs)
  } catch {
    return { ai: 'unavailable (database unreachable)', credential: 'unknown' }
  }
  if (stored === TIMED_OUT) return { ai: 'unavailable (database timed out)', credential: 'unknown' }
  const credential = stored ? 'configured' : 'not configured'
  if (!deps.kek.ok) return { ai: `disabled (no key-encryption key: ${deps.kek.reason})`, credential }
  if (!stored) return { ai: 'disabled (no Claude credential)', credential }
  if (stored.kekId !== deps.kek.kek.id) {
    return { ai: 'unavailable (stored credential was sealed with a different key-encryption key)', credential }
  }
  if (stored.legacyFormat) {
    return { ai: 'unavailable (stored credential is in an outdated format; save it again)', credential }
  }
  return { ai: 'enabled', credential }
}

/**
 * GET /api/v1/ai/status: whether the assistant can be offered, for the UI's
 * gate (frontend src/agent/chat/availability.ts). `state` is the AiStatus
 * prefix; `available` also needs the chat socket to exist, and this request
 * to be one the socket's gate would let in; `reason` says why not, in words
 * for Settings. `chat` is that last verdict for the calling request (the
 * transport and origin half of routes/chat.ts's gate, as guard.ts
 * `uiReadProblem` judges a GET), so a page opened by LAN IP or over plain HTTP
 * is told so instead of offering a panel whose socket is refused. It carries
 * nothing /healthz does not, beyond what the caller sent.
 */
export type AiStatusView = {
  available: boolean
  state: 'enabled' | 'disabled' | 'unavailable'
  ai: AiStatus
  reason?: string
  /** Set when this request would be refused by the chat socket's gate. */
  chat?: 'refused'
}

/** The words the UI shows for an AiStatus other than `enabled`. */
export function statusReason(ai: AiStatus): string | undefined {
  if (ai === 'enabled') return undefined
  if (ai === 'disabled (no database)') return 'The agent service has no database (SCADBUDDY_DATABASE_URL is not set).'
  if (ai === 'disabled (no Claude credential)') return 'No Claude credential is configured yet.'
  if (ai.startsWith('disabled (no key-encryption key')) {
    return `The agent service has no key-encryption key, so it cannot store a Claude credential (${ai.slice('disabled (no key-encryption key: '.length, -1)}).`
  }
  const inner = ai.replace(/^unavailable \((.*)\)$/, '$1')
  return `The agent service is unavailable: ${inner}.`
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

  // Unguarded, like /healthz: it says only what /healthz says.
  app.get('/api/v1/ai/status', async (c) => {
    const dbOk = deps.database ? await deps.database.ping() : undefined
    const { ai } = await aiStatus(deps, dbOk)
    const chat = deps.sessions !== undefined && deps.upgradeWebSocket !== undefined
    const state = ai === 'enabled' ? 'enabled' : ai.startsWith('disabled') ? 'disabled' : 'unavailable'
    const refused = uiReadProblem(c, deps.origins, deps.remoteAddress, 'The assistant')
    const reason =
      statusReason(ai) ??
      (chat ? undefined : 'The agent service was started without its chat socket.') ??
      (refused ? `${refused}. Open ScadBuddy at its public HTTPS address to use it.` : undefined)
    const body: AiStatusView = {
      available: ai === 'enabled' && chat && !refused,
      state,
      ai,
      ...(reason ? { reason } : {}),
      ...(refused ? { chat: 'refused' as const } : {}),
    }
    c.header('Cache-Control', 'no-store')
    return c.json(body)
  })

  // Credential and plugin writes, refused attempts included, go in the audit
  // log (#258). Mounted before the routes so they run around them.
  if (deps.audit) {
    const audit = deps.audit
    // Refusals need no authentication, so they are coalesced per peer and
    // action rather than written one row per request (audit/writes.ts).
    const refusals = new RefusalCoalescer(audit)
    const writes = { audit, remoteAddress: deps.remoteAddress, refusals }
    app.use('/api/v1/ai/credentials', auditWrites({ ...writes, kind: 'credential', verb: credentialVerb }))
    // Refused or failed token writes; successful ones are recorded by the
    // token store itself (audit/writes.ts auditedTokenStore), with the token's id.
    app.use('/api/v1/ai/mcp-tokens/*', auditWrites({ ...writes, kind: 'token', verb: tokenVerb, failuresOnly: true }))
    // Hono's `/*` also matches the bare prefix, so this covers POST /api/v1/ai/plugins too.
    app.use('/api/v1/ai/plugins/*', auditWrites({ ...writes, kind: 'plugin', verb: pluginVerb }))
    // Refused or failed raises of a session's budget (#790); a raise that
    // lands is recorded by the manager (sessions/manager.ts raiseBudget).
    app.use('/api/v1/ai/sessions/*', auditWrites({ ...writes, kind: 'settings', verb: budgetVerb, failuresOnly: true }))
  }

  registerAuditRoutes(app, {
    audit: deps.audit,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  registerCredentialRoutes(app, {
    credentials: deps.credentials,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    kek: deps.kek,
    testConnection: deps.testConnection,
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
    ...(deps.resolveHost === undefined ? {} : { resolveHost: deps.resolveHost }),
    ...(deps.testCooldownMs === undefined ? {} : { testCooldownMs: deps.testCooldownMs }),
    ...(deps.now === undefined ? {} : { now: deps.now }),
  })

  if (deps.mcpOidc) {
    registerMcpAuthRoutes(app, {
      ...deps.mcpOidc,
      ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
    })
  }
  registerPluginRoutes(app, {
    plugins: deps.plugins,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    kek: deps.kek,
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
    testPlugin:
      deps.testPlugin ??
      ((plugin, address) =>
        deps.pluginForwarder
          ? testPlugin(plugin, address, deps.pluginForwarder)
          : Promise.resolve({
              ok: false,
              detail: 'the plugin forwarder is not running',
              duration_ms: 0,
              server: null,
              tools: [],
              truncated: false,
            })),
    ...(deps.resolveHost === undefined ? {} : { resolveHost: deps.resolveHost }),
  })

  registerMcpTokenRoutes(app, {
    tokens: deps.database ? deps.tokens : undefined,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    authSettings: deps.mcp?.authSettings ?? (() => DEFAULT_MCP_AUTH),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  registerMcpAuthModeRoutes(app, {
    settings: deps.database ? deps.aiSettings : undefined,
    authSettings: deps.mcp?.authSettings ?? (() => DEFAULT_MCP_AUTH),
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  registerPluginPackageRoutes(app, {
    packages: deps.pluginPackages,
    installer: deps.packageInstaller,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  registerHeadlessBrowserRoutes(app, {
    settings: deps.settings,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  registerSessionLimitsRoutes(app, {
    settings: deps.settings,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  registerApprovalRoutes(app, {
    approvals: deps.approvals,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  const ready = deps.database ? deps.database.ready : () => Promise.resolve(false)
  const shutdown = new AbortController()
  registerSessionRoutes(app, {
    sessions: deps.sessions,
    ready,
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
    shutdown: shutdown.signal,
  })
  registerChatRoute(app, {
    sessions: deps.sessions,
    ready,
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
    upgradeWebSocket: deps.upgradeWebSocket,
    ...(deps.chatSnapshotMs === undefined ? {} : { snapshotMs: deps.chatSnapshotMs }),
    ...(deps.tabs ? { tabs: deps.tabs } : {}),
  })
  if (deps.tabs) {
    registerBridgeRoute(app, {
      tabs: deps.tabs,
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      upgradeWebSocket: deps.upgradeWebSocket,
    })
  }

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
