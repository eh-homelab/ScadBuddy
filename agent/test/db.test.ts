import type { Sql } from 'postgres'
import { describe, expect, it, vi } from 'vitest'
import { type CancellableQuery, createLockClient, makePing, makeReady, TEST_LOCK_CONNECTIONS } from '../src/db.js'

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

describe('makeReady', () => {
  it('retries after a failure and memoises the first success', async () => {
    let calls = 0
    const errors: unknown[] = []
    const ready = makeReady(
      () => (++calls === 1 ? Promise.reject(new Error('down')) : Promise.resolve()),
      (err) => errors.push(err),
    )
    expect(await ready()).toBe(false)
    expect(errors).toHaveLength(1)
    expect(await Promise.all([ready(), ready()])).toEqual([true, true])
    expect(await ready()).toBe(true)
    expect(calls).toBe(2)
  })
})

describe('createLockClient (#4994)', () => {
  /** A fake postgres.js client per `make` call: each reserve hands out a connection that records its release. */
  function fakeMake() {
    const clients: { reserve: ReturnType<typeof vi.fn>; end: ReturnType<typeof vi.fn>; released: number }[] = []
    const make = vi.fn((max: number) => {
      expect(max).toBe(TEST_LOCK_CONNECTIONS)
      const client = {
        released: 0,
        reserve: vi.fn(async () => ({ release: () => void client.released++ })),
        end: vi.fn(async () => {}),
      }
      clients.push(client)
      return client as unknown as Sql
    })
    return { make, clients }
  }

  it('makes its client on the first take, reuses it, and caps the leases out at once', async () => {
    const { make, clients } = fakeMake()
    const locks = createLockClient(make)
    expect(make).not.toHaveBeenCalled()
    const leases = await Promise.all(Array.from({ length: TEST_LOCK_CONNECTIONS }, () => locks.take()))
    expect(make).toHaveBeenCalledTimes(1)
    expect(await locks.take()).toBeUndefined()
    leases[0]!.done(true)
    expect(clients[0]!.released).toBe(1)
    expect(await locks.take()).toBeDefined()
    expect(make).toHaveBeenCalledTimes(1)
  })

  it('retires its client on an unhealthy lease: never released, ended after its last lease, the next take from a fresh one', async () => {
    const { make, clients } = fakeMake()
    const locks = createLockClient(make)
    const bad = (await locks.take())!
    const other = (await locks.take())!
    bad.done(false)
    expect(clients[0]!.released).toBe(0)
    // Another lease of the retired client is still out: it is not ended under it.
    expect(clients[0]!.end).not.toHaveBeenCalled()
    const fresh = (await locks.take())!
    expect(make).toHaveBeenCalledTimes(2)
    expect(clients[1]!.reserve).toHaveBeenCalledTimes(1)
    other.done(true)
    expect(clients[0]!.released).toBe(1)
    expect(clients[0]!.end).toHaveBeenCalledWith({ timeout: 0 })
    fresh.done(true)
    expect(clients[1]!.end).not.toHaveBeenCalled()
    // A second done is ignored: the count stays right.
    fresh.done(false)
    expect(make).toHaveBeenCalledTimes(2)
    await locks.end()
    expect(clients[1]!.end).toHaveBeenCalledTimes(1)
  })

  it('a reserve that fails frees its slot and throws', async () => {
    const make = vi.fn(() => ({ reserve: vi.fn(() => Promise.reject(new Error('connection refused'))), end: vi.fn(async () => {}) }) as unknown as Sql)
    const locks = createLockClient(make)
    for (let i = 0; i <= TEST_LOCK_CONNECTIONS; i++) await expect(locks.take()).rejects.toThrow('connection refused')
  })
})
