import { describe, expect, it } from 'vitest'
import { checkTransport, isLoopback } from '../src/auth/authenticate.js'
import { tiersUpTo } from '../src/auth/principal.js'
import { FailClosedTokenStore, hashToken, InMemoryTokenStore, TOKEN_PREFIX } from '../src/auth/tokens.js'
import { BoundedEventStore } from '../src/mcp/eventStore.js'

describe('tiers', () => {
  it('include every lower tier', () => {
    expect(tiersUpTo('read')).toEqual(['read'])
    expect(tiersUpTo('write')).toEqual(['read', 'write'])
    expect(tiersUpTo('outward')).toEqual(['read', 'write', 'outward'])
  })
})

describe('InMemoryTokenStore', () => {
  it('stores only a hash, verifies to the token tier, and records last use', async () => {
    const store = new InMemoryTokenStore()
    const { token, record } = await store.mint({ name: 'ci', tier: 'write' })
    expect(token.startsWith(TOKEN_PREFIX)).toBe(true)
    expect(JSON.stringify(await store.list())).not.toContain(token)
    expect(hashToken(token)).toMatch(/^[0-9a-f]{64}$/)
    const now = new Date('2026-09-27T12:00:00Z')
    expect(await store.verify(token, now)).toEqual({ id: `token:${record.id}`, kind: 'bearer', tiers: ['read', 'write'] })
    expect((await store.list())[0]?.lastUsedAt).toEqual(now)
  })

  it('refuses unknown, expired and revoked tokens', async () => {
    const store = new InMemoryTokenStore()
    expect(await store.verify('sbmcp_unknown')).toBeNull()
    const expiring = await store.mint({ name: 'e', tier: 'read', expiresAt: new Date('2026-01-01T00:00:00Z') })
    expect(await store.verify(expiring.token, new Date('2025-12-31T00:00:00Z'))).not.toBeNull()
    expect(await store.verify(expiring.token, new Date('2026-01-01T00:00:00Z'))).toBeNull()
    const revoked = await store.mint({ name: 'r', tier: 'read' })
    expect(await store.revoke(revoked.record.id)).toBe(true)
    expect(await store.revoke(revoked.record.id)).toBe(false)
    expect(await store.verify(revoked.token)).toBeNull()
  })
})

describe('FailClosedTokenStore (production until #255)', () => {
  it('verifies nothing and cannot mint', async () => {
    const store = new FailClosedTokenStore()
    expect(await store.verify()).toBeNull()
    await expect(store.mint()).rejects.toThrow(/#255/)
  })
})

describe('transport rule', () => {
  const req = (headers: Record<string, string> = {}) =>
    new Request('http://agent:8081/mcp?x=1', { headers: { host: 'scadbuddy.lan', ...headers } })

  it('recognises loopback addresses, including IPv4-mapped IPv6', () => {
    for (const a of ['127.0.0.1', '127.1.2.3', '::1', '::ffff:127.0.0.1']) expect(isLoopback(a), a).toBe(true)
    for (const a of ['10.0.0.1', '::ffff:10.0.0.1', '', undefined]) expect(isLoopback(a), String(a)).toBe(false)
  })

  it('names the https URL on the Host the client used', async () => {
    const res = checkTransport(req(), '10.1.1.1')
    expect(res?.status).toBe(403)
    expect(await res?.json()).toMatchObject({ https_url: 'https://scadbuddy.lan/mcp?x=1' })
  })

  it('trusts the first X-Forwarded-Proto value', () => {
    expect(checkTransport(req({ 'x-forwarded-proto': 'https' }), '10.1.1.1')).toBeUndefined()
    expect(checkTransport(req({ 'x-forwarded-proto': 'HTTPS, http' }), '10.1.1.1')).toBeUndefined()
    expect(checkTransport(req({ 'x-forwarded-proto': 'http, https' }), '10.1.1.1')?.status).toBe(403)
    expect(checkTransport(req(), '127.0.0.1')).toBeUndefined()
  })
})

describe('BoundedEventStore (Last-Event-ID replay, in memory)', () => {
  const msg = (id: number) => ({ jsonrpc: '2.0' as const, method: 'notifications/progress', params: { id } })

  it('replays only later events of the same stream', async () => {
    const store = new BoundedEventStore()
    const a1 = await store.storeEvent('a', msg(1))
    await store.storeEvent('b', msg(2))
    await store.storeEvent('a', msg(3))
    const sent: unknown[] = []
    expect(await store.replayEventsAfter(a1, { send: async (_id, m) => void sent.push(m) })).toBe('a')
    expect(sent).toEqual([msg(3)])
    expect(await store.getStreamIdForEventId(a1)).toBe('a')
  })

  it('forgets the oldest events past its bound', async () => {
    const store = new BoundedEventStore(2)
    const first = await store.storeEvent('a', msg(1))
    await store.storeEvent('a', msg(2))
    await store.storeEvent('a', msg(3))
    expect(await store.replayEventsAfter(first, { send: async () => {} })).toBe('')
  })
})
