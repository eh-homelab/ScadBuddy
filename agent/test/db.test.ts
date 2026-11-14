import type { Sql } from 'postgres'
import { describe, expect, it, vi } from 'vitest'
import {
  AGENT_DB_CONNECTIONS,
  APPLICATION_NAME,
  type CancellableQuery,
  clientOptions,
  createLockClient,
  IDLE_TIMEOUT_SECONDS,
  makePing,
  makeReady,
  makeUntilReady,
  SQL_CONNECTIONS,
  TEST_LOCK_CONNECTIONS,
} from '../src/db.js'

describe('the connection budget (#5226)', () => {
  it('sums the stores, the lock client and the listener, and every client closes what sits idle', () => {
    // Change this on purpose: README "Running it" counts it per pod.
    expect(AGENT_DB_CONNECTIONS).toBe(9)
    expect(SQL_CONNECTIONS + TEST_LOCK_CONNECTIONS + 1).toBe(AGENT_DB_CONNECTIONS)
    expect(IDLE_TIMEOUT_SECONDS).toBe(60)
    expect(clientOptions(SQL_CONNECTIONS)).toMatchObject({
      max: SQL_CONNECTIONS,
      idle_timeout: IDLE_TIMEOUT_SECONDS,
      connection: { application_name: APPLICATION_NAME },
    })
    expect(clientOptions(1, 'a_schema').connection).toEqual({ application_name: APPLICATION_NAME, search_path: 'a_schema' })
  })
})

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

describe('makeUntilReady (#5299)', () => {
  it("calls ready() again, with back-off, until the first true, and shares that one wait", async () => {
    // The API may take longer than one ready() to apply the agent's migrations: a start task
    // that ran once on the first false would never run in this process.
    const answers = [false, false, true]
    const ready = vi.fn(() => Promise.resolve(answers.shift() ?? true))
    const untilReady = makeUntilReady(ready, { firstDelayMs: 1, maxDelayMs: 2 })
    await Promise.all([untilReady(), untilReady()])
    expect(ready).toHaveBeenCalledTimes(3)
    await untilReady()
    expect(ready).toHaveBeenCalledTimes(3)
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
