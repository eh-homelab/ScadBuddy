import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { AuditEntry } from '../src/audit/log.js'
import type { Database } from '../src/db.js'
import { durableRequestId } from '../src/gate/ids.js'
import { ORPHAN_REASON, PendingInputSweep, type RunState } from '../src/gate/sweep.js'
import { EventLog } from '../src/sessions/eventLog.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// The orphan sweep of ai_pending_input (spec 2026-10-01 §6.6, plan 5b Task 9), with a
// fake describer standing in for Temporal.

describe.skipIf(!TEST_DATABASE_URL)(`the pending-input orphan sweep${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
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
      VALUES (${id}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'waiting_approval', 10, 1, 'durable')`
    return id
  }

  async function park(sessionId: string, runId: string, toolUseId: string, kind: 'approval' | 'answer', ageS = 3600): Promise<string> {
    const id = durableRequestId(sessionId, runId, toolUseId)
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, input_hash, prompt,
                                    requested_by, responders, created_at, expires_at)
      VALUES (${id}, ${sessionId}, ${`session-${sessionId}`}, ${runId}, ${kind}, ${kind === 'approval' ? 'print_output' : 'ask_user'},
              '{}', ${kind === 'approval' ? 'a'.repeat(64) : null}, ${kind === 'answer' ? 'Which?' : ''},
              ${kind === 'approval' ? db.sql.json({ kind: 'browser', id: 'browser', label: 'You' }) : null},
              ${kind === 'approval' ? ['browser', 'grant'] : ['browser']},
              now() - make_interval(secs => ${ageS}), now() - make_interval(secs => ${ageS}) + interval '1 day')`
    return id
  }

  function sweeper(states: Record<string, RunState | Error>) {
    const described: string[] = []
    const audits: AuditEntry[] = []
    const events = new EventLog(db.sql)
    const sweep = new PendingInputSweep({
      sql: db.sql,
      events,
      audit: { record: async (e) => void audits.push(e) },
      describe: async (_workflowId, runId) => {
        described.push(runId)
        const state = states[runId] ?? 'closed'
        if (state instanceof Error) throw state
        return state
      },
    })
    return { sweep, described, audits, events }
  }

  it("cancels a closed run's rows once, with one input.resolved each, and the session goes idle", async () => {
    const s = await session()
    const approval = await park(s, 'run-dead', 'toolu_1', 'approval')
    const answer = await park(s, 'run-dead', 'toolu_2', 'answer')
    const { sweep, described, audits, events } = sweeper({})
    expect(await sweep.sweep()).toBe(2)
    expect(described).toEqual(['run-dead'])
    expect(await db.sql`SELECT 1 FROM ai_pending_input`).toHaveLength(0)
    const outcomes = await db.sql<{ request_id: string; outcome: string; reason: string; responder: { kind: string } }[]>`
      SELECT request_id, outcome, reason, responder FROM ai_input_responses ORDER BY request_id`
    expect(outcomes.map((r) => [r.request_id, r.outcome, r.reason, r.responder.kind])).toEqual(
      [
        [approval, 'cancelled', ORPHAN_REASON, 'system'],
        [answer, 'cancelled', ORPHAN_REASON, 'system'],
      ].sort((a, b) => a[0]!.localeCompare(b[0]!)),
    )
    const log = (await events.read(s)).map((e) => e.event)
    expect(log.filter((e) => e.type === 'input.resolved')).toEqual([
      expect.objectContaining({ id: approval, kind: 'approval', outcome: 'cancelled', reason: ORPHAN_REASON }),
      expect.objectContaining({ id: answer, kind: 'answer', outcome: 'cancelled', reason: ORPHAN_REASON }),
    ])
    expect(log).toContainEqual(expect.objectContaining({ type: 'approval.resolved', id: approval, decision: 'cancelled' }))
    expect(log).toContainEqual(expect.objectContaining({ type: 'question.resolved', id: answer, answered: false }))
    expect(log.filter((e) => e.type === 'session.status')).toEqual([expect.objectContaining({ status: 'idle' })])
    const [row] = await db.sql<{ status: string }[]>`SELECT status FROM ai_sessions WHERE id = ${s}`
    expect(row!.status).toBe('idle')
    expect(audits).toEqual([expect.objectContaining({ kind: 'approval', action: 'cancelled', requestId: approval, toolUseId: 'toolu_1' })])
    // Nothing left: a second pass writes nothing more.
    expect(await sweep.sweep()).toBe(0)
  })

  it("keeps an open run's rows, stamps them, and does not describe them again within the recheck", async () => {
    const s = await session()
    await park(s, 'run-live', 'toolu_1', 'approval')
    const { sweep, described } = sweeper({ 'run-live': 'open' })
    expect(await sweep.sweep()).toBe(0)
    expect(await sweep.sweep()).toBe(0)
    expect(described).toEqual(['run-live'])
    const [row] = await db.sql<{ last_checked_at: Date | null }[]>`SELECT last_checked_at FROM ai_pending_input`
    expect(row!.last_checked_at).not.toBeNull()
  })

  it('does not describe a young row, and leaves a row whose describe failed', async () => {
    const s = await session()
    await park(s, 'run-young', 'toolu_1', 'approval', 5)
    await park(s, 'run-flaky', 'toolu_2', 'approval')
    const { sweep, described } = sweeper({ 'run-flaky': new Error('Temporal is down') })
    expect(await sweep.sweep()).toBe(0)
    expect(described).toEqual(['run-flaky'])
    expect(await db.sql`SELECT 1 FROM ai_pending_input`).toHaveLength(2)
  })

  it('writes nothing for a row the workflow resolved first (the guarded delete)', async () => {
    const s = await session()
    const id = await park(s, 'run-dead', 'toolu_1', 'approval')
    const events = new EventLog(db.sql)
    const sweep = new PendingInputSweep({
      sql: db.sql,
      events,
      // The workflow's resolve_input wins the race while the run is described.
      describe: async () => {
        await db.sql`DELETE FROM ai_pending_input WHERE request_id = ${id}`
        return 'closed'
      },
    })
    expect(await sweep.sweep()).toBe(0)
    expect(await db.sql`SELECT 1 FROM ai_input_responses`).toHaveLength(0)
    expect((await events.read(s)).filter((e) => e.event.type === 'input.resolved')).toHaveLength(0)
  })
})
