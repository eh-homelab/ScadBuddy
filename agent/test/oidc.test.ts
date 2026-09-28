import { createServer } from 'node:http'
import type { AddressInfo } from 'node:net'
import { base64url, SignJWT } from 'jose'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import {
  defaultOidcConfig,
  DiscoveryError,
  discoveryUrls,
  type OidcConfig,
  OidcConfigSchema,
  OidcProvider,
  protectedResourceMetadata,
  SettingsOidcConfigRepo,
  tierFromClaims,
} from '../src/auth/oidc.js'
import { EgressError, egressGetJson } from '../src/http/egress.js'
import { type FakeIdp, newKey, startFakeIdp } from './support/fakeIdp.js'

// OIDC access-token validation for /mcp (#262, src/auth/oidc.ts) against a
// local fake IdP (test/support/fakeIdp.ts): keys generated per run, JWKS and
// discovery served on 127.0.0.1.

const AUD = 'https://scadbuddy.test/mcp'

let idp: FakeIdp
let config: OidcConfig
let provider: OidcProvider

beforeEach(async () => {
  idp = await startFakeIdp()
  config = { ...defaultOidcConfig(idp.issuer), enabled: true }
  provider = new OidcProvider()
})
afterEach(async () => {
  await idp.close()
})

const now = () => Math.floor(Date.now() / 1000)

describe('OidcProvider.verify: a good token', () => {
  it('maps a scadbuddy:read token to a read-only oidc principal named by its subject', async () => {
    const verdict = await provider.verify(await idp.sign({ azp: 'claude-code' }), config, AUD)
    expect(verdict).toEqual({
      ok: true,
      principal: { id: 'oidc:alice', kind: 'oidc', tiers: ['read'], subject: 'alice', clientId: 'claude-code' },
    })
  })

  it('grants the highest mapped tier and the ones below it, from scope, scp or the tier claim', async () => {
    const tiers = async (claims: Record<string, unknown>, c: OidcConfig = config) => {
      const v = await provider.verify(await idp.sign(claims), c, AUD)
      return v.ok ? v.principal.tiers : v.error
    }
    expect(await tiers({ scope: 'openid scadbuddy:write' })).toEqual(['read', 'write'])
    expect(await tiers({ scope: 'scadbuddy:outward' })).toEqual(['read', 'write', 'outward'])
    expect(await tiers({ scope: undefined, scp: ['scadbuddy:write'] })).toEqual(['read', 'write'])
    expect(await tiers({ scope: 'openid' })).toBe('insufficient_scope')
    const byGroup = { ...config, tier_claim: 'groups', scopes: { read: 'sb-read', write: 'sb-write', outward: 'sb-admin' } }
    expect(await tiers({ scope: 'openid', groups: ['staff', 'sb-admin'] }, byGroup)).toEqual(['read', 'write', 'outward'])
  })

  it('accepts an aud array that contains the resource', async () => {
    const verdict = await provider.verify(await idp.sign({ aud: ['account', AUD] }), config, AUD)
    expect(verdict.ok).toBe(true)
  })

  it('fetches metadata and JWKS once and reuses them', async () => {
    for (let i = 0; i < 3; i++) expect((await provider.verify(await idp.sign(), config, AUD)).ok).toBe(true)
    expect(idp.hits.metadata).toBe(1)
    expect(idp.hits.jwks).toBe(1)
  })
})

