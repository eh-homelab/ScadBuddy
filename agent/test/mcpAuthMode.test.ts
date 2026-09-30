import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import type { AuditContext } from '../src/audit/log.js'
import { UI_ACTOR } from '../src/audit/writes.js'
import { mcpAuthSettings, SETTING_MCP_ANONYMOUS_CAP, SETTING_MCP_AUTH_MODE } from '../src/auth/authenticate.js'
import { defaultOidcConfig, type OidcConfigRepo } from '../src/auth/oidc.js'
import { type McpAuthView, registerMcpAuthModeRoutes } from '../src/routes/mcpAuthMode.js'
import { appFetch, baseDeps, connect, INGRESS, LOOPBACK, testApp, UNTRUSTED } from './helpers/mcp.js'

// /api/v1/ai/mcp/auth (#251, spec §8.3): Settings reads and changes the /mcp
// auth mode and the anonymous cap (#526's ai_settings keys), and /mcp applies
// them on its next request.

const URL_BASE = 'https://scadbuddy.test/api/v1/ai/mcp/auth'
/** A request from the UI through the TLS ingress (a trusted proxy). */
const UI = { origin: 'https://scadbuddy.test', 'x-forwarded-proto': 'https' }

/** `ai_settings` in memory: what credentials.ts `SettingsStore` offers. */
function memorySettings(initial: Record<string, unknown> = {}) {
  const map = new Map<string, unknown>(Object.entries(initial))
  const contexts: AuditContext[] = []
  return {
    map,
    contexts,
    get: async <T>(key: string) => map.get(key) as T | undefined,
    setMany: async (
      values: Record<string, unknown>,
      check: (current: { get<T>(key: string): Promise<T | undefined> }) => Promise<boolean>,
      context: AuditContext,
    ) => {
      if (!(await check({ get: async <T>(key: string) => map.get(key) as T | undefined }))) return false
      contexts.push(context)
      for (const [key, value] of Object.entries(values)) map.set(key, value)
      return true
    },
  }
}

/** An OIDC configuration that is enabled (#262): /mcp's mode is then `oidc`. */
const OIDC_ON: OidcConfigRepo = {
  get: async () => ({ ...defaultOidcConfig(), enabled: true }),
  put: async () => {},
}

function setup(store = memorySettings(), options: Parameters<typeof testApp>[0] = {}, oidc?: OidcConfigRepo) {
  const made = testApp({
    ...options,
    authSettings: mcpAuthSettings(store, () => {}, oidc),
    deps: { aiSettings: store, ...options.deps },
  })
  return { ...made, store, fetch: appFetch(made.app, { address: INGRESS, headers: UI }) }
}

/** The stored values as the page showed them, which a PUT sends as `expected`. */
const DEFAULTS = { mode: 'bearer', anonymous_cap: 'outward' } as const

function put(fetcher: typeof fetch, body: unknown, headers: Record<string, string> = {}) {
  return fetcher(URL_BASE, {
    method: 'PUT',
    headers: { 'content-type': 'application/json', ...headers },
    body: typeof body === 'string' ? body : JSON.stringify(body),
  })
}

async function detail(res: Response): Promise<string> {
  return ((await res.json()) as { detail: string }).detail
}

