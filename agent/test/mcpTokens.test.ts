import { describe, expect, it } from 'vitest'
import { type AgentApp, createApp } from '../src/app.js'
import { hashToken, TOKEN_PREFIX } from '../src/auth/tokens.js'
import type { McpTokenList, MintedMcpToken } from '../src/routes/mcpTokens.js'
import { appFetch, baseDeps, connect, INGRESS, LOOPBACK, testApp, UNTRUSTED } from './helpers/mcp.js'
import { InMemoryTokenStore } from './support/memoryTokens.js'

// /api/v1/ai/mcp-tokens (#251): Settings mints, lists and revokes MCP bearer tokens.

const URL_BASE = 'https://scadbuddy.test/api/v1/ai/mcp-tokens'
/** A request from the UI through the TLS ingress (a trusted proxy). */
const UI = { origin: 'https://scadbuddy.test', 'x-forwarded-proto': 'https' }

function ui(app: AgentApp, headers: Record<string, string> = UI, address = INGRESS) {
  return appFetch(app, { address, headers })
}

function mint(fetcher: typeof fetch, body: unknown) {
  return fetcher(URL_BASE, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  })
}

async function list(fetcher: typeof fetch): Promise<McpTokenList> {
  const res = await fetcher(URL_BASE)
  expect(res.status).toBe(200)
  return (await res.json()) as McpTokenList
}

function setup(options: Parameters<typeof testApp>[0] = {}) {
  const tokens = new InMemoryTokenStore()
  const made = testApp({ ...options, tokens, deps: { tokens, ...options.deps } })
  return { ...made, tokens, fetch: ui(made.app) }
}

