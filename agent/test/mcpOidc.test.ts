import type { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AuditContext } from '../src/audit/log.js'
import { UI_ACTOR } from '../src/audit/writes.js'
import { type McpAuthSettings, quoted } from '../src/auth/authenticate.js'
import { defaultOidcConfig, type OidcConfig, type OidcConfigRepo, OidcProvider } from '../src/auth/oidc.js'
import { appFetch, connect, firstText, INGRESS, MCP_URL, testApp } from './helpers/mcp.js'
import { type FakeIdp, startFakeIdp } from './support/fakeIdp.js'

// /mcp in `oidc` mode (#262) and its Settings routes, with a local fake IdP.
// The SDK's own OAuth flow (discovery → 401 → login → token → call) is
// test/oidc.e2e.test.ts.

const PUBLIC_URL = 'https://scadbuddy.test'
const METADATA_URL = `${PUBLIC_URL}/.well-known/oauth-protected-resource`

let idp: FakeIdp
let config: OidcConfig

beforeEach(async () => {
  idp = await startFakeIdp()
  config = { ...defaultOidcConfig(idp.issuer), enabled: true }
})
afterEach(async () => {
  await Promise.all(clients.splice(0).map((c) => c.close()))
  await idp.close()
})

const clients: Client[] = []
async function open(...args: Parameters<typeof connect>): Promise<Client> {
  const client = await connect(...args)
  clients.push(client)
  return client
}

function oidcApp(settings: Partial<McpAuthSettings> = {}, publicUrl: string | null = PUBLIC_URL) {
  return testApp({
    settings: { mode: 'oidc', oidc: config, ...settings },
    mcp: { oidc: new OidcProvider(), publicUrl: publicUrl ?? undefined },
  })
}

const post = (app: Parameters<typeof appFetch>[0], headers: Record<string, string> = {}) =>
  appFetch(app, { headers })(MCP_URL, { method: 'POST', body: '{}' })

describe('/mcp: oidc mode, discovery', () => {
  it('answers 401 with resource_metadata (RFC 9728 §5.1) and serves the metadata', async () => {
    const { app } = oidcApp()
    const res = await post(app)
    expect(res.status).toBe(401)
    expect(res.headers.get('www-authenticate')).toBe(`Bearer realm="scadbuddy", resource_metadata="${METADATA_URL}"`)

    for (const path of ['/.well-known/oauth-protected-resource', '/.well-known/oauth-protected-resource/mcp']) {
      const meta = await appFetch(app)(`http://scadbuddy.test${path}`)
      expect(meta.status).toBe(200)
      expect(await meta.json()).toMatchObject({
        resource: `${PUBLIC_URL}/mcp`,
        authorization_servers: [idp.issuer],
        scopes_supported: ['scadbuddy:read', 'scadbuddy:write', 'scadbuddy:outward'],
        bearer_methods_supported: ['header'],
      })
    }
  })

  it('serves no metadata and names none in bearer or disabled mode, or with OIDC switched off', async () => {
    for (const settings of [{ mode: 'bearer' as const }, { mode: 'disabled' as const }, { oidc: { ...config, enabled: false } }]) {
      const { app } = oidcApp(settings)
      expect((await appFetch(app)(`http://scadbuddy.test/.well-known/oauth-protected-resource`)).status).toBe(404)
      const res = await post(app)
      if (settings.mode !== 'disabled') expect(res.headers.get('www-authenticate')).not.toContain('resource_metadata')
    }
  })
})

