import { describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { FailClosedTokenStore, hashToken, TOKEN_PREFIX } from '../src/auth/tokens.js'
import { BoundedEventStore } from '../src/mcp/eventStore.js'
import { PendingActionStore, PendingStoreFullError } from '../src/tools/pending.js'
import { InMemoryTokenStore } from './support/memoryTokens.js'

describe('tiers', () => {
  it('include every lower tier', () => {
    expect(tiersUpTo('read')).toEqual(['read'])
    expect(tiersUpTo('write')).toEqual(['read', 'write'])
    expect(tiersUpTo('outward')).toEqual(['read', 'write', 'outward'])
  })
})

describe('InMemoryTokenStore (the /mcp tests’ stand-in; Postgres is test/tokens.pg.test.ts)', () => {
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

describe('FailClosedTokenStore (no database configured)', () => {
  it('verifies nothing and cannot mint', async () => {
    const store = new FailClosedTokenStore()
    expect(await store.verify()).toBeNull()
    await expect(store.mint()).rejects.toThrow(/SCADBUDDY_DATABASE_URL/)
    expect(await store.list()).toEqual([])
    expect(await store.revoke()).toBe(false)
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

describe('PendingActionStore bounds', () => {
  const prep = (store: PendingActionStore, principalId: string, n = 1) =>
    Array.from({ length: n }, (_, i) =>
      store.prepare({ tool: 't', args: {}, summary: `${principalId} ${i}`, principalId }),
    )

  it("a principal filling its quota evicts only its own oldest, never another's", () => {
    const store = new PendingActionStore({ perPrincipal: 3, total: 100 })
    const [b] = prep(store, 'B')
    const a = prep(store, 'A', 10)
    expect(store.get(b!.id, 'B')).toBeDefined()
    expect(store.list('A').map((x) => x.id)).toEqual(a.slice(-3).map((x) => x.id))
    expect(store.list('B')).toHaveLength(1)
  })

  it('refuses new prepares at the global bound instead of evicting anyone', () => {
    const store = new PendingActionStore({ perPrincipal: 5, total: 4 })
    const kept = [...prep(store, 'A', 2), ...prep(store, 'B', 2)]
    expect(() => prep(store, 'C')).toThrow(PendingStoreFullError)
    for (const action of kept) expect(store.get(action.id, action.principalId)).toBeDefined()
  })
})