describe('OidcProvider.verify: refused tokens', () => {
  const refused = async (token: string, c: OidcConfig = config) => {
    const verdict = await provider.verify(token, c, AUD)
    expect(verdict.ok).toBe(false)
    return verdict as { ok: false; error: string; detail: string }
  }

  it('refuses a token from another issuer', async () => {
    const v = await refused(await idp.sign({ iss: 'https://evil.example' }))
    expect(v).toMatchObject({ error: 'invalid_token', detail: 'the token was issued by another issuer' })
  })

  it('refuses a token for another audience (RFC 8707), and one with no aud', async () => {
    expect(await refused(await idp.sign({ aud: 'https://other.example/mcp' }))).toMatchObject({
      error: 'invalid_token',
      detail: 'the token was issued for another audience (resource)',
    })
    expect((await refused(await idp.sign({}, { omit: ['aud'] }))).error).toBe('invalid_token')
  })

  it('checks the configured audience instead of the resource when one is set', async () => {
    const custom = { ...config, audience: 'scadbuddy' }
    expect((await provider.verify(await idp.sign({ aud: 'scadbuddy' }), custom, 'scadbuddy')).ok).toBe(true)
  })

  it('refuses an expired token, and one without exp', async () => {
    expect(await refused(await idp.sign({ iat: now() - 600, exp: now() - 120 }))).toMatchObject({
      error: 'invalid_token',
      detail: 'the token has expired',
    })
    expect((await refused(await idp.sign({}, { omit: ['exp'] }))).detail).toContain('"exp"')
  })

  it('refuses a token that is not valid yet, and one without sub', async () => {
    expect((await refused(await idp.sign({ nbf: now() + 600 }))).detail).toBe('the token is not valid yet')
    expect((await refused(await idp.sign({}, { omit: ['sub'] }))).detail).toContain('"sub"')
  })

  it('refuses an algorithm off the allowlist before fetching anything', async () => {
    const es384 = await newKey('ES384')
    idp.published.push(es384)
    const v = await refused(await idp.sign({}, { key: es384 }))
    expect(v).toMatchObject({ error: 'invalid_token', detail: 'the token is signed with ES384, which is not allowed' })
    expect(idp.hits.metadata + idp.hits.jwks).toBe(0)
    // Allowed, the same token verifies.
    expect((await provider.verify(await idp.sign({}, { key: es384 }), { ...config, algorithms: ['ES384'] }, AUD)).ok).toBe(
      true,
    )
  })

  it('refuses alg "none" and an HMAC token keyed with the public key (algorithm confusion)', async () => {
    const header = base64url.encode(JSON.stringify({ alg: 'none', typ: 'JWT' }))
    const body = base64url.encode(JSON.stringify({ iss: idp.issuer, sub: 'alice', aud: AUD, exp: now() + 60, scope: 'scadbuddy:outward' }))
    expect((await refused(`${header}.${body}.`)).error).toBe('invalid_token')
    expect((await refused(`${header}.${body}.x`)).detail).toContain('none')

    const secret = new TextEncoder().encode(JSON.stringify(idp.key.publicJwk))
    const hs = await new SignJWT({ scope: 'scadbuddy:outward' })
      .setProtectedHeader({ alg: 'HS256', kid: idp.key.kid })
      .setIssuer(idp.issuer)
      .setSubject('alice')
      .setAudience(AUD)
      .setExpirationTime('5m')
      .sign(secret)
    expect((await refused(hs)).detail).toBe('the token is signed with HS256, which is not allowed')
    // And no configuration can allow them.
    for (const alg of ['none', 'HS256']) {
      expect(OidcConfigSchema.safeParse({ ...config, algorithms: [alg] }).success).toBe(false)
    }
  })

  it('refuses a signature by a key the IdP does not publish, even under a published kid', async () => {
    const forger = await newKey('RS256', idp.key.kid)
    expect((await refused(await idp.sign({ scope: 'scadbuddy:outward' }, { key: forger }))).detail).toBe(
      'the token signature is invalid',
    )
  })

  it('refuses an ID token or other explicitly typed JWT', async () => {
    expect((await refused(await idp.sign({}, { typ: 'id_token+jwt' }))).detail).toContain('not an access token')
  })

  it('refuses a token with no mapped scope as insufficient_scope', async () => {
    const v = await refused(await idp.sign({ scope: 'openid profile' }))
    expect(v.error).toBe('insufficient_scope')
    expect(v.detail).toContain('scadbuddy:read')
  })

  it('reports an unreachable IdP as temporarily_unavailable, not as a bad token', async () => {
    const token = await idp.sign()
    await idp.close()
    const v = await refused(token)
    expect(v.error).toBe('temporarily_unavailable')
  })
})

describe('OidcProvider: key rotation and refetch limits', () => {
  it('refetches the JWKS once for an unknown kid, then waits out the cooldown', async () => {
    // The provider's clock is real time plus `skew`, so tokens stay valid while it moves on.
    let skew = 0
    const at = (offsetMs: number) => {
      skew = offsetMs
    }
    provider = new OidcProvider({ now: () => Date.now() + skew, refreshCooldownMs: 30_000 })
    const iat = now()
    const clock = { iat, exp: iat + 3600 }
    expect((await provider.verify(await idp.sign(clock), config, AUD)).ok).toBe(true)
    expect(idp.hits.jwks).toBe(1)

    // The IdP rotates; within the cooldown the new kid is not fetched for.
    const next = await newKey('RS256')
    idp.published = [next]
    const rotated = await idp.sign(clock, { key: next })
    at(1000)
    expect(await provider.verify(rotated, config, AUD)).toMatchObject({ ok: false, detail: 'no key of the issuer matches the token' })
    expect(idp.hits.jwks).toBe(1)
    // After it, one refetch finds the new key.
    at(31_000)
    expect((await provider.verify(rotated, config, AUD)).ok).toBe(true)
    expect(idp.hits.jwks).toBe(2)
    // A flood of unknown kids costs no further fetch within the cooldown.
    const stranger = await newKey('RS256')
    for (let i = 0; i < 5; i++) await provider.verify(await idp.sign(clock, { key: stranger }), config, AUD)
    expect(idp.hits.jwks).toBe(2)
  })
})

