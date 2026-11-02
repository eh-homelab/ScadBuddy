import { randomUUID } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { PgApprovalRecords, PgSessionOwners } from '../src/temporal/toolActivities.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { browser, manager, scriptedRunner, tempPaths } from './support/sessions.js'

// Only a durable session's workflow may run tools as activities (spec 2026-10-01 §6.1,
// §6.3; #1055): the activity skips the approval its workflow made, so a classic
// session's id, whose outward calls park in ai_approvals, must not be borrowed.

const BACKEND_MIGRATIONS = fileURLToPath(new URL('./fixtures/backend-flow-migrations/', import.meta.url))

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

    it('answers for every session, a classic one an earlier release left included (plan Step 6.3)', async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      const owners = new PgSessionOwners(db.sql)
      expect(await owners.ownerOf(randomUUID())).toBeUndefined()
      expect(await owners.ownerOf(session.id)).toEqual({ ...browser, turnTiers: null, turnId: null })
      await db.sql`UPDATE ai_sessions SET runtime = NULL WHERE id = ${session.id}`
      expect(await owners.ownerOf(session.id)).toEqual({ ...browser, turnTiers: null, turnId: null })
      // The tiers its current turn was sent with (#2320), only while the sender owns it.
      const sent = (by: { kind: string; id: string }, tiers: string[]) =>
        db.sql`UPDATE ai_sessions SET turn_tiers = ${db.sql.json({ by, tiers })} WHERE id = ${session.id}`
      await sent({ kind: 'browser', id: 'browser' }, ['read', 'write', 'admin'])
      expect(await owners.ownerOf(session.id)).toEqual({ ...browser, turnTiers: ['read', 'write'], turnId: null })
      await sent({ kind: 'bearer', id: 'token:old-owner' }, ['read', 'write', 'outward'])
      expect(await owners.ownerOf(session.id)).toEqual({ ...browser, turnTiers: null, turnId: null })
      // The durable turn running now, for its calls' audit rows (#2497).
      const turn = '6a1f2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d'
      await db.sql`UPDATE ai_sessions SET durable_turn_id = ${turn} WHERE id = ${session.id}`
      expect((await owners.ownerOf(session.id))?.turnId).toBe(turn)
      await expect(db.sql`UPDATE ai_sessions SET runtime = 'other' WHERE id = ${session.id}`).rejects.toThrow()
    })

    it("finds a call's approval only as an approved outcome of that request id", async () => {
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      const id = (call: string) => `durable:${session.id}:run-1:${call}`
      const record = (call: string, kind: string, outcome: string) => db.sql`
        INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
        VALUES (${id(call)}, ${session.id}, ${kind}, ${outcome}, ${db.sql.json({ kind: 'browser', id: 'browser', label: 'You' })})`
      // resolve_input's audit row for an approval (gate/store.py `_audit`): its input hash.
      const audited = (call: string, action: string, hash: string) => db.sql`
        INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label,
                              session_id, tool_use_id, tier, input_hash, input_summary, request_id,
                              outcome, detail, started_at, finished_at, duration_ms)
        VALUES ('approval', ${action}, 'http', 'browser', 'browser', 'You', ${session.id}, ${call}, 'outward',
                ${hash}, '{}', ${id(call)}, 'ok', '', now(), now(), 0)`
      await record('a', 'approval', 'approved')
      await audited('a', 'approved', 'h-a')
      await record('d', 'approval', 'denied')
      await audited('d', 'denied', 'h-d')
      await record('q', 'answer', 'answered')
      await record('u', 'approval', 'approved') // no audit row: nothing binds it to a call
      const approvals = new PgApprovalRecords(db.sql)
      expect(await approvals.approved(id('a'), { inputHash: 'h-a' })).toBe(true)
      // #2088: only for the call it was given for, and never without the call's hash.
      expect(await approvals.approved(id('a'), { inputHash: 'h-other' })).toBe(false)
      expect(await approvals.approved(id('a'))).toBe(false)
      expect(await approvals.approved(id('u'), { inputHash: 'h-a' })).toBe(false)
      expect(await approvals.approved(id('d'), { inputHash: 'h-d' })).toBe(false)
      expect(await approvals.approved(id('q'), { inputHash: 'h-a' })).toBe(false)
      expect(await approvals.approved(id('none'), { inputHash: 'h-a' })).toBe(false)
      expect(await approvals.approved(`durable:${session.id}:run-2:a`, { inputHash: 'h-a' })).toBe(false)
    })

    // #2481 (plan 2026-10-11 Task A4): a child session's call a run grant approved
    // (responder kind `run_grant`) runs only while that grant is in force.
    it("approves a run grant's call only while the grant is in force", async () => {
      for (const file of readdirSync(BACKEND_MIGRATIONS).sort()) await db.sql.unsafe(readFileSync(`${BACKEND_MIGRATIONS}${file}`, 'utf8'))
      const m = manager({ sql: db.sql, paths: await tempPaths(), run: scriptedRunner(() => ({ reply: 'ok' })).runner })
      const { session } = await m.start(browser, { origin: 'chat', title: 't' })
      await db.sql`
        INSERT INTO flow_grants (id, run_id, status, spec, requested_by, approved_by, approved_at, expires_at)
        VALUES ('g1', 'r1', 'approved', '{}', '{"kind": "browser"}', 'browser', now(), now() + interval '1 hour')`
      const id = `durable:${session.id}:run-1:a`
      await db.sql`
        INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
        VALUES (${id}, ${session.id}, 'approval', 'approved', ${db.sql.json({ kind: 'run_grant', id: 'g1', label: 'Run grant' })})`
      await db.sql`
        INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label,
                              session_id, tool_use_id, tier, input_hash, input_summary, request_id,
                              outcome, detail, started_at, finished_at, duration_ms)
        VALUES ('approval', 'approved', 'http', 'browser', 'browser', 'You', ${session.id}, 'a', 'outward',
                'h-a', '{}', ${id}, 'ok', '', now(), now(), 0)`
      const approvals = new PgApprovalRecords(db.sql)
      expect(await approvals.approved(id, { inputHash: 'h-a' })).toBe(true)
      expect(await approvals.lapsed(id, { inputHash: 'h-a' })).toBe(false)
      await db.sql`UPDATE flow_grants SET revoked_at = now(), revoked_by = 'browser'`
      expect(await approvals.approved(id, { inputHash: 'h-a' })).toBe(false)
      expect(await approvals.lapsed(id, { inputHash: 'h-a' })).toBe(true)
      expect(await approvals.lapsed(id, { inputHash: 'h-other' })).toBe(false)
      await db.sql`UPDATE flow_grants SET revoked_at = NULL, expires_at = now() - interval '1 second'`
      expect(await approvals.approved(id, { inputHash: 'h-a' })).toBe(false)
      expect(await approvals.lapsed(id, { inputHash: 'h-a' })).toBe(true)
    })
  },
)
