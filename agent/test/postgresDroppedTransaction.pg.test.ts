import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #3294: a transaction whose connection the server ends (a backend terminated, a
// restart, a failover) while its callback still runs. postgres.js rejects the
// transaction at once, but the callback goes on; when it settles, postgres.js sends
// ROLLBACK (or COMMIT) on the connection that has already closed, and its deferred
// write throws `Cannot read properties of null (reading 'write')` from setImmediate:
// an uncaught exception, which ends the process (CI: escalationDelivery.pg,
// attention.pg). The patch in patches/ has that query fail as the connection did.

describe.skipIf(!TEST_DATABASE_URL)(`a transaction whose connection is dropped${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  const uncaught: unknown[] = []
  const record = (error: unknown) => uncaught.push(error)

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    uncaught.length = 0
    process.on('uncaughtException', record)
  })
  afterEach(async () => {
    process.off('uncaughtException', record)
    await drop()
  })

  /** A transaction held open until `settle`, whose backend is then terminated. */
  async function dropped(settle: (release: { resolve: () => void; reject: (e: Error) => void }) => void) {
    let pidOf!: (pid: number) => void
    const pid = new Promise<number>((resolve) => (pidOf = resolve))
    let release!: { resolve: () => void; reject: (e: Error) => void }
    const held = new Promise<void>((resolve, reject) => (release = { resolve, reject }))
    const tx = db.sql.begin(async (t) => {
      const [row] = await t<{ pid: number }[]>`SELECT pg_backend_pid() AS pid`
      pidOf(row!.pid)
      await held
    })
    // The transaction fails as soon as its connection closes, before its callback ends.
    // Its handler is attached before the terminate: the closed socket can be seen before
    // the terminate's reply, and a rejection with no handler yet is an unhandled one.
    // Awaited together, so a wrong error that lands first fails this test, not the run.
    await Promise.all([
      expect(tx).rejects.toMatchObject({ code: 'CONNECTION_CLOSED' }),
      pid.then((p) => db.sql`SELECT pg_terminate_backend(${p})`),
    ])
    settle(release)
    // Long enough for the rollback or commit postgres.js then writes to have gone out:
    // connection.js defers each write through one setImmediate (nextWriteTimer), so the
    // throw this test catches lands within these turns. A shorter wait would pass
    // without ever reaching the write.
    for (let i = 0; i < 5; i++) await new Promise((resolve) => setImmediate(resolve))
    await new Promise((resolve) => setTimeout(resolve, 50))
  }

  it('a callback that fails afterwards rolls back nothing and throws nothing', async () => {
    await dropped((release) => release.reject(new Error('gone')))
    expect(uncaught).toEqual([])
    expect(await db.sql`SELECT 1 AS one`).toEqual([{ one: 1 }])
  })

  it('a callback that returns afterwards commits nothing and throws nothing', async () => {
    await dropped((release) => release.resolve())
    expect(uncaught).toEqual([])
    expect(await db.sql`SELECT 1 AS one`).toEqual([{ one: 1 }])
  })
})
