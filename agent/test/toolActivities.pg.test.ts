import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { PgSessionOwners } from '../src/temporal/toolActivities.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// Only a durable session's workflow may run tools as activities (spec 2026-10-01 §6.1,
// §6.3; #1055): the activity skips the approval its workflow made, so a classic
// session's id, whose outward calls park in ai_approvals, must not be borrowed.

describe.skipIf(!TEST_DATABASE_URL)(
  `tool activities' session owners${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`,
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

    it('answers only for a durable session, and a session is classic unless made durable', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      const owners = new PgSessionOwners(db.sql)
      expect(await owners.ownerOf(session.id)).toBeUndefined()
      await db.sql`UPDATE ai_sessions SET mode = 'durable' WHERE id = ${session.id}`
      expect(await owners.ownerOf(session.id)).toEqual(browser)
      await expect(db.sql`UPDATE ai_sessions SET mode = 'other' WHERE id = ${session.id}`).rejects.toThrow()
    })
  },
)
