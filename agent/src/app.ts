import { Hono } from 'hono'
import type { UpgradeWebSocket } from 'hono/ws'
import type { ApprovalService } from './approvals/service.js'
import type { Credential, CredentialRepo } from './credentials.js'
import type { ConnectionTest } from './harness/testConnection.js'
import type { Resolver } from './http/egress.js'
import type { OriginPolicy } from './http/origins.js'
import { type McpEndpointDeps, type McpHandle, mountMcp } from './mcp/http.js'
import type { PluginForwarder } from './plugins/forwarder.js'
import type { PluginRepo, RemotePlugin } from './plugins/registry.js'
import { type PluginTest, testPlugin } from './plugins/testConnection.js'
import { registerApprovalRoutes } from './routes/approvals.js'
import { registerChatRoute } from './routes/chat.js'
import { registerCredentialRoutes } from './routes/credentials.js'
import { registerPluginRoutes } from './routes/plugins.js'
import { registerSessionRoutes } from './routes/sessions.js'
import type { RemoteAddress } from './routes/guard.js'
import type { KekStatus } from './secrets.js'
import type { SessionManager } from './sessions/manager.js'

// The HTTP surface. Hono per spec §4.5: web-standard Request/Response and
// direct streaming. /healthz, /api/v1/ai/status (below), the Claude credential
// routes (#255, routes/credentials.ts), the approval routes (#258,
// routes/approvals.ts), the plugin registry routes (#297, routes/plugins.ts),
// the session routes and the assistant's chat socket (#300, #256,
// routes/sessions.ts, routes/chat.ts), and /mcp when `mcp` is given (#251,
// mcp/http.ts).
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
  testConnection: (credential: Credential) => Promise<ConnectionTest>
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
 * prefix; `available` also needs the chat socket to exist; `reason` says why
 * not, in words for Settings. It carries nothing /healthz does not.
 */
export type AiStatusView = {
  available: boolean
  state: 'enabled' | 'disabled' | 'unavailable'
  ai: AiStatus
  reason?: string
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

/** The app, plus `close()` for graceful shutdown: it ends every open `/mcp` session and its sweep. */
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
    const reason =
      statusReason(ai) ?? (chat ? undefined : 'The agent service was started without its chat socket.')
    const body: AiStatusView = { available: ai === 'enabled' && chat, state, ai, ...(reason ? { reason } : {}) }
    c.header('Cache-Control', 'no-store')
    return c.json(body)
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

  registerApprovalRoutes(app, {
    approvals: deps.approvals,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  const ready = deps.database ? deps.database.ready : () => Promise.resolve(false)
  registerSessionRoutes(app, {
    sessions: deps.sessions,
    ready,
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })
  registerChatRoute(app, {
    sessions: deps.sessions,
    ready,
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
    upgradeWebSocket: deps.upgradeWebSocket,
    ...(deps.chatSnapshotMs === undefined ? {} : { snapshotMs: deps.chatSnapshotMs }),
  })

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
      await mcp?.close()
    },
  })
}