describe('discovery (the check Settings runs before OIDC can be enabled)', () => {
  it('finds the metadata and the keys, and says whether dynamic registration is offered', async () => {
    const report = await provider.test(idp.issuer)
    expect(report).toMatchObject({
      issuer: idp.issuer,
      jwks_uri: `${idp.issuer}/jwks`,
      source: `${idp.issuer}/.well-known/oauth-authorization-server`,
      keys: 1,
      key_algorithms: ['RS256'],
      dynamic_registration: true,
    })
  })

  it('refuses metadata that names another issuer (RFC 8414 §3.3)', async () => {
    idp.metadataIssuer = `${idp.issuer}/`
    await expect(provider.test(idp.issuer)).rejects.toThrow(DiscoveryError)
    await expect(provider.test(idp.issuer)).rejects.toThrow(/mind the trailing slash/)
  })

  it('refuses plain http off loopback, and a host that resolves to a metadata address', async () => {
    await expect(provider.test('http://idp.example')).rejects.toThrow(/must use https/)
    const rebinding = new OidcProvider({ resolve: async () => ['169.254.169.254'] })
    await expect(rebinding.test('https://idp.example')).rejects.toThrow(/link-local or cloud metadata/)
    await expect(rebinding.test('https://metadata.google.internal')).rejects.toThrow(/is a cloud metadata service/)
    await expect(egressGetJson('https://metadata.google.internal/x', { label: 'x' })).rejects.toThrow(EgressError)
  })

  it('probes the well-known URLs in the MCP authorization spec order', () => {
    expect(discoveryUrls('https://idp.example')).toEqual([
      'https://idp.example/.well-known/oauth-authorization-server',
      'https://idp.example/.well-known/openid-configuration',
    ])
    expect(discoveryUrls('https://idp.example/realms/home/')).toEqual([
      'https://idp.example/.well-known/oauth-authorization-server/realms/home',
      'https://idp.example/.well-known/openid-configuration/realms/home',
      'https://idp.example/realms/home/.well-known/openid-configuration',
    ])
  })
})

describe('egressGetJson (the IdP fetcher)', () => {
  it('does not follow redirects and caps the body', async () => {
    await expect(egressGetJson(`${idp.issuer}/authorize?response_type=code&code_challenge_method=S256&redirect_uri=http://127.0.0.1/cb`, { label: 'x' })).rejects.toThrow(
      /HTTP 302 \(redirects are not followed\)/,
    )
    await expect(egressGetJson(`${idp.issuer}/jwks`, { label: 'jwks_uri', maxBytes: 10 })).rejects.toThrow(/more than 10 bytes/)
  })

  it('connects only to the address that passed the check (DNS pinning)', async () => {
    // "idp.test" resolves to the fake IdP's loopback address; the request
    // goes there without any system DNS lookup of that name.
    const port = new URL(idp.issuer).port
    const got = await egressGetJson(`http://localhost:${port}/jwks`, { label: 'jwks_uri', resolve: async () => ['127.0.0.1'] })
    expect(got).toMatchObject({ keys: [{ kid: idp.key.kid }] })
  })

  it('gives up after the deadline', async () => {
    const slow = createServer(() => {})
    await new Promise<void>((r) => slow.listen(0, '127.0.0.1', r))
    try {
      const port = (slow.address() as AddressInfo).port
      await expect(egressGetJson(`http://127.0.0.1:${port}/`, { label: 'issuer', timeoutMs: 100 })).rejects.toThrow(/within 100 ms/)
    } finally {
      slow.closeAllConnections()
      await new Promise<void>((r) => slow.close(() => r()))
    }
  })
})

describe('configuration', () => {
  it('stores and reads back through ai_settings, and reads a malformed value as off', async () => {
    const rows = new Map<string, unknown>()
    const problems: string[] = []
    const repo = new SettingsOidcConfigRepo(
      { get: async <T>(k: string) => rows.get(k) as T | undefined, set: async (k, v) => void rows.set(k, v) },
      (d) => problems.push(d),
    )
    expect(await repo.get()).toBeUndefined()
    await repo.put(config)
    expect(await repo.get()).toEqual(config)
    rows.set('mcp_oidc', { enabled: true, issuer: 'not a url' })
    expect(await repo.get()).toBeUndefined()
    expect(problems[0]).toContain('OIDC is off')
  })

  it('refuses duplicate scopes and unknown fields', () => {
    expect(OidcConfigSchema.safeParse({ ...config, scopes: { read: 'a', write: 'a', outward: 'b' } }).success).toBe(false)
    expect(OidcConfigSchema.safeParse({ ...config, extra: 1 }).success).toBe(false)
  })

  it('describes the resource per RFC 9728', () => {
    expect(protectedResourceMetadata(config, 'https://scadbuddy.test/some/path')).toEqual({
      resource: 'https://scadbuddy.test/mcp',
      authorization_servers: [idp.issuer],
      scopes_supported: ['scadbuddy:read', 'scadbuddy:write', 'scadbuddy:outward'],
      bearer_methods_supported: ['header'],
      resource_signing_alg_values_supported: ['RS256', 'ES256'],
      resource_name: 'ScadBuddy MCP',
    })
  })

  it('maps scopes to tiers without a token', () => {
    expect(tierFromClaims({ scope: 'scadbuddy:read scadbuddy:write' }, config)).toBe('write')
    expect(tierFromClaims({ scp: 'scadbuddy:outward' }, config)).toBe('outward')
    expect(tierFromClaims({}, config)).toBeUndefined()
  })
})
