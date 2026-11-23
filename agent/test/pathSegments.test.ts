import { describe, expect, it } from 'vitest'
import { crudRead } from '../src/api/crudRead.js'
import { DotSegmentError, isDotSegment, refuseDotSegments } from '../src/api/pathSegments.js'
import { crudCostPoster } from '../src/sessions/orderCosts.js'

describe('dot segments in backend paths (#7221)', () => {
  it('reads ., .. and their %2e spellings as dot segments, and nothing else', () => {
    for (const dot of ['.', '..', '%2e', '%2E%2e', '.%2e']) expect(isDotSegment(dot)).toBe(true)
    for (const other of ['', '...', '.env', 'a.', '..x', '%2e%2e%2e', '%252e']) expect(isDotSegment(other)).toBe(false)
  })

  it('refuses a URL whose path names one, but not its query or fragment', () => {
    expect(() => refuseDotSegments('http://api.example/a/../b', 'a read')).toThrow(DotSegmentError)
    expect(() => refuseDotSegments('/a/./b', 'a read')).toThrow('a read: a path segment is "." or "..", so it was not sent')
    expect(() => refuseDotSegments('http://api.example/a/..', 'a read')).toThrow(DotSegmentError)
    expect(refuseDotSegments('http://api.example/a/b?x=../y#..', 'a read')).toBe('http://api.example/a/b?x=../y#..')
    expect(refuseDotSegments('http://api.example/a/..%2Fx/b', 'a read')).toBe('http://api.example/a/..%2Fx/b')
  })

  it('a CRUD read with a dot segment mid-path is never sent', async () => {
    const sent: string[] = []
    const fetchImpl = (async (url: string) => {
      sent.push(url)
      return new Response('{}', { headers: { 'x-scadbuddy-crud': '1' } })
    }) as unknown as typeof fetch
    await expect(crudRead(fetchImpl, 'http://api.example', 'a background task', '/background-tasks/../operations/x')).rejects.toBeInstanceOf(DotSegmentError)
    expect(sent).toEqual([])
  })

  it("refuses an order cost for an order id of . or .. for good, and posts nothing", async () => {
    const sent: string[] = []
    const fetchImpl = (async (url: string) => {
      sent.push(url)
      return new Response('{}')
    }) as unknown as typeof fetch
    const report = { sessionId: 's1', principal: 'person:owner', orderItemId: 'i1', costUsd: '0.5' }
    for (const orderId of ['..', '.']) expect(await crudCostPoster('http://api.example', fetchImpl)({ ...report, orderId } as never, 1_000)).toBe('refused')
    expect(sent).toEqual([])
  })
})
