import { afterEach, describe, expect, it } from 'vitest'
import {
  checkIssuer,
  configuredAuthMethod,
  discover,
  OAuthError,
  parseBearerChallenge,
  resourceMatches,
  resourceMetadataUrls,
  serverMetadataUrls,
  TokenRefusedError,
  tokenRequest,
} from '../src/plugins/oauth.js'
import { type FakeAuthServer, startFakeAuthServer, startProtectedMcp } from './support/fakeOAuth.js'

// The protocol half of a plugin's OAuth sign-in (#780, src/plugins/oauth.ts), against a
// local fake authorization server and MCP server (test/support/fakeOAuth.ts).

describe('the Bearer challenge (RFC 6750 §3, RFC 9728 §5.1)', () => {
  it("reads GitHub's challenge", () => {
    // As https://api.githubcopilot.com/mcp/ answered a bare POST on 2026-11-01 (curl -I).
    const header =
      'Bearer error="invalid_request", error_description="No access token was provided in this request", ' +
      'resource_metadata="https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/"'
    expect(parseBearerChallenge(header)).toEqual({
      error: 'invalid_request',
      resourceMetadata: 'https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp/',
    })
  })

  it('finds the Bearer challenge among others, with scope, quoted or not', () => {
    expect(parseBearerChallenge('Basic realm="x", Bearer scope="files:read files:write", resource_metadata=https://m/x')).toEqual({
      scope: 'files:read files:write',
      resourceMetadata: 'https://m/x',
    })
    expect(parseBearerChallenge(['Negotiate abc==', 'bearer resource_metadata="https://m/\\"q"'])).toEqual({
      resourceMetadata: 'https://m/"q',
    })
    expect(parseBearerChallenge('Basic realm="x"')).toBeUndefined()
    expect(parseBearerChallenge(undefined)).toBeUndefined()
  })
})

describe('well-known URLs', () => {
  it('inserts the path, then tries the root (RFC 9728 §3.1, the MCP spec order)', () => {
    expect(resourceMetadataUrls('https://api.githubcopilot.com/mcp/')).toEqual([
      'https://api.githubcopilot.com/.well-known/oauth-protected-resource/mcp',
      'https://api.githubcopilot.com/.well-known/oauth-protected-resource',
    ])
    expect(resourceMetadataUrls('https://m.example/')).toEqual(['https://m.example/.well-known/oauth-protected-resource'])
  })

  it('tries RFC 8414 then OIDC, path-inserted then appended', () => {
    expect(serverMetadataUrls('https://github.com/login/oauth')).toEqual([
      'https://github.com/.well-known/oauth-authorization-server/login/oauth',
      'https://github.com/.well-known/openid-configuration/login/oauth',
      'https://github.com/login/oauth/.well-known/openid-configuration',
    ])
    expect(serverMetadataUrls('https://auth.example')).toEqual([
      'https://auth.example/.well-known/oauth-authorization-server',
      'https://auth.example/.well-known/openid-configuration',
    ])
  })

  it('matches the resource to the endpoint, a trailing slash aside', () => {
    expect(resourceMatches('https://api.githubcopilot.com/mcp', 'https://api.githubcopilot.com/mcp/')).toBe(true)
    expect(resourceMatches('https://api.githubcopilot.com/mcp', 'https://api.githubcopilot.com/mcp')).toBe(true)
    expect(resourceMatches('https://evil.example/mcp', 'https://api.githubcopilot.com/mcp/')).toBe(false)
    expect(resourceMatches('https://api.githubcopilot.com/', 'https://api.githubcopilot.com/mcp/')).toBe(false)
    expect(resourceMatches('https://api.githubcopilot.com/mcp?x=1', 'https://api.githubcopilot.com/mcp')).toBe(false)
  })
})

describe('iss (RFC 9207 §2.4, the MCP spec table)', () => {
  it('compares a present iss exactly, and refuses an absent one only when advertised', () => {
    expect(checkIssuer('https://a.example', true, 'https://a.example')).toBe(true)
    expect(checkIssuer('https://a.example', true, 'https://A.example')).toBe(false)
    expect(checkIssuer('https://a.example', true, undefined)).toBe(false)
    expect(checkIssuer('https://a.example', false, 'https://b.example')).toBe(false)
    expect(checkIssuer('https://a.example', false, undefined)).toBe(true)
  })
})

