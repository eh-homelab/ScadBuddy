import { describe, expect, it } from 'vitest'
import {
  checkOrigin,
  effectiveRequest,
  isLoopbackPeer,
  isSecureTransport,
  normaliseOrigin,
  OriginConfigError,
  originPolicy,
  parseCidrList,
  type RequestFacts,
  requestOrigin,
} from '../src/http/origins.js'

function req(peer: string | undefined, headers: Record<string, string>): RequestFacts {
  return { peer, header: (name) => headers[name.toLowerCase()] }
}

describe('origin normalisation', () => {
  it.each([
    ['https://X.example:443/path?q', 'https://x.example'],
    ['http://x.example:80', 'http://x.example'],
    ['https://x.example:8443', 'https://x.example:8443'],
    ['http://[::1]:8081', 'http://[::1]:8081'],
  ])('%s → %s', (raw, origin) => {
    expect(normaliseOrigin(raw)).toBe(origin)
  })

  it.each(['null', '', 'file:///etc/passwd', 'chrome-extension://abc'])('refuses %j', (raw) => {
    expect(normaliseOrigin(raw)).toBeUndefined()
  })

  it('drops the default port from Host the same way', () => {
    expect(requestOrigin('https', 'scadbuddy.example:443')).toBe('https://scadbuddy.example')
    expect(requestOrigin('http', 'scadbuddy.example:80')).toBe('http://scadbuddy.example')
    expect(requestOrigin('https', 'scadbuddy.example:80')).toBe('https://scadbuddy.example:80')
  })

  it('refuses a Host that is not a host', () => {
    for (const host of ['user@evil.test', 'evil.test/path', 'a b', '', 'evil.test:99999999']) {
      expect(requestOrigin('https', host)).toBeUndefined()
    }
  })
})

describe('trusted proxies', () => {
  it('parses addresses and ranges of both families', () => {
    const list = parseCidrList('10.42.0.0/16, 192.168.1.10 ,fd00::/8')
    expect(list.check('10.42.9.9', 'ipv4')).toBe(true)
    expect(list.check('10.43.0.1', 'ipv4')).toBe(false)
    expect(list.check('192.168.1.10', 'ipv4')).toBe(true)
    expect(list.check('192.168.1.11', 'ipv4')).toBe(false)
    expect(list.check('fd12::1', 'ipv6')).toBe(true)
  })

  it('believes forwarded headers from a trusted peer only, including an IPv4-mapped one', () => {
    const policy = originPolicy('https://scadbuddy.example', '10.42.0.0/16')
    const headers = { host: 'agent:8081', 'x-forwarded-proto': 'https', 'x-forwarded-host': 'scadbuddy.example' }
    expect(effectiveRequest(req('::ffff:10.42.0.5', headers), policy)).toEqual({
      scheme: 'https',
      host: 'scadbuddy.example',
      viaTrustedProxy: true,
    })
    expect(effectiveRequest(req('10.43.0.5', headers), policy)).toEqual({
      scheme: 'http',
      host: 'agent:8081',
      viaTrustedProxy: false,
    })
    expect(isSecureTransport(req('10.43.0.5', headers), policy)).toBe(false)
    expect(isSecureTransport(req('10.42.0.5', headers), policy)).toBe(true)
  })

  it('treats an unknown forwarded scheme from a trusted proxy as neither', () => {
    const policy = originPolicy('https://scadbuddy.example', '10.42.0.0/16')
    const facts = req('10.42.0.5', { host: 'scadbuddy.example', origin: 'https://scadbuddy.example', 'x-forwarded-proto': 'wss' })
    expect(isSecureTransport(facts, policy)).toBe(false)
    expect(checkOrigin(facts, policy)).toEqual({ ok: false, reason: 'not-allowed' })
  })
})

describe('checkOrigin', () => {
  it('needs both Origin and the request origin on the list', () => {
    const policy = originPolicy('https://scadbuddy.example', '10.0.0.0/8')
    const via = (headers: Record<string, string>) => checkOrigin(req('10.0.0.7', { 'x-forwarded-proto': 'https', ...headers }), policy)
    expect(via({ host: 'scadbuddy.example', origin: 'https://scadbuddy.example' })).toEqual({
      ok: true,
      origin: 'https://scadbuddy.example',
      via: 'public',
    })
    expect(via({ host: 'evil.test', origin: 'https://evil.test' }).ok).toBe(false)
    expect(via({ host: 'evil.test', origin: 'https://scadbuddy.example' }).ok).toBe(false)
    expect(via({ host: 'scadbuddy.example', origin: 'https://evil.test' }).ok).toBe(false)
    expect(via({ host: 'scadbuddy.example' })).toEqual({ ok: false, reason: 'no-origin' })
    expect(via({ host: 'scadbuddy.example', origin: 'null' })).toEqual({ ok: false, reason: 'malformed-origin' })
  })

  it('accepts every origin in SCADBUDDY_ALLOWED_ORIGINS beside the public URL, each as its own pair', () => {
    const policy = originPolicy(
      'https://scadbuddy.example',
      '10.0.0.0/8',
      ' https://scadbuddy.internal.example:443 ,http://scadbuddy.lan:8080, ',
    )
    const via = (headers: Record<string, string>) => checkOrigin(req('10.0.0.7', { 'x-forwarded-proto': 'https', ...headers }), policy)
    expect(via({ host: 'scadbuddy.internal.example', origin: 'https://scadbuddy.internal.example' })).toEqual({
      ok: true,
      origin: 'https://scadbuddy.internal.example',
      via: 'public',
    })
    expect(via({ host: 'scadbuddy.example', origin: 'https://scadbuddy.example' }).ok).toBe(true)
    // Both names are allowed, but a page on one may not address the other.
    expect(via({ host: 'scadbuddy.example', origin: 'https://scadbuddy.internal.example' }).ok).toBe(false)
    expect(via({ host: 'scadbuddy.lan', origin: 'https://scadbuddy.lan' }).ok).toBe(false)
    expect(
      checkOrigin(
        req('10.0.0.7', { 'x-forwarded-proto': 'http', host: 'scadbuddy.lan:8080', origin: 'http://scadbuddy.lan:8080' }),
        policy,
      ).ok,
    ).toBe(true)
    expect(() => originPolicy(undefined, undefined, 'scadbuddy.lan')).toThrow(OriginConfigError)
  })

  it('accepts the loopback pair only from a loopback peer that is not a proxy', () => {
    const policy = originPolicy(undefined, '127.0.0.1')
    const local = { host: 'localhost:8081', origin: 'http://localhost:8081' }
    // 127.0.0.1 is configured as a trusted proxy here, so it is not "direct" loopback.
    expect(checkOrigin(req('127.0.0.1', local), policy).ok).toBe(false)
    expect(checkOrigin(req('127.0.0.2', local), policy)).toMatchObject({ ok: true, via: 'loopback' })
  })

  it('knows loopback peers', () => {
    for (const peer of ['127.0.0.1', '127.9.9.9', '::1', '::ffff:127.0.0.1']) expect(isLoopbackPeer(peer)).toBe(true)
    for (const peer of ['10.0.0.1', '::ffff:10.0.0.1', 'localhost', undefined]) expect(isLoopbackPeer(peer)).toBe(false)
  })
})
