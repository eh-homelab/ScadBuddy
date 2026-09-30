import type { OAuthClientProvider } from '@modelcontextprotocol/sdk/client/auth.js'
import { UnauthorizedError } from '@modelcontextprotocol/sdk/client/auth.js'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StreamableHTTPClientTransport } from '@modelcontextprotocol/sdk/client/streamableHttp.js'
import type { OAuthClientInformationMixed, OAuthClientMetadata, OAuthTokens } from '@modelcontextprotocol/sdk/shared/auth.js'
import { decodeJwt } from 'jose'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { defaultOidcConfig, OidcProvider } from '../src/auth/oidc.js'
import { appFetch, firstText, testApp } from './helpers/mcp.js'
import { type FakeIdp, startFakeIdp } from './support/fakeIdp.js'

// The whole MCP authorization flow with the MCP SDK's own client and OAuth
// code (#262): the client calls /mcp with no token, gets 401 with
// `resource_metadata`, reads the Protected Resource Metadata (RFC 9728),
// discovers the fake IdP (RFC 8414), registers itself (RFC 7591), runs the
// authorization-code flow with PKCE and the `resource` indicator (RFC 8707),
// and calls a tool with the JWT it got. Nothing is stubbed on the ScadBuddy
// side: the app is the real one, reached in-process; the IdP is a real HTTP
// server on 127.0.0.1.

const PUBLIC_URL = 'https://scadbuddy.test'
const SERVER_URL = new URL(`${PUBLIC_URL}/mcp`)
const REDIRECT = 'http://127.0.0.1:1/callback'

/** A headless OAuth client: "redirecting" the user just records the URL. */
class RecordingProvider implements OAuthClientProvider {
  info: OAuthClientInformationMixed | undefined
  saved: OAuthTokens | undefined
  verifier = ''
  authorizationUrl: URL | undefined

  get redirectUrl(): string {
    return REDIRECT
  }
  get clientMetadata(): OAuthClientMetadata {
    return {
      client_name: 'scadbuddy-e2e',
      redirect_uris: [REDIRECT],
      grant_types: ['authorization_code'],
      response_types: ['code'],
      token_endpoint_auth_method: 'none',
    }
  }
  clientInformation() {
    return this.info
  }
  saveClientInformation(info: OAuthClientInformationMixed) {
    this.info = info
  }
  tokens() {
    return this.saved
  }
  saveTokens(tokens: OAuthTokens) {
    this.saved = tokens
  }
  redirectToAuthorization(url: URL) {
    this.authorizationUrl = url
  }
  saveCodeVerifier(verifier: string) {
    this.verifier = verifier
  }
  codeVerifier() {
    return this.verifier
  }
}

let idp: FakeIdp
beforeEach(async () => {
  idp = await startFakeIdp({ audience: SERVER_URL.href })
})
afterEach(async () => {
  await idp.close()
})

describe('MCP authorization end to end: discovery → 401 → login → token → call', () => {
  it('logs in through the IdP and calls a tool as the OIDC subject', async () => {
    const { app } = testApp({
      settings: { mode: 'oidc', oidc: { ...defaultOidcConfig(idp.issuer), enabled: true } },
      mcp: { oidc: new OidcProvider(), publicUrl: PUBLIC_URL },
    })
    // ScadBuddy in-process (from loopback, the development exception to HTTPS);
    // the IdP over the network.
    const inApp = appFetch(app)
    const seen: string[] = []
    const fetchFn: typeof fetch = (input, init) => {
      const url = new URL(input instanceof Request ? input.url : String(input))
      seen.push(`${init?.method ?? 'GET'} ${url.origin}${url.pathname}`)
      return url.origin === PUBLIC_URL ? inApp(input, init) : fetch(input, init)
    }
    const provider = new RecordingProvider()

    // 1. No token: the SDK meets the 401, discovers everything and "redirects".
    const first = new StreamableHTTPClientTransport(SERVER_URL, { authProvider: provider, fetch: fetchFn })
    await expect(new Client({ name: 'e2e', version: '0' }).connect(first)).rejects.toThrow(UnauthorizedError)
    expect(seen).toContain(`GET ${PUBLIC_URL}/.well-known/oauth-protected-resource`)
    expect(seen).toContain(`GET ${idp.issuer}/.well-known/oauth-authorization-server`)
    expect(idp.hits.register).toBe(1)
    const authorize = provider.authorizationUrl!
    expect(authorize.origin).toBe(idp.issuer)
    expect(authorize.searchParams.get('resource')).toBe(SERVER_URL.href)
    expect(authorize.searchParams.get('code_challenge_method')).toBe('S256')
    expect(authorize.searchParams.get('scope')?.split(' ')).toEqual(
      expect.arrayContaining(['scadbuddy:read', 'scadbuddy:write', 'scadbuddy:outward']),
    )

    // 2. The user consents (the fake IdP at once); the redirect carries the code.
    const consent = await fetch(authorize, { redirect: 'manual' })
    const code = new URL(consent.headers.get('location')!).searchParams.get('code')!
    await first.finishAuth(code)
    expect(idp.hits.token).toBe(1)
    const claims = decodeJwt(provider.saved!.access_token)
    expect(claims.aud).toBe(SERVER_URL.href)

    // 3. With the token, the session opens and a tool runs as oidc:<issuer>#alice.
    const client = new Client({ name: 'e2e', version: '0' })
    await client.connect(new StreamableHTTPClientTransport(SERVER_URL, { authProvider: provider, fetch: fetchFn }))
    try {
      expect((await client.listTools()).tools.length).toBeGreaterThan(0)
      expect(firstText(await client.callTool({ name: 'list_pending_actions', arguments: {} }))).toEqual({ items: [], next_cursor: null, total: 0 })
    } finally {
      await client.close()
    }
  })

  it('a token the IdP scoped to read only cannot run a write tool', async () => {
    idp.grantScope = 'scadbuddy:read'
    const { app } = testApp({
      settings: { mode: 'oidc', oidc: { ...defaultOidcConfig(idp.issuer), enabled: true } },
      mcp: { oidc: new OidcProvider(), publicUrl: PUBLIC_URL },
    })
    const inApp = appFetch(app)
    const fetchFn: typeof fetch = (input, init) =>
      new URL(input instanceof Request ? input.url : String(input)).origin === PUBLIC_URL ? inApp(input, init) : fetch(input, init)
    const provider = new RecordingProvider()
    const first = new StreamableHTTPClientTransport(SERVER_URL, { authProvider: provider, fetch: fetchFn })
    await expect(new Client({ name: 'e2e', version: '0' }).connect(first)).rejects.toThrow(UnauthorizedError)
    const consent = await fetch(provider.authorizationUrl!, { redirect: 'manual' })
    await first.finishAuth(new URL(consent.headers.get('location')!).searchParams.get('code')!)

    const client = new Client({ name: 'e2e', version: '0' })
    await client.connect(new StreamableHTTPClientTransport(SERVER_URL, { authProvider: provider, fetch: fetchFn }))
    try {
      const write = await client.callTool({ name: 'install_font', arguments: { family: 'Lobster Two' } })
      expect(write.isError).toBe(true)
      expect(firstText(write)).toContain('needs the "write" tier')
    } finally {
      await client.close()
    }
  })
})
