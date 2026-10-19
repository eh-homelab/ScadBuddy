import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { sessionFinishActivities } from '../src/temporal/session/finishActivity.js'
import { SESSION_FINISH_ACTIVITY } from '../src/temporal/session/names.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, tempPaths } from './support/sessions.js'

// `session.finish_turn` (temporal/session/finishActivity.ts) writes a turn's end only while the
// row still runs that turn. AgentSession takes a message once the row is idle, before it has
// heard the activity's answer (Task 5.3, no busy window), so a retried finish of the turn before
// must never end the turn that claimed the row since.

describe.skipIf(!TEST_DATABASE_URL)(`session.finish_turn${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  it('writes nothing for a turn the row no longer runs: a retry after the next turn claimed it', async () => {
    const m = manager({ sql: db.sql, paths: await tempPaths() })
    const { session } = await m.start(browser, { origin: 'chat', title: 'two turns' })
    const finish = sessionFinishActivities(db.sql, m.events)[SESSION_FINISH_ACTIVITY]
    const [t1, t2] = [randomUUID(), randomUUID()]
    // The next turn has claimed the row (DurableTurns.send) after t1's end was written.
    await db.sql`UPDATE ai_sessions SET mode = 'durable', status = 'running', durable_turn_id = ${t2} WHERE id = ${session.id}`
    const before = (await m.events.read(session.id, 0, 1000)).length
    expect(await finish({ sessionId: session.id, turnId: t1, outcome: 'done' })).toBe(false)
    const [row] = await db.sql<{ status: string; durable_turn_id: string | null }[]>`SELECT status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
    expect(row).toEqual({ status: 'running', durable_turn_id: t2 })
    expect((await m.events.read(session.id, 0, 1000)).length).toBe(before)
    // Its own turn's end is written.
    expect(await finish({ sessionId: session.id, turnId: t2, outcome: 'done' })).toBe(true)
    const [after] = await db.sql<{ status: string; durable_turn_id: string | null }[]>`SELECT status, durable_turn_id FROM ai_sessions WHERE id = ${session.id}`
    expect(after).toEqual({ status: 'idle', durable_turn_id: null })
  })
})
