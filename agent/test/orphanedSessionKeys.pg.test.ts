import { randomBytes, randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { kekFromBase64 } from '../src/secrets.js'
import { sweepOrphanedSessionKeys } from '../src/sessions/forget.js'
import { PgPayloadKeys } from '../src/temporal/payloadCodec.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// #3317: a durable fork whose workflow could not start has its row deleted (SessionManager.fork),
// after the start had already made the child's payload key. The hourly sweep removes such a key,
// and never one a session still has.

const kek = kekFromBase64(randomBytes(32).toString('base64'))

describe.skipIf(!TEST_DATABASE_URL)(`sweepOrphanedSessionKeys${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  async function session(): Promise<string> {
    const id = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd)
      VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'idle', 50, 5)`
    return id
  }
  async function keyed(id: string, hoursAgo: number): Promise<string> {
    const subject = `session-${id}`
    await new PgPayloadKeys(db.sql, kek).keyFor(subject, true)
    await db.sql`UPDATE ai_payload_keys SET created_at = now() - make_interval(hours => ${hoursAgo}) WHERE subject = ${subject}`
    return subject
  }
  const keys = async () => (await db.sql<{ subject: string }[]>`SELECT subject FROM ai_payload_keys ORDER BY subject`).map((r) => r.subject)

  it('deletes an old key whose session is gone, and keeps a live session`s, a young one and a flow`s', async () => {
    const live = await keyed(await session(), 48)
    const forked = await session()
    const orphan = await keyed(forked, 48)
    await db.sql`DELETE FROM ai_sessions WHERE id = ${forked}`
    const young = await keyed(randomUUID(), 1)
    const flow = `flow-${randomUUID()}`
    await new PgPayloadKeys(db.sql, kek).keyFor(flow, true)
    await db.sql`UPDATE ai_payload_keys SET created_at = now() - interval '48 hours' WHERE subject = ${flow}`
    expect(await sweepOrphanedSessionKeys(db.sql)).toBe(1)
    expect(await keys()).toEqual([live, young, flow].sort())
    expect(await keys()).not.toContain(orphan)
    // Nothing left to sweep.
    expect(await sweepOrphanedSessionKeys(db.sql)).toBe(0)
  })

  it('keeps the key of a session whose row is gone but whose transcript rows remain', async () => {
    const id = randomUUID()
    const subject = await keyed(id, 48)
    await db.sql`
      INSERT INTO ai_session_entries (project_key, session_id, subpath, entry_type, sealed)
      VALUES ('p', ${id}, '', 'user', ${Buffer.from('opaque')})`
    expect(await sweepOrphanedSessionKeys(db.sql)).toBe(0)
    expect(await keys()).toEqual([subject])
  })
})
