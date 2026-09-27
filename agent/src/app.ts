import { Hono } from 'hono'
import type { Credential, CredentialRepo } from './credentials.js'
import type { ConnectionTest } from './harness/testConnection.js'
import { type McpEndpointDeps, mountMcp } from './mcp/http.js'
import { registerCredentialRoutes } from './routes/credentials.js'
import type { RemoteAddress } from './routes/guard.js'
import type { KekStatus } from './secrets.js'

// The HTTP surface. Hono per spec §4.5: web-standard Request/Response and
// direct streaming. /healthz, the Claude credential routes (#255,
// routes/credentials.ts), and /mcp when `mcp` is given (#251, mcp/http.ts).

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
  /** The external MCP endpoint (src/mcp/http.ts). Left out, there is no /mcp route. */
  mcp?: McpEndpointDeps | undefined
}

/**
 * `ai` is `enabled` only when every prerequisite holds; otherwise it names the
 * first one missing, in the order an operator has to fix them. The backend's
 * health report reads it to decide whether the UI shows AI (#261), and the CI
 * smoke test asserts the no-database string, so keep those strings stable.
 */
export type AiStatus =
  | 'enabled'
  | 'disabled (no database)'
  | 'unavailable (database unreachable)'
  | 'unavailable (database migrations failed)'
  | `disabled (no key-encryption key: ${string})`
  | 'disabled (no Claude credential)'
  | 'unavailable (stored credential was sealed with a different key-encryption key)'

export type Health = {
  status: 'ok'
  ai: AiStatus
  database: 'ok' | 'unreachable' | 'not configured'
  backend: 'ok' | 'unreachable'
  secret_key: 'ok' | 'not configured'
  credential: 'configured' | 'not configured' | 'unknown'
}

async function aiStatus(deps: AppDeps, dbOk: boolean | undefined): Promise<Pick<Health, 'ai' | 'credential'>> {
  if (dbOk === undefined || !deps.database || !deps.credentials) {
    return { ai: 'disabled (no database)', credential: 'unknown' }
  }
  if (!dbOk) return { ai: 'unavailable (database unreachable)', credential: 'unknown' }
  if (!(await deps.database.ready())) {
    return { ai: 'unavailable (database migrations failed)', credential: 'unknown' }
  }
  let stored
  try {
    stored = await deps.credentials.get()
  } catch {
    return { ai: 'unavailable (database unreachable)', credential: 'unknown' }
  }
  const credential = stored ? 'configured' : 'not configured'
  if (!deps.kek.ok) return { ai: `disabled (no key-encryption key: ${deps.kek.reason})`, credential }
  if (!stored) return { ai: 'disabled (no Claude credential)', credential }
  if (stored.kekId !== deps.kek.kek.id) {
    return { ai: 'unavailable (stored credential was sealed with a different key-encryption key)', credential }
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
      mountMcp(app, deps.mcp)
    } else {
      // Spec §9, "No database": AI features are disabled. /mcp answers why
      // instead of 404, so an MCP client's error names the fix.
      app.all('/mcp', (c) =>
        c.json({ error: 'AI disabled: no database (SCADBUDDY_DATABASE_URL is not set)' }, 503),
      )
    }
  }

  return app
}
