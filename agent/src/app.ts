import { Hono } from 'hono'
import type { ApprovalService } from './approvals/service.js'
import type { Credential, CredentialRepo } from './credentials.js'
import type { ConnectionTest } from './harness/testConnection.js'
import type { Resolver } from './http/egress.js'
import type { OriginPolicy } from './http/origins.js'
import { registerApprovalRoutes } from './routes/approvals.js'
import { registerCredentialRoutes } from './routes/credentials.js'
import type { RemoteAddress } from './routes/guard.js'
import type { KekStatus } from './secrets.js'

// The HTTP surface. Hono per spec §4.5: web-standard Request/Response and
// direct streaming, which the later /mcp and /api/v1/ai/* routes need.
// /healthz, the Claude credential routes (#255, routes/credentials.ts) and the
// approval routes (#258, routes/approvals.ts).

export type Probe = () => Promise<boolean>

export type AppDeps = {
  /** Undefined when SCADBUDDY_DATABASE_URL is unset. */
  database: { ping: Probe; ready: Probe } | undefined
  backend: Probe
  /** The key-encryption key, or why there is none (secrets.ts `loadKek`). */
  kek: KekStatus
  /** Undefined exactly when `database` is. */
  credentials: CredentialRepo | undefined
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
}

export const DEFAULT_HEALTH_TIMEOUT_MS = 2000

/**
 * `ai` is `enabled` only when every prerequisite holds; otherwise it names the
 * first one missing, in the order an operator has to fix them. Today only the
 * CI smoke test reads it (.github/workflows/ci.yml asserts the no-database
 * string); nothing in the backend or frontend does yet. Keep the strings stable
 * for that test and for the UI gate #261 plans to build on them.
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

export function createApp(deps: AppDeps): Hono {
  const app = new Hono()

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

  registerApprovalRoutes(app, {
    approvals: deps.approvals,
    ready: deps.database ? deps.database.ready : () => Promise.resolve(false),
    remoteAddress: deps.remoteAddress,
    origins: deps.origins,
  })

  return app
}
