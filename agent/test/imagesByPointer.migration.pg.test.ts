import { createHash, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { migrate, MIGRATIONS } from '../src/db/migrations.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #2479: the migration that takes base64 images out of stored rows. An upload's
// preview becomes bytes, and a `user.turn` logged with its previews inline names
// them instead, each moved into the session's blobs under its sha256 name.

const ID = '20261012T0226Z_images_by_pointer'
const skip = TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`

const PNG = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=', 'base64')
const JPEG = Buffer.from([0xff, 0xd8, 0xff, 0xe0, 0, 0x10, 0x4a, 0x46, 0x49, 0x46, 0, 1])
const sha = (bytes: Buffer) => createHash('sha256').update(bytes).digest('hex')

describe.skipIf(skip !== '')(`the images-by-pointer migration${skip}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase({ empty: true }))
    expect(MIGRATIONS.some((m) => m.id === ID)).toBe(true)
    await migrate(
      db.sql,
      MIGRATIONS.filter((m) => m.id < ID),
    )
  })
  afterEach(async () => {
    await drop()
  })

  it("turns an upload's base64 preview into bytes", async () => {
    const id = randomUUID()
    await db.sql`
      INSERT INTO ai_attachments (id, owner_kind, owner_id, name, media_type, data, preview_media_type, preview_data, expires_at)
      VALUES (${id}, 'browser', 'browser', ${`${sha(PNG)}.png`}, 'image/png', ${PNG}, 'image/jpeg', ${JPEG.toString('base64')},
              now() + interval '1 hour')`
    await migrate(db.sql, MIGRATIONS)
    const [row] = await db.sql<{ preview: Buffer }[]>`SELECT preview FROM ai_attachments WHERE id = ${id}`
    expect(Buffer.from(row!.preview).equals(JPEG)).toBe(true)
    const columns = await db.sql`SELECT column_name FROM information_schema.columns WHERE table_name = 'ai_attachments' AND column_name = 'preview_data'`
    expect(columns).toHaveLength(0)
  })

  it('names the previews a logged user.turn carried, and moves them into the session', async () => {
    const session = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${session}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 10, 1)`
    const full = `${'e'.repeat(64)}.gif`
    const turn = {
      v: 1,
      type: 'user.turn',
      sessionId: session,
      turnId: 't',
      text: 'look',
      author: { kind: 'browser', id: 'browser', label: 'You' },
      images: [
        { mediaType: 'image/jpeg', data: JPEG.toString('base64'), name: full },
        // One whose full image could not be stored.
        { mediaType: 'image/png', data: PNG.toString('base64') },
      ],
    }
    const other = { v: 1, type: 'assistant.text.done', sessionId: session, messageId: 'm' }
    await db.sql`
      INSERT INTO ai_session_events (session_id, seq, event)
      VALUES (${session}, 1, ${JSON.stringify(turn)}), (${session}, 2, ${JSON.stringify(other)})`

    await migrate(db.sql, MIGRATIONS)

    const rows = await db.sql<{ seq: string; event: string }[]>`SELECT seq, event FROM ai_session_events ORDER BY seq`
    const migrated = JSON.parse(rows[0]!.event) as Record<string, unknown>
    expect(migrated).toEqual({
      ...turn,
      images: [
        { name: full, mediaType: 'image/gif', preview: { name: `${sha(JPEG)}.jpg`, mediaType: 'image/jpeg' } },
        { preview: { name: `${sha(PNG)}.png`, mediaType: 'image/png' } },
      ],
    })
    expect(rows[0]!.event).not.toContain(JPEG.toString('base64'))
    expect(JSON.parse(rows[1]!.event)).toEqual(other)
    const blobs = await db.sql<{ name: string; media_type: string; data: Buffer }[]>`
      SELECT name, media_type, data FROM ai_session_blobs WHERE session_id = ${session} ORDER BY name`
    expect(blobs.map((b) => [b.name, b.media_type])).toEqual(
      [
        [`${sha(JPEG)}.jpg`, 'image/jpeg'],
        [`${sha(PNG)}.png`, 'image/png'],
      ].sort((a, b) => (a[0]! < b[0]! ? -1 : 1)),
    )
    expect(Buffer.from(blobs.find((b) => b.media_type === 'image/jpeg')!.data).equals(JPEG)).toBe(true)
  })
})
