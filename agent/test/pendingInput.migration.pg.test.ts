import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The tool-call gate's durable tables (spec 2026-10-01 §6.6): the projection
// `ai_pending_input`, the outcomes `ai_input_responses`, and `ai_audit.request_id`.

describe.skipIf(!TEST_DATABASE_URL)(`the pending-input tables${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
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
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
      VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10, 1, 'durable')`
    return id
  }

  const pending = (sessionId: string, requestId: string, over: Record<string, unknown> = {}) => ({
    request_id: requestId,
    session_id: sessionId,
    workflow_id: `session-${sessionId}`,
    workflow_run_id: 'run-1',
    kind: 'approval',
    tool: 'print_output',
    summary: '{"output":"x"}',
    input_hash: 'a'.repeat(64),
    responders: ['browser', 'grant'],
    expires_at: new Date(Date.now() + 600_000),
    ...over,
  })

  it('holds an entry, its outcome and an audit row that joins them, and goes with its session', async () => {
    const s = await session()
    const id = `durable:${s}:run-1:toolu_1`
    await db.sql`INSERT INTO ai_pending_input ${db.sql(pending(s, id))}`
    await db.sql`
      INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
      VALUES (${id}, ${s}, 'approval', 'approved', ${db.sql.json({ kind: 'browser', id: 'browser', label: 'You' })})`
    await db.sql`
      INSERT INTO ai_audit (kind, action, surface, principal_kind, principal_id, principal_label, outcome, request_id)
      VALUES ('approval', 'approved', 'http', 'browser', 'browser', 'You', 'ok', ${id})`
    const [audit] = await db.sql<{ request_id: string }[]>`SELECT request_id FROM ai_audit`
    expect(audit?.request_id).toBe(id)

    await db.sql`DELETE FROM ai_sessions WHERE id = ${s}`
    expect(await db.sql`SELECT 1 FROM ai_pending_input`).toHaveLength(0)
    expect(await db.sql`SELECT 1 FROM ai_input_responses`).toHaveLength(0)
  })

  it('refuses an unknown kind or outcome, and an entry that waits past the 86 400 s ceiling', async () => {
    const s = await session()
    await expect(db.sql`INSERT INTO ai_pending_input ${db.sql(pending(s, 'durable:a', { kind: 'other' }))}`).rejects.toThrow(/check/)
    await expect(
      db.sql`INSERT INTO ai_pending_input ${db.sql(pending(s, 'durable:b', { expires_at: new Date(Date.now() + 90_000_000) }))}`,
    ).rejects.toThrow(/check/)
    await expect(
      db.sql`
        INSERT INTO ai_input_responses (request_id, session_id, kind, outcome, responder)
        VALUES ('durable:c', ${s}, 'answer', 'maybe', '{}'::jsonb)`,
    ).rejects.toThrow(/check/)
  })
})