describe('/api/v1/ai/mcp/auth', () => {
  it('reports the defaults until something is saved', async () => {
    const { fetch } = setup()
    const res = await fetch(URL_BASE)
    expect(res.status).toBe(200)
    expect(res.headers.get('cache-control')).toBe('no-store')
    expect(await res.json()).toEqual({
      mode: 'bearer',
      configured_mode: 'bearer',
      anonymous_cap: 'outward',
    } satisfies McpAuthView)
  })

  it('reports what /mcp applies: a stored oidc without OIDC is bearer, an unknown value fails closed', async () => {
    const oidc = setup(memorySettings({ [SETTING_MCP_AUTH_MODE]: 'oidc' }))
    expect(await (await oidc.fetch(URL_BASE)).json()).toEqual({
      mode: 'bearer',
      configured_mode: 'bearer',
      anonymous_cap: 'outward',
    })
    const unknown = setup(memorySettings({ [SETTING_MCP_AUTH_MODE]: 'open', [SETTING_MCP_ANONYMOUS_CAP]: 'all' }))
    expect(await (await unknown.fetch(URL_BASE)).json()).toEqual({
      mode: 'bearer',
      configured_mode: 'bearer',
      anonymous_cap: 'read',
    })
  })

  it('reports OIDC as the mode while it is enabled, with the stored mode it overrides', async () => {
    const { app, fetch, store } = setup(memorySettings({ [SETTING_MCP_AUTH_MODE]: 'disabled' }), {}, OIDC_ON)
    expect(await (await fetch(URL_BASE)).json()).toEqual({
      mode: 'oidc',
      configured_mode: 'disabled',
      anonymous_cap: 'outward',
    })

    // Saving `disabled` stores it, but /mcp keeps requiring authentication.
    const res = await put(fetch, {
      mode: 'disabled',
      anonymous_cap: 'read',
      expected: { mode: 'disabled', anonymous_cap: 'outward' },
    })
    expect(res.status).toBe(200)
    expect(await res.json()).toEqual({ mode: 'oidc', configured_mode: 'disabled', anonymous_cap: 'read' })
    expect(store.map.get(SETTING_MCP_AUTH_MODE)).toBe('disabled')
    await expect(connect(app)).rejects.toThrow()
  })

  it('audits the save as the browser user over HTTP, not as ScadBuddy (#831)', async () => {
    const { fetch, store } = setup()
    expect((await put(fetch, { mode: 'disabled', anonymous_cap: 'read', expected: DEFAULTS })).status).toBe(200)
    expect(store.contexts).toEqual([{ actor: UI_ACTOR, surface: 'http', clientIp: INGRESS }])
  })

  it('saves both keys, and /mcp applies them on the next request', async () => {
    const { app, fetch, store } = setup()
    await expect(connect(app)).rejects.toThrow(/bearer token is required/)

    const res = await put(fetch, { mode: 'disabled', anonymous_cap: 'read', expected: DEFAULTS })
    expect(res.status).toBe(200)
    const off = { mode: 'disabled', configured_mode: 'disabled', anonymous_cap: 'read' }
    expect(await res.json()).toEqual(off)
    expect(Object.fromEntries(store.map)).toEqual({ mcp_auth_mode: 'disabled', mcp_anonymous_cap: 'read' })
    expect(await (await fetch(URL_BASE)).json()).toEqual(off)

    // disabled: an anonymous session, held to the cap.
    const client = await connect(app)
    const write = await client.callTool({ name: 'install_font', arguments: { family: 'Lobster Two' } })
    expect(write.isError).toBe(true)
    expect(JSON.stringify(write.content)).toContain('needs the \\"write\\" tier')

    // Back to bearer: the anonymous session is refused from its next request.
    const back = { mode: 'bearer', anonymous_cap: 'outward', expected: { mode: 'disabled', anonymous_cap: 'read' } }
    expect((await put(fetch, back)).status).toBe(200)
    await expect(client.listTools()).rejects.toThrow()
    await client.close().catch(() => {})
    await expect(connect(app)).rejects.toThrow(/bearer token is required/)
  })

  it('answers 409 and saves nothing when the stored setting is not what the page showed', async () => {
    // A stale tab: it loaded `disabled`/`write`, and auth has been turned back on since.
    const { fetch, store } = setup(memorySettings({ [SETTING_MCP_AUTH_MODE]: 'bearer' }))
    const res = await put(fetch, {
      mode: 'disabled',
      anonymous_cap: 'read',
      expected: { mode: 'disabled', anonymous_cap: 'write' },
    })
    expect(res.status).toBe(409)
    expect(await detail(res)).toMatch(/changed since this page loaded it/)
    expect(Object.fromEntries(store.map)).toEqual({ mcp_auth_mode: 'bearer' })
  })

  function loggedRoute(authSettings?: () => Promise<never>) {
    const lines: string[] = []
    const store = memorySettings()
    const deps = baseDeps()
    const hono = new Hono()
    registerMcpAuthModeRoutes(hono, {
      settings: store,
      authSettings: authSettings ?? mcpAuthSettings(store, () => {}),
      ready: async () => true,
      remoteAddress: deps.remoteAddress,
      origins: deps.origins,
      log: (line) => lines.push(line),
    })
    return { hono, lines, store }
  }

  it('logs each change with the client the ingress names, and not a save that changes nothing', async () => {
    const { hono, lines } = loggedRoute()
    const fetch = appFetch(hono, {
      address: INGRESS,
      headers: { ...UI, 'x-forwarded-for': '198.51.100.1, 203.0.113.9' },
    })
    const body = { mode: 'disabled', anonymous_cap: 'outward', expected: DEFAULTS }
    expect((await put(fetch, body)).status).toBe(200)
    expect((await put(fetch, { ...body, expected: { mode: 'disabled', anonymous_cap: 'outward' } })).status).toBe(200)
    expect(lines).toEqual([
      `mcp auth: mcp_auth_mode set to disabled, anonymous cap outward (was bearer, outward; from 203.0.113.9 via ${INGRESS})`,
    ])

    // From loopback no proxy is involved: X-Forwarded-For is not believed.
    const local = loggedRoute()
    const direct = appFetch(local.hono, {
      address: LOOPBACK,
      headers: { origin: 'http://localhost:8081', 'x-forwarded-for': '203.0.113.9' },
    })
    const res = await direct('http://localhost:8081/api/v1/ai/mcp/auth', {
      method: 'PUT',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    })
    expect(res.status).toBe(200)
    expect(local.lines).toEqual([
      `mcp auth: mcp_auth_mode set to disabled, anonymous cap outward (was bearer, outward; from ${LOOPBACK})`,
    ])
  })

  it('logs a committed change even when the settings cannot be read back', async () => {
    const { hono, lines, store } = loggedRoute(async () => {
      throw new Error('database blip')
    })
    const fetch = appFetch(hono, { address: INGRESS, headers: UI })
    const res = await put(fetch, { mode: 'disabled', anonymous_cap: 'read', expected: DEFAULTS })
    expect(res.status).toBe(503)
    expect(await detail(res)).toMatch(/^saved, but/)
    expect(store.map.get(SETTING_MCP_AUTH_MODE)).toBe('disabled')
    expect(lines).toEqual([
      `mcp auth: mcp_auth_mode set to disabled, anonymous cap read (was bearer, outward; from ${INGRESS})`,
    ])
  })

  it('refuses a body that is not a mode it sets, and saves nothing', async () => {
    const { fetch, store } = setup()
    for (const [body, pattern] of [
      [{ mode: 'oidc', anonymous_cap: 'outward', expected: DEFAULTS }, /#262/],
      [{ mode: 'open', anonymous_cap: 'outward', expected: DEFAULTS }, /^mode:/],
      [{ mode: 'disabled', anonymous_cap: 'admin', expected: DEFAULTS }, /^anonymous_cap:/],
      [{ mode: 'disabled', expected: DEFAULTS }, /^anonymous_cap:/],
      [{ mode: 'disabled', anonymous_cap: 'read' }, /^expected:/],
      [{ mode: 'disabled', anonymous_cap: 'read', expected: { mode: 'oidc', anonymous_cap: 'read' } }, /^expected\.mode:/],
      [{ mode: 'disabled', anonymous_cap: 'read', expected: DEFAULTS, extra: 1 }, /extra/],
      ['{not json', /not valid JSON/],
      ['null', /body/],
    ] as const) {
      const res = await put(fetch, body)
      expect(res.status, JSON.stringify(body)).toBe(400)
      expect(await detail(res)).toMatch(pattern)
    }
    expect(store.map.size).toBe(0)
  })

  it('guards writes as settings writes and reads as UI reads', async () => {
    const { app, store } = setup()
    const body = { mode: 'disabled', anonymous_cap: 'outward', expected: DEFAULTS }
    // Another origin, no origin, a LAN host claiming HTTPS, and not JSON: all refused.
    const cases: [typeof fetch, Record<string, string>][] = [
      [appFetch(app, { address: INGRESS, headers: { ...UI, origin: 'https://evil.test' } }), {}],
      [appFetch(app, { address: INGRESS, headers: { 'x-forwarded-proto': 'https' } }), {}],
      [appFetch(app, { address: UNTRUSTED, headers: UI }), {}],
      [appFetch(app, { address: INGRESS, headers: UI }), { 'content-type': 'text/plain' }],
    ]
    for (const [fetcher, headers] of cases) {
      const res = await put(fetcher, body, headers)
      expect(res.status).toBe(403)
      expect(await detail(res)).toMatch(/MCP auth changes|application\/json/)
    }
    expect(store.map.size).toBe(0)

    const crossSite = appFetch(app, {
      address: INGRESS,
      headers: { 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' },
    })
    expect((await crossSite(URL_BASE)).status).toBe(403)
    // The local-development exception: loopback, no proxy.
    expect((await appFetch(app, { address: LOOPBACK })('http://localhost:8081/api/v1/ai/mcp/auth')).status).toBe(200)
  })

  it('answers 503 without a database, before migrations, and when the settings cannot be read', async () => {
    const none = setup(undefined, { deps: { database: undefined, credentials: undefined } })
    const res = await none.fetch(URL_BASE)
    expect(res.status).toBe(503)
    expect(await detail(res)).toContain('SCADBUDDY_DATABASE_URL')

    const notReady = setup(undefined, { deps: { database: { ping: async () => true, ready: async () => false } } })
    expect((await notReady.fetch(URL_BASE)).status).toBe(503)
    expect((await put(notReady.fetch, { mode: 'disabled', anonymous_cap: 'outward', expected: DEFAULTS })).status).toBe(
      503,
    )
    expect(notReady.store.map.size).toBe(0)

    const store = memorySettings()
    store.get = async () => {
      throw new Error('database blip')
    }
    const broken = setup(store)
    const unreadable = await broken.fetch(URL_BASE)
    expect(unreadable.status).toBe(503)
    expect(await detail(unreadable)).toContain('cannot be read')
    // /mcp fails closed meanwhile (mcp/http.ts).
    await expect(connect(broken.app)).rejects.toThrow(/bearer token is required/)
  })
})
