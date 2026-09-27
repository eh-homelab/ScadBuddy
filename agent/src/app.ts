import { Hono } from 'hono'
import { type McpEndpointDeps, mountMcp } from './mcp/http.js'

// The HTTP surface. Hono per spec §4.5: web-standard Request/Response and
// direct streaming. /healthz, and /mcp when `mcp` is given (#251); the
// /api/v1/ai/* routes come later.

export type Probe = () => Promise<boolean>

export type AppDeps = {
  /** Undefined when SCADBUDDY_DATABASE_URL is unset. */
  database: { ping: Probe } | undefined
  backend: Probe
  /** The external MCP endpoint (src/mcp/http.ts). Left out, there is no /mcp route. */
  mcp?: McpEndpointDeps | undefined
}

export type Health = {
  status: 'ok'
  ai: 'enabled' | 'disabled (no database)' | 'unavailable (database unreachable)'
  database: 'ok' | 'unreachable' | 'not configured'
  backend: 'ok' | 'unreachable'
}

export function createApp(deps: AppDeps): Hono {
  const app = new Hono()

  // Liveness: always 200 while the process serves HTTP. A missing or
  // unreachable database or backend is REPORTED, not failed on, so a Postgres
  // blip does not restart the container; the backend's health report reads
  // `ai` to decide whether the UI shows AI (#261).
  app.get('/healthz', async (c) => {
    const [dbOk, backendOk] = await Promise.all([
      deps.database ? deps.database.ping() : Promise.resolve(undefined),
      deps.backend(),
    ])
    const body: Health = {
      status: 'ok',
      ai:
        dbOk === undefined
          ? 'disabled (no database)'
          : dbOk
            ? 'enabled'
            : 'unavailable (database unreachable)',
      database: dbOk === undefined ? 'not configured' : dbOk ? 'ok' : 'unreachable',
      backend: backendOk ? 'ok' : 'unreachable',
    }
    return c.json(body)
  })

  if (deps.mcp) mountMcp(app, deps.mcp)

  return app
}
