import { randomBytes, randomUUID } from 'node:crypto'
import type { Sql } from 'postgres'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { kekFromBase64 } from '../src/secrets.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { PgPayloadKeys, SubjectForgotten } from '../src/temporal/payloadCodec.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// forgetSubject and key creation serialise on the subject's advisory lock (#2157), so an
// encoder that asks for a key while a forget is committing waits for it, sees the
// tombstone and makes no key. Without the lock on either side, the encoder's tombstone
// check reads from before the forget commits and a forgotten subject gets a fresh key.

const kek = kekFromBase64(randomBytes(32).toString('base64'))

/** `sql`, whose transactions stay open, their statements run, until `release`. */
function holdingCommits(sql: Sql): { sql: Sql; committing: Promise<void>; release: () => void } {
  let release!: () => void
  const gate = new Promise<void>((resolve) => (release = resolve))
  let reached!: () => void
  const committing = new Promise<void>((resolve) => (reached = resolve))
  const held = Object.assign((...args: Parameters<Sql>) => sql(...args), {
    begin: (fn: (tx: Sql) => Promise<unknown>) =>
      sql.begin(async (tx) => {
        const result = await fn(tx as unknown as Sql)
        reached()
        await gate
        return result
      }),
  })
  return { sql: held as unknown as Sql, committing, release }
}

describe.skipIf(!TEST_DATABASE_URL)(
  `forgetSubject against key creation${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
    })
    afterEach(async () => {
      await drop()
    })

    it('makes no key for a subject whose forget commits while the encoder waits', async () => {
      const subject = `flow-${randomUUID()}`
      const hold = holdingCommits(db.sql)
      const forgetting = forgetSubject({ sql: hold.sql }, subject)
      await hold.committing

      const making = new PgPayloadKeys(db.sql, kek).keyFor(subject, true)
      const settled = making.then(
        () => 'settled',
        () => 'settled',
      )
      // The encoder is blocked on the lock the open forget holds (or, without it, done).
      for (let i = 0; i < 100; i++) {
        const [row] = await db.sql<{ waiting: number }[]>`
          SELECT count(*)::int AS waiting FROM pg_locks WHERE locktype = 'advisory' AND NOT granted`
        if (row && row.waiting > 0) break
        if ((await Promise.race([settled, new Promise((r) => setTimeout(r, 20, 'pending'))])) === 'settled') break
      }

      hold.release()
      expect(await forgetting).toMatchObject({ key: false })
      await expect(making).rejects.toBeInstanceOf(SubjectForgotten)
      expect(await db.sql`SELECT 1 FROM ai_payload_keys WHERE subject = ${subject}`).toHaveLength(0)
    })
  },
)
