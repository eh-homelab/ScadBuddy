import { createHash } from 'node:crypto'
import { describe, expect, it } from 'vitest'
import { CrudPluginFeeds, FeedWriteError } from '../src/plugins/feed.js'

// The feed's CRUD client (#6936, plugins/feed.ts `CrudPluginFeeds`): the writes and the mirror
// read over area `plugin-feeds` and `package-mirrors`, against a fake of the routes (backend
// the Go tests in tools/go/core covers the routes).

const BASE = 'http://api.test'
const PREFIX = '/api/v1/internal/crud/agent'
const CRUD = { 'x-scadbuddy-crud': '1', 'content-type': 'application/json' }

function fake(answer: () => Response) {
  const seen: { method: string; path: string }[] = []
  const fetchImpl: typeof fetch = (input, init) => {
    seen.push({ method: init?.method ?? 'GET', path: new URL(String(input)).pathname.slice(PREFIX.length) })
    return Promise.resolve(answer())
  }
  return { seen, feeds: new CrudPluginFeeds({ baseUrl: BASE, fetch: fetchImpl }) }
}

describe('CrudPluginFeeds', () => {
  it('adds with a PUT and removes with a DELETE, answering whether it changed', async () => {
    const { seen, feeds } = fake(() => new Response('{"done":true}', { status: 200, headers: CRUD }))
    expect(await feeds.add('default', 'hello')).toBe(true)
    expect(await feeds.remove('default', 'hello')).toBe(true)
    expect(seen).toEqual([
      { method: 'PUT', path: '/plugin-feeds/default/packages/hello' },
      { method: 'DELETE', path: '/plugin-feeds/default/packages/hello' },
    ])
  })

  it("names the route's 404 reason", async () => {
    for (const reason of ['feed-not-found', 'package-not-found'] as const) {
      const { feeds } = fake(() => new Response(JSON.stringify({ detail: 'no', reason }), { status: 404, headers: CRUD }))
      const err = await feeds.add('default', 'hello').catch((e: unknown) => e)
      expect(err).toBeInstanceOf(FeedWriteError)
      expect((err as FeedWriteError).reason).toBe(reason)
    }
  })

  it('names a 404 or 405 that is not the route\'s own as an API that does not serve the write yet', async () => {
    for (const answer of [
      () => new Response('{"detail":"Not Found"}', { status: 404, headers: { 'content-type': 'application/json' } }),
      () => new Response('{"detail":"Method Not Allowed"}', { status: 405, headers: { 'content-type': 'application/json' } }),
    ]) {
      const { feeds } = fake(answer)
      const err = await feeds.remove('default', 'hello').catch((e: unknown) => e)
      expect(err).toBeInstanceOf(FeedWriteError)
      expect((err as FeedWriteError).reason).toBe('not-served')
    }
  })

  it('reads a mirror, checks it against its sha256, and answers undefined for none', async () => {
    const bytes = Buffer.from('PK\u0003\u0004 a zip')
    const sha = createHash('sha256').update(bytes).digest('hex')
    const kept = fake(() => new Response(new Uint8Array(bytes), { status: 200, headers: { 'x-scadbuddy-crud': '1' } }))
    expect(await kept.feeds.mirror('hello', sha)).toEqual(bytes)
    expect(kept.seen[0]!.path).toBe(`/package-mirrors/hello/${sha}`)
    await expect(kept.feeds.mirror('hello', 'f'.repeat(64))).rejects.toThrow(/does not hash/)
    const none = fake(() => new Response('{"detail":"no","reason":"mirror-not-found"}', { status: 404, headers: CRUD }))
    expect(await none.feeds.mirror('hello', sha)).toBeUndefined()
  })
})