describe('discovery against a fake server', () => {
  let auth: FakeAuthServer | undefined
  let close: (() => Promise<void>) | undefined
  afterEach(async () => {
    await close?.()
    await auth?.close()
    auth = undefined
    close = undefined
  })

  it('follows the 401 to the metadata and the authorization server, scope from the challenge', async () => {
    auth = await startFakeAuthServer({ registration: true })
    const mcp = await startProtectedMcp(auth, { scope: 'mcp:tools', scopesSupported: ['a', 'b'] })
    close = mcp.close
    const found = await discover(mcp.url)
    expect(found.resource).toBe(`${new URL(mcp.url).origin}/mcp`)
    expect(found.server.issuer).toBe(auth.issuer)
    expect(found.server.token_endpoint).toBe(`${auth.issuer}/token`)
    expect(found.server.registration_endpoint).toBe(`${auth.issuer}/register`)
    expect(found.server.authorization_response_iss_parameter_supported).toBe(true)
    expect(found.scope).toBe('mcp:tools')
    // The probe went without a credential.
    expect(mcp.seen[0]).toBeUndefined()
  })

  it('falls back to scopes_supported, and finds a path issuer (GitHub-shaped)', async () => {
    auth = await startFakeAuthServer({ issuerPath: '/login/oauth' })
    const mcp = await startProtectedMcp(auth, { scopesSupported: ['repo', 'read:org'] })
    close = mcp.close
    const found = await discover(mcp.url)
    expect(found.server.issuer).toBe(auth.issuer)
    expect(found.scope).toBe('repo read:org')
  })

  it('refuses metadata for another resource, and a server without PKCE', async () => {
    auth = await startFakeAuthServer()
    const elsewhere = await startProtectedMcp(auth, { resource: () => 'https://other.example/mcp' })
    await expect(discover(elsewhere.url)).rejects.toThrow(/RFC 9728/)
    await elsewhere.close()
    await auth.close()
    auth = await startFakeAuthServer({ noPkce: true })
    const mcp = await startProtectedMcp(auth)
    close = mcp.close
    await expect(discover(mcp.url)).rejects.toThrow(/S256/)
  })

  it('refuses a cloud-metadata authorization server before fetching it', async () => {
    await expect(discover('http://169.254.169.254/mcp/')).rejects.toBeInstanceOf(OAuthError)
  })
})

describe('the token endpoint', () => {
  let auth: FakeAuthServer | undefined
  afterEach(async () => {
    await auth?.close()
    auth = undefined
  })

  it('takes an `error` member as a refusal whatever the status (GitHub answers 200)', async () => {
    auth = await startFakeAuthServer({ githubErrors: true, client: { id: 'app', secret: 's3cret' } })
    const endpoint = {
      tokenEndpoint: `${auth.issuer}/token`,
      resource: 'https://m.example/mcp',
      clientId: 'app',
      clientSecret: 's3cret',
      authMethod: 'client_secret_post' as const,
    }
    const err = await tokenRequest(endpoint, { grant_type: 'refresh_token', refresh_token: 'nope' }).catch((e: unknown) => e)
    expect(err).toBeInstanceOf(TokenRefusedError)
    expect((err as TokenRefusedError).code).toBe('bad_refresh_token')
    expect(auth.tokenRequests[0]?.get('resource')).toBe('https://m.example/mcp')
    expect(auth.tokenRequests[0]?.get('client_secret')).toBe('s3cret')
  })

  it('posts the client secret when the server lists no methods (GitHub), else uses what it lists', () => {
    const server = { issuer: 'https://a', authorization_endpoint: 'https://a/a', token_endpoint: 'https://a/t' }
    expect(configuredAuthMethod(server, undefined)).toBe('none')
    expect(configuredAuthMethod(server, 's')).toBe('client_secret_post')
    expect(configuredAuthMethod({ ...server, token_endpoint_auth_methods_supported: ['client_secret_basic'] }, 's')).toBe(
      'client_secret_basic',
    )
    expect(() => configuredAuthMethod({ ...server, token_endpoint_auth_methods_supported: ['private_key_jwt'] }, 's')).toThrow(
      OAuthError,
    )
  })
})