describe('/api/v1/ai/mcp-tokens', () => {
  it('mints a token, returns the plaintext once, and lists metadata only', async () => {
    const { fetch, tokens } = setup()
    const res = await mint(fetch, { name: ' Claude Desktop ', tier: 'write' })
    expect(res.status).toBe(201)
    expect(res.headers.get('cache-control')).toBe('no-store')
    const minted = (await res.json()) as MintedMcpToken
    expect(minted.token.startsWith(TOKEN_PREFIX)).toBe(true)
    expect(minted.record).toMatchObject({
      name: 'Claude Desktop',
      tier: 'write',
      expires_at: null,
      last_used_at: null,
      revoked_at: null,
      status: 'active',
    })
    const listed = await list(fetch)
    expect(listed).toEqual({ auth_mode: 'bearer', tokens: [minted.record] })
    expect(await tokens.verify(minted.token)).not.toBeNull()
    const text = JSON.stringify(listed)
    expect(text).not.toContain(minted.token)
    expect(text).not.toContain(hashToken(minted.token))
    expect(Object.keys(listed.tokens[0]!).sort()).toEqual(
      ['created_at', 'expires_at', 'id', 'last_used_at', 'name', 'revoked_at', 'status', 'tier'].sort(),
    )
  })

  it('sets the expiry from expires_in, and reports expired tokens', async () => {
    const { fetch } = setup()
    const before = Date.now()
    const minted = (await (await mint(fetch, { name: 'short', tier: 'read', expires_in: 3600 })).json()) as MintedMcpToken
    const expires = Date.parse(minted.record.expires_at!)
    expect(expires).toBeGreaterThanOrEqual(before + 3600_000)
    expect(expires).toBeLessThanOrEqual(Date.now() + 3600_000)

    const { app, tokens } = setup()
    await tokens.mint({ name: 'old', tier: 'read', expiresAt: new Date(Date.now() - 1000) })
    const [old] = (await list(ui(app))).tokens
    expect(old?.status).toBe('expired')
  })

  it('lists newest first, with last use', async () => {
    const { fetch, tokens } = setup()
    const a = (await (await mint(fetch, { name: 'a', tier: 'read' })).json()) as MintedMcpToken
    await new Promise((r) => setTimeout(r, 5))
    await mint(fetch, { name: 'b', tier: 'outward' })
    await tokens.verify(a.token)
    const listed = (await list(fetch)).tokens
    expect(listed.map((t) => t.name)).toEqual(['b', 'a'])
    expect(listed[1]?.last_used_at).not.toBeNull()
  })

  it('refuses a bad body', async () => {
    const { fetch } = setup()
    for (const body of [
      {},
      { name: '', tier: 'read' },
      { name: '   ', tier: 'read' },
      { name: 'x'.repeat(101), tier: 'read' },
      { name: 'a\nb', tier: 'read' },
      { name: 'x', tier: 'admin' },
      { name: 'x', tier: 'read', expires_in: 0 },
      { name: 'x', tier: 'read', expires_in: 1.5 },
      { name: 'x', tier: 'read', expires_in: 11 * 366 * 86400 },
      { name: 'x', tier: 'read', extra: true },
    ]) {
      const res = await mint(fetch, body)
      expect(res.status, JSON.stringify(body)).toBe(400)
    }
    const res = await fetch(URL_BASE, { method: 'POST', headers: { 'content-type': 'application/json' }, body: '{' })
    expect(await res.json()).toEqual({ detail: 'body is not valid JSON' })
    expect((await list(fetch)).tokens).toEqual([])
  })

  it('refuses a body that is not JSON (a cross-origin form)', async () => {
    const { fetch } = setup()
    const res = await fetch(URL_BASE, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: 'name=x&tier=outward',
    })
    expect(res.status).toBe(415)
  })

  it('revokes: the token stays listed as revoked, and /mcp refuses it', async () => {
    const { app, fetch } = setup()
    const minted = (await (await mint(fetch, { name: 'r', tier: 'read' })).json()) as MintedMcpToken
    const client = await connect(app, { headers: { authorization: `Bearer ${minted.token}` } })
    await client.close()

    const res = await fetch(`${URL_BASE}/${minted.record.id}`, { method: 'DELETE' })
    expect(res.status).toBe(204)
    const [listed] = (await list(fetch)).tokens
    expect(listed).toMatchObject({ id: minted.record.id, status: 'revoked' })
    expect(listed?.revoked_at).not.toBeNull()
    await expect(connect(app, { headers: { authorization: `Bearer ${minted.token}` } })).rejects.toThrow(
      /unknown, expired or revoked/,
    )

    // Again, or an id that was never minted: 404.
    expect((await fetch(`${URL_BASE}/${minted.record.id}`, { method: 'DELETE' })).status).toBe(404)
    expect((await fetch(`${URL_BASE}/nope`, { method: 'DELETE' })).status).toBe(404)
  })

  it('reports the auth mode, and manages tokens in every mode (spec §8.3)', async () => {
    for (const mode of ['bearer', 'disabled', 'oidc'] as const) {
      const { fetch } = setup({ settings: { mode } })
      expect((await mint(fetch, { name: mode, tier: 'read' })).status).toBe(201)
      expect((await list(fetch)).auth_mode).toBe(mode)
    }
    const { fetch } = setup({
      authSettings: () => {
        throw new Error('settings unreadable')
      },
    })
    expect((await list(fetch)).auth_mode).toBeNull()
  })

  it('answers 503 without a database, or before migrations apply', async () => {
    const none = testApp({ deps: { database: undefined, credentials: undefined, tokens: undefined } })
    const res = await ui(none.app)(URL_BASE)
    expect(res.status).toBe(503)
    expect(((await res.json()) as { detail: string }).detail).toMatch(/SCADBUDDY_DATABASE_URL/)
    // A store handed in without a database is not used.
    const orphan = testApp({ deps: { database: undefined, tokens: new InMemoryTokenStore() } })
    expect((await mint(ui(orphan.app), { name: 'x', tier: 'read' })).status).toBe(503)

    const tokens = new InMemoryTokenStore()
    const unmigrated = testApp({ deps: { tokens, database: { ping: async () => true, ready: async () => false } } })
    expect((await ui(unmigrated.app)(URL_BASE)).status).toBe(503)
    expect((await mint(ui(unmigrated.app), { name: 'x', tier: 'read' })).status).toBe(503)
    expect(await tokens.list()).toEqual([])
  })

  it('answers 503 when the app is built without a token store', async () => {
    // baseDeps() has no `tokens`: the store is opt-in.
    const app = createApp(baseDeps())
    expect((await ui(app)(URL_BASE)).status).toBe(503)
  })

  describe('writes pass the UI write guard (guard.ts uiRequestProblem)', () => {
    const cases: [string, Record<string, string>, string][] = [
      ['no Origin', { 'x-forwarded-proto': 'https' }, INGRESS],
      ['another Origin', { ...UI, origin: 'https://evil.example' }, INGRESS],
      ['plain HTTP through the proxy', { origin: 'http://scadbuddy.test', 'x-forwarded-proto': 'http' }, INGRESS],
      ['forwarded headers from an untrusted peer', UI, UNTRUSTED],
    ]
    for (const [label, headers, address] of cases) {
      it(`refuses POST and DELETE with ${label}`, async () => {
        const { app, tokens } = setup()
        const record = (await tokens.mint({ name: 'kept', tier: 'read' })).record
        const fetcher = ui(app, headers, address)
        const post = await mint(fetcher, { name: 'x', tier: 'outward' })
        expect(post.status).toBe(403)
        expect(((await post.json()) as { detail: string }).detail).toMatch(/^MCP token changes must/)
        expect((await fetcher(`${URL_BASE}/${record.id}`, { method: 'DELETE' })).status).toBe(403)
        expect((await tokens.list()).map((t) => [t.name, t.revokedAt])).toEqual([['kept', undefined]])
      })
    }

    it('accepts loopback without the ingress (local development)', async () => {
      const { app } = setup()
      const fetcher = appFetch(app, { address: LOOPBACK, headers: { origin: 'http://localhost:8081' } })
      const res = await fetcher('http://localhost:8081/api/v1/ai/mcp-tokens', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ name: 'dev', tier: 'read' }),
      })
      expect(res.status).toBe(201)
    })
  })

  describe('reads pass the UI read guard (guard.ts uiReadProblem)', () => {
    it('accepts a same-origin GET with no Origin header', async () => {
      const { app } = setup()
      const res = await ui(app, { 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' })(URL_BASE)
      expect(res.status).toBe(200)
    })

    const cases: [string, Record<string, string>, string][] = [
      ['a cross-site fetch', { 'x-forwarded-proto': 'https', 'sec-fetch-site': 'cross-site' }, INGRESS],
      ['another Origin', { ...UI, origin: 'https://evil.example' }, INGRESS],
      ['plain HTTP', { 'x-forwarded-proto': 'http' }, INGRESS],
      ['forwarded headers from an untrusted peer', UI, UNTRUSTED],
    ]
    for (const [label, headers, address] of cases) {
      it(`refuses GET with ${label}`, async () => {
        const { app } = setup()
        const res = await ui(app, headers, address)(URL_BASE)
        expect(res.status).toBe(403)
        expect(((await res.json()) as { detail: string }).detail).toMatch(/^MCP token reads must/)
      })
    }

    it('refuses a rebound Host (DNS rebinding)', async () => {
      const { app } = setup()
      const res = await appFetch(app, { address: INGRESS, headers: { 'x-forwarded-proto': 'https' } })(
        'https://attacker.example/api/v1/ai/mcp-tokens',
      )
      expect(res.status).toBe(403)
    })
  })
})