describe('/mcp: oidc mode, tokens', () => {
  it('runs tools as the OIDC subject, within the scope tier', async () => {
    const { app } = oidcApp()
    const client = await open(app, { headers: { authorization: `Bearer ${await idp.sign({ scope: 'scadbuddy:read' })}` } })
    expect((await client.listTools()).tools.length).toBeGreaterThan(0)
    expect(firstText(await client.callTool({ name: 'list_pending_actions', arguments: {} }))).toEqual({ items: [], next_cursor: null, total: 0 })
    const write = await client.callTool({ name: 'install_font', arguments: { family: 'Lobster Two' } })
    expect(write.isError).toBe(true)
    expect(firstText(write)).toContain('needs the "write" tier')
  })

  it('keeps ScadBuddy bearer tokens working alongside OIDC', async () => {
    const t = oidcApp()
    const { token } = await t.tokens.mint({ name: 'ci', tier: 'read' })
    const client = await open(t.app, { headers: { authorization: `Bearer ${token}` } })
    expect((await client.listTools()).tools.length).toBeGreaterThan(0)
    const bad = await post(t.app, { authorization: 'Bearer sbmcp_nope' })
    expect(bad.status).toBe(401)
    expect(bad.headers.get('www-authenticate')).toContain('error="invalid_token"')
  })

  it('401s a token for another resource with invalid_token and a description (RFC 6750 §3.1)', async () => {
    const { app } = oidcApp()
    const res = await post(app, { authorization: `Bearer ${await idp.sign({ aud: 'https://elsewhere.example/mcp' })}` })
    expect(res.status).toBe(401)
    expect(res.headers.get('www-authenticate')).toBe(
      `Bearer realm="scadbuddy", resource_metadata="${METADATA_URL}", error="invalid_token", ` +
        `error_description="the token was issued for another audience (resource)"`,
    )
  })

  it('401s, not 500s, a forged header whose alg or typ would break WWW-Authenticate', async () => {
    const { app } = oidcApp()
    const body = Buffer.from('{}').toString('base64url')
    for (const header of [{ alg: 'RS256', typ: 1 }, { alg: 'RS\n256' }, { alg: 'RS256', typ: 'x\r\ny' }, { alg: 'RS256', typ: '\u2603' }]) {
      const token = `${Buffer.from(JSON.stringify(header)).toString('base64url')}.${body}.x`
      const res = await post(app, { authorization: `Bearer ${token}` })
      expect(res.status).toBe(401)
      expect(res.headers.get('www-authenticate')).toContain('error="invalid_token"')
    }
  })

  it('403s a token without a mapped scope as insufficient_scope, naming the scopes', async () => {
    const { app } = oidcApp()
    const res = await post(app, { authorization: `Bearer ${await idp.sign({ scope: 'openid' })}` })
    expect(res.status).toBe(403)
    expect(res.headers.get('www-authenticate')).toContain(
      'error="insufficient_scope", scope="scadbuddy:read scadbuddy:write scadbuddy:outward"',
    )
  })

  it('answers 503, not 401, while the IdP cannot be reached', async () => {
    const { app } = oidcApp()
    const token = await idp.sign()
    await idp.close()
    const res = await post(app, { authorization: `Bearer ${token}` })
    expect(res.status).toBe(503)
    expect(res.headers.get('retry-after')).toBe('30')
  })

  it('refuses JWTs without SCADBUDDY_PUBLIC_URL (no resource URI), but still takes bearer tokens', async () => {
    const t = oidcApp({}, null)
    const res = await post(t.app, { authorization: `Bearer ${await idp.sign()}` })
    expect(res.status).toBe(401)
    expect(await res.text()).toContain('SCADBUDDY_PUBLIC_URL')
    const { token } = await t.tokens.mint({ name: 'ci', tier: 'read' })
    await open(t.app, { headers: { authorization: `Bearer ${token}` } })
  })

  it('does not let another subject use a session', async () => {
    const { app } = oidcApp()
    const client = await open(app, { headers: { authorization: `Bearer ${await idp.sign({ sub: 'alice' })}` } })
    const id = (client.transport as { sessionId?: string }).sessionId!
    const res = await appFetch(app, {
      headers: {
        authorization: `Bearer ${await idp.sign({ sub: 'mallory' })}`,
        'mcp-session-id': id,
        'content-type': 'application/json',
        accept: 'application/json, text/event-stream',
      },
    })(MCP_URL, { method: 'POST', body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }) })
    expect(res.status).toBe(403)
  })
})

describe('quoted (a WWW-Authenticate parameter value)', () => {
  it('keeps only the RFC 6750 §3 error_description characters', () => {
    expect(quoted('the "token" is \\bad')).toBe(`"the 'token' is 'bad"`)
    expect(quoted('a\r\nb\u2603\u00e9')).toBe('"a??b??"')
    expect(() => new Headers({ 'www-authenticate': quoted('x\r\ny\u2603') })).not.toThrow()
  })
})

