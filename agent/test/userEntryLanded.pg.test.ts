import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { userEntryLanded } from '../src/sessions/turnHost.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// TurnHost's check that a turn's message reached the transcript (a continuation lost after it
// landed resumes with the restart message, never sends it twice). Rows from before sealing
// carry no entry_type, so their JSON is read.

describe.skipIf(!TEST_DATABASE_URL)(`userEntryLanded${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  const id = randomUUID()

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  async function legacy(entry: string, subpath = ''): Promise<number> {
    const [row] = await db.sql<{ id: string }[]>`
      INSERT INTO ai_session_entries (project_key, session_id, subpath, entry)
      VALUES ('p', ${id}, ${subpath}, ${entry}) RETURNING id`
    return Number(row!.id)
  }

  it('reads a legacy row`s own type, not a substring an assistant entry quotes', async () => {
    const mark = await legacy(JSON.stringify({ type: 'system', subtype: 'init' }))
    // A nested object, so `"type":"user"` is in the row's text unescaped.
    await legacy(JSON.stringify({ type: 'assistant', message: { content: [{ type: 'tool_use', name: 'x', input: { type: 'user' } }] } }))
    await legacy('not json "user" {')
    await legacy(JSON.stringify({ type: 'user', message: { content: 'side' } }), 'subagents/a')
    expect(await userEntryLanded(db.sql, id, mark)).toBe(false)
    await legacy(JSON.stringify({ uuid: 'u1', type: 'user', message: { content: 'hello' } }))
    expect(await userEntryLanded(db.sql, id, mark)).toBe(true)
  })

  it('takes a sealed row`s entry_type', async () => {
    const mark = await legacy(JSON.stringify({ type: 'system' }))
    await db.sql`
      INSERT INTO ai_session_entries (project_key, session_id, subpath, entry_type, sealed)
      VALUES ('p', ${id}, '', 'user', ${Buffer.from('opaque')})`
    expect(await userEntryLanded(db.sql, id, mark)).toBe(true)
  })
})
