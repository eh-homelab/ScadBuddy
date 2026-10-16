import { createHash, randomBytes, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { kekFromBase64 } from '../src/secrets.js'
import { sessionImage } from '../src/sessions/blobs.js'
import { forgetSubject } from '../src/sessions/forget.js'
import { PgFlowBlobs } from '../src/temporal/flowBlobs.js'
import { PgPayloadKeys, SubjectForgotten } from '../src/temporal/payloadCodec.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2477: a flow run's images, kept by its subject and sealed under its payload key.

const kek = kekFromBase64(randomBytes(32).toString('base64'))
const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const NAME = `${createHash('sha256').update(PNG).digest('hex')}.png`

describe.skipIf(!TEST_DATABASE_URL)(`flow images${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it("keeps a run's image sealed under its key and gives it back to that run only", async () => {
    const subject = `flow-${randomUUID()}`
    const blobs = new PgFlowBlobs(db.sql, new PgPayloadKeys(db.sql, kek))
    await blobs.put(subject, [sessionImage('image/png', PNG), sessionImage('image/png', PNG)])
    const rows = await db.sql<{ name: string; size: number; data: Buffer }[]>`SELECT name, size, data FROM ai_flow_blobs WHERE subject = ${subject}`
    expect(rows.map((r) => [r.name, r.size])).toEqual([[NAME, PNG.length]])
    // Sealed: neither the bytes nor their PNG signature are in the row.
    expect(rows[0]!.data.includes(PNG.subarray(0, 8))).toBe(false)
    expect(await blobs.get(subject, NAME)).toEqual({ mediaType: 'image/png', bytes: PNG })
    expect(await blobs.get(`flow-${randomUUID()}`, NAME)).toBeUndefined()
    expect(await blobs.get(subject, '../x.png')).toBeUndefined()
  })

  it("keeps at most the run's cap, and answers the names it holds (#2578)", async () => {
    const subject = `flow-${randomUUID()}`
    const blobs = new PgFlowBlobs(db.sql, new PgPayloadKeys(db.sql, kek), 1)
    const png = sessionImage('image/png', PNG)
    const other = sessionImage('image/png', Buffer.concat([PNG, Buffer.from([0])]))
    expect(await blobs.put(subject, [png])).toEqual([NAME])
    expect(await blobs.put(subject, [other, png])).toEqual([NAME])
    const rows = await db.sql<{ name: string }[]>`SELECT name FROM ai_flow_blobs WHERE subject = ${subject}`
    expect(rows.map((r) => r.name)).toEqual([NAME])
    // Another run has its own room.
    expect(await blobs.put(`flow-${randomUUID()}`, [other])).toEqual([other.name])
  })

  it('deletes them with the run, and keeps none for a forgotten run', async () => {
    const subject = `flow-${randomUUID()}`
    const keys = new PgPayloadKeys(db.sql, kek)
    const blobs = new PgFlowBlobs(db.sql, keys)
    await blobs.put(subject, [sessionImage('image/png', PNG)])
    await forgetSubject({ sql: db.sql }, subject)
    expect(await db.sql`SELECT 1 FROM ai_flow_blobs WHERE subject = ${subject}`).toHaveLength(0)
    // This process still holds the key for a while: the tombstone refuses the rows anyway.
    await expect(blobs.put(subject, [sessionImage('image/png', PNG)])).rejects.toBeInstanceOf(SubjectForgotten)
    expect(await db.sql`SELECT 1 FROM ai_flow_blobs WHERE subject = ${subject}`).toHaveLength(0)
  })
})