describe('/api/v1/ai/mcp/oidc (Settings)', () => {
  const UI = { host: 'scadbuddy.test', origin: PUBLIC_URL, 'x-forwarded-proto': 'https', 'content-type': 'application/json' }

  function settingsApp(publicUrl: string | null = PUBLIC_URL) {
    let stored: OidcConfig | undefined
    const contexts: AuditContext[] = []
    const repo: OidcConfigRepo = {
      get: async () => stored,
      put: async (c, context) => {
        stored = c
        contexts.push(context)
      },
    }
    const { app } = testApp({ deps: { mcpOidc: { repo, provider: new OidcProvider(), publicUrl: publicUrl ?? undefined } } })
    const call = (method: string, path = '', body?: unknown, headers: Record<string, string> = UI) =>
      appFetch(app, { address: INGRESS, headers })(`http://scadbuddy.test/api/v1/ai/mcp/oidc${path}`, {
        method,
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      })
    return { call, stored: () => stored, contexts }
  }

  it('answers its read only to the UI, like every sibling read (#989)', async () => {
    const { call } = settingsApp()
    const https = { 'x-forwarded-proto': 'https' }
    for (const headers of [
      { ...UI, origin: 'https://evil.example' },
      { ...https, host: 'scadbuddy.test', 'sec-fetch-site': 'cross-site' },
      // DNS rebinding: the attacker's name in Host, and no Origin on a same-origin GET.
      { ...https, host: 'evil.example' },
    ]) {
      const res = await call('GET', '', undefined, headers)
      expect(res.status, JSON.stringify(headers)).toBe(403)
      expect(JSON.stringify(await res.json())).not.toContain('scadbuddy:read')
    }
    // The UI's own same-origin GET sends no Origin.
    const own = { ...https, host: 'scadbuddy.test', 'sec-fetch-site': 'same-origin' }
    expect((await call('GET', '', undefined, own)).status).toBe(200)
  })

  it('shows the defaults, the resource and the metadata URL before anything is saved', async () => {
    const { call } = settingsApp()
    const res = await call('GET')
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({
      saved: false,
      config: { enabled: false, scopes: { read: 'scadbuddy:read' }, algorithms: ['RS256', 'ES256'] },
      resource: `${PUBLIC_URL}/mcp`,
      resource_metadata_url: METADATA_URL,
      can_enable: true,
    })
  })

  it('enables OIDC only after discovery against the issuer passes', async () => {
    const { call, stored } = settingsApp()
    const typo = await call('PUT', '', { ...config, issuer: `${idp.issuer}/typo` })
    expect(typo.status).toBe(400)
    expect(((await typo.json()) as { detail: string }).detail).toContain('nothing was saved')
    expect(stored()).toBeUndefined()

    const ok = await call('PUT', '', config)
    expect(ok.status).toBe(200)
    expect(await ok.json()).toMatchObject({ saved: true, config: { enabled: true }, discovery: { keys: 1, dynamic_registration: true } })
    expect(stored()).toEqual(config)
  })

  it('saves a disabled configuration without contacting the issuer', async () => {
    const { call, stored } = settingsApp()
    const res = await call('PUT', '', { ...config, enabled: false, issuer: 'https://idp.invalid/' })
    expect(res.status).toBe(200)
    expect(stored()?.issuer).toBe('https://idp.invalid/')
    expect(idp.hits.metadata).toBe(0)
  })

  it('audits the save as the browser user over HTTP, not as ScadBuddy (#831)', async () => {
    const { call, contexts } = settingsApp()
    expect((await call('PUT', '', { ...config, enabled: false })).status).toBe(200)
    expect(contexts).toEqual([{ actor: UI_ACTOR, surface: 'http', clientIp: INGRESS }])
  })

  it('refuses to enable without SCADBUDDY_PUBLIC_URL, and refuses a bad body', async () => {
    expect((await settingsApp(null).call('PUT', '', config)).status).toBe(409)
    const { call } = settingsApp()
    const bad = await call('PUT', '', { ...config, algorithms: ['none'] })
    expect(bad.status).toBe(400)
    expect(((await bad.json()) as { detail: string }).detail).toContain('algorithms')
  })

  it('tests an issuer without saving', async () => {
    const { call, stored } = settingsApp()
    const res = await call('POST', '/test', { issuer: idp.issuer })
    expect(res.status).toBe(200)
    expect(await res.json()).toMatchObject({ issuer: idp.issuer, jwks_uri: `${idp.issuer}/jwks` })
    expect(stored()).toBeUndefined()
    const bad = await call('POST', '/test', { issuer: 'http://idp.example' })
    expect(bad.status).toBe(400)
    expect(((await bad.json()) as { detail: string }).detail).toContain('must use https')
  })

  it('takes writes only from the UI origin over HTTPS (guard.ts)', async () => {
    const { call, stored } = settingsApp()
    const res = await call('PUT', '', config, { ...UI, origin: 'https://evil.example' })
    expect(res.status).toBe(403)
    expect(stored()).toBeUndefined()
  })
})
