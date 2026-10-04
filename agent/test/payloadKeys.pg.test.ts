import { randomBytes, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { migrate } from '../src/db/migrations.js'
import { kekFromBase64, SealError } from '../src/secrets.js'
import { PgPayloadKeys } from '../src/temporal/payloadKeys.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// ai_payload_keys (spec 2026-10-01 §6.5, plan ruling 11): one data key per subject,
// sealed under the KEK, cached for 60 s.

const newKek = () => kekFromBase64(randomBytes(32).toString('base64'))

describe.skipIf(!TEST_DATABASE_URL)(
  `payload keys in Postgres${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
  () => {
    let db: Database
    let drop: () => Promise<void>
    const subject = `session-${randomUUID()}`

    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      await migrate(db.sql)
    })
    afterEach(async () => {
      await drop()
    })

    it('keeps one row when a subject is keyed twice', async () => {
      const keys = new PgPayloadKeys(db.sql, { current: newKek() })
      await keys.createKey(subject)
      const first = await keys.dataKey(subject)
      await keys.createKey(subject)
      const rows = await db.sql`SELECT subject FROM ai_payload_keys`
      expect(rows).toHaveLength(1)
      expect(first).toHaveLength(32)
      expect(await new PgPayloadKeys(db.sql, keys.keks).dataKey(subject)).toEqual(first)
    })

    it('creates the key inside a caller transaction', async () => {
      const keys = new PgPayloadKeys(db.sql, { current: newKek() })
      await db.sql
        .begin(async (tx) => {
          await keys.createKey(subject, tx)
          throw new Error('roll back')
        })
        .catch(() => undefined)
      expect(await keys.dataKey(subject)).toBeUndefined()
    })

    it('opens a key sealed under the previous KEK', async () => {
      const previous = newKek()
      const old = new PgPayloadKeys(db.sql, { current: previous })
      await old.createKey(subject)
      const dek = await old.dataKey(subject)
      const rotated = new PgPayloadKeys(db.sql, { current: newKek(), previous })
      expect(await rotated.dataKey(subject)).toEqual(dek)
      await expect(new PgPayloadKeys(db.sql, { current: newKek() }).dataKey(subject)).rejects.toThrow(SealError)
    })

    it('re-wraps keys from the previous KEK, leaving rows that do not open', async () => {
      const previous = newKek()
      const current = newKek()
      const old = new PgPayloadKeys(db.sql, { current: previous })
      await old.createKey(subject)
      const dek = await old.dataKey(subject)
      // A row stamped with the previous KEK's id that it cannot open.
      const broken = `flow-${randomUUID()}`
      await new PgPayloadKeys(db.sql, { current: { id: previous.id, key: randomBytes(32) } }).createKey(broken)
      expect(await new PgPayloadKeys(db.sql, { current }).rewrapFrom(previous, current)).toEqual({ rewrapped: 1, failed: 1 })
      expect(await new PgPayloadKeys(db.sql, { current }).dataKey(subject)).toEqual(dek)
      const rows = await db.sql<{ subject: string; kek_id: string }[]>`SELECT subject, kek_id FROM ai_payload_keys`
      expect(Object.fromEntries(rows.map((r) => [r.subject, r.kek_id]))).toEqual({ [subject]: current.id, [broken]: previous.id })
    })

    it('rejects a subject that is not a session or flow id', async () => {
      const keys = new PgPayloadKeys(db.sql, { current: newKek() })
      await expect(keys.createKey('render-x')).rejects.toThrow('render-x is not a session or flow workflow id')
    })

    it('forgets at once here, and within the cache window elsewhere', async () => {
      let now = 0
      const kek = newKek()
      const here = new PgPayloadKeys(db.sql, { current: kek }, { now: () => now })
      const elsewhere = new PgPayloadKeys(db.sql, { current: kek }, { now: () => now })
      await here.createKey(subject)
      expect(await elsewhere.dataKey(subject)).toHaveLength(32)
      await here.forget(subject)
      expect(await here.dataKey(subject)).toBeUndefined()
      now += 59_000
      expect(await elsewhere.dataKey(subject)).toHaveLength(32)
      now += 2_000
      expect(await elsewhere.dataKey(subject)).toBeUndefined()
    })
  },
)
