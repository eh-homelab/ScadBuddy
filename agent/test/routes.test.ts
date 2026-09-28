import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { OidcProvider } from '../src/auth/oidc.js'
import { ROUTES } from '../src/routes/index.js'
import { baseDeps } from './helpers/mcp.js'

// Every optional dependency given, so each route group registers all it can.
const deps = baseDeps({
  mcpOidc: { repo: undefined, provider: new OidcProvider(), publicUrl: undefined },
})

/** The method and path of every endpoint (not middleware) each route group registers. */
function endpoints(): { file: string; method: string; path: string }[] {
  return ROUTES.flatMap(({ file, route }) => {
    const app = new Hono()
    route.register(app, deps)
    return app.routes.filter((r) => r.method !== 'ALL').map((r) => ({ file, method: r.method, path: r.path }))
  })
}

function pattern(path: string): RegExp {
  const source = path
    .split('/')
    .map((part) => (part.startsWith(':') ? '[^/]+' : part === '*' ? '.*' : part.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
    .join('/')
  return new RegExp(`^${source}$`)
}

describe('route groups', () => {
  it('finds every routes/ file that exports `route`', () => {
    expect(ROUTES.map((r) => r.file)).toEqual(
      expect.arrayContaining(['approvals.ts', 'credentials.ts', 'mcpTokens.ts', 'plugins.ts']),
    )
    expect(ROUTES.map((r) => r.file)).not.toContain('guard.ts')
  })

  it('never has two groups answer the same request, so their order does not matter', () => {
    const all = endpoints()
    expect(all.length).toBeGreaterThan(0)
    for (const a of all) {
      // A concrete request for `a`: each parameter filled with a literal.
      const request = a.path.replace(/:[^/]+/g, 'x').replace(/\*/g, 'x')
      const owners = new Set(
        all.filter((b) => b.method === a.method && pattern(b.path).test(request)).map((b) => b.file),
      )
      expect([...owners], `${a.method} ${a.path}`).toEqual([a.file])
    }
  })
})
