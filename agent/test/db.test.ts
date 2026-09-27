import { describe, expect, it } from 'vitest'
import { type CancellableQuery, makePing } from '../src/db.js'

/** A fake postgres.js query that settles only when told to, and records cancels. */
function fakeQuery() {
  let settle!: (ok: boolean) => void
  const promise = new Promise<unknown>((resolve, reject) => {
    settle = (ok) => (ok ? resolve([{ '?column?': 1 }]) : reject(new Error('canceling statement')))
  })
  const query = Object.assign(promise, {
    cancelled: false,
    cancel() {
      query.cancelled = true
      settle(false)
    },
  })
  return { query, settle }
}

describe('makePing', () => {
  it('is true when the query answers in time', async () => {
    const { query, settle } = fakeQuery()
    const ping = makePing(() => query)
    const result = ping(1000)
    settle(true)
    expect(await result).toBe(true)
    expect(query.cancelled).toBe(false)
  })

  it('cancels the query on timeout so it releases its connection', async () => {
    const { query } = fakeQuery()
    const ping = makePing(() => query)
    expect(await ping(20)).toBe(false)
    expect(query.cancelled).toBe(true)
  })

  it('is false when the query fails', async () => {
    const { query, settle } = fakeQuery()
    const ping = makePing(() => query)
    const result = ping(1000)
    settle(false)
    expect(await result).toBe(false)
  })

  it('shares one outstanding ping between concurrent callers', async () => {
    const issued: CancellableQuery[] = []
    const ping = makePing(() => {
      const { query } = fakeQuery()
      issued.push(query)
      return query
    })
    const results = await Promise.all([ping(20), ping(20), ping(20)])
    expect(results).toEqual([false, false, false])
    expect(issued).toHaveLength(1)
    // Once settled, the next ping issues a fresh query.
    await ping(20)
    expect(issued).toHaveLength(2)
  })
})
