import { randomUUID } from 'node:crypto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import type { Owner } from '../src/sessions/protocol.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool } from '../src/tools/registry.js'
import { firstText, services } from './helpers/mcp.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'
import { agentA, agentB, manager, tempPaths } from './support/sessions.js'

// pending_input_list and sessions_pending_input (spec 2026-10-01 §6.6 "Reads", plan 5b
// ruling 10): what each principal sees, under the approvals' visibility rules.

type Page = { entries: { id: string; kind: string; requested_by: { kind: string; id?: string; label: string } | null }[] }

describe.skipIf(!TEST_DATABASE_URL)(`the pending-input read tools${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
  })
  afterEach(async () => {
    await drop()
  })

  async function setUp() {
    // agentA holds the approval grant; agentB does not.
    const m = manager({ sql: db.sql, paths: await tempPaths(), approvalGrants: async (p) => p.id === agentA.id })
    const prepare = (by: Owner, toolUseId: string) =>
      m.approvals.create({ sessionId: null, turnId: null, toolUseId, tool: 'print_output', input: { output: toolUseId }, tier: 'outward', requestedBy: by })
    const a = await prepare(agentA, 'ta')
    const b = await prepare(agentB, 'tb')
    // A durable session agentB owns, with an approval and a question parked.
    const durable = randomUUID()
    await db.sql`
      INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
      VALUES (${durable}, 'mcp', 'bearer', ${agentB.id}, ${agentB.label}, 'bearer', ${agentB.id}, 'waiting_approval', 10, 1, 'durable')`
    const ids = { approval: `durable:${durable}:run-1:t1`, answer: `durable:${durable}:run-1:t2` }
    await db.sql`
      INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, prompt, requested_by, responders, expires_at)
      VALUES (${ids.approval}, ${durable}, ${`session-${durable}`}, 'run-1', 'approval', 'print_output', '{}', '',
              ${db.sql.json(agentB)}, ${['browser', 'grant']}, now() + interval '1 hour'),
             (${ids.answer}, ${durable}, ${`session-${durable}`}, 'run-1', 'answer', 'ask_user', '', 'Which?',
              null, ${['browser']}, now() + interval '1 hour')`
    const call = async (who: Owner, name: string, args: Record<string, unknown> = {}) => {
      const result = await runTool(ALL_TOOLS.find((t) => t.name === name)!, args, {
        ...services({ sessions: m }),
        principal: { id: who.id, kind: 'bearer', tiers: ['read', 'write', 'outward'] },
        progress: async () => {},
        signal: new AbortController().signal,
      })
      return result
    }
    return { m, call, durable, ids, a: `approval:${a.id}`, b: `approval:${b.id}` }
  }

  it('a grant holder sees every approval, and the answers of no session it does not own', async () => {
    const { call, ids, a, b } = await setUp()
    const page = firstText(await call(agentA, 'pending_input_list')) as Page
    expect(page.entries.map((e) => e.id).sort()).toEqual([a, b, ids.approval].sort())
    // Another principal's requester is shown by label only.
    const theirs = page.entries.find((e) => e.id === ids.approval)!
    expect(theirs.requested_by).toMatchObject({ kind: 'bearer' })
    expect(theirs.requested_by).not.toHaveProperty('id')
  })

  it('a grant holder approves a classic entry by the id pending_input_list gave it', async () => {
    const { m, call, b } = await setUp()
    const page = firstText(await call(agentA, 'pending_input_list')) as Page
    expect(page.entries.map((e) => e.id)).toContain(b)
    const result = await call(agentA, 'sessions_approve', { approval_id: b })
    expect(result.isError ?? false, JSON.stringify(result.content)).toBe(false)
    expect((await m.approvals.list(agentA, { pending: true })).map((a) => `approval:${a.id}`)).not.toContain(b)
  })

  it('a principal without the grant sees its own requests and its own sessions, answers included', async () => {
    const { call, ids, b } = await setUp()
    const page = firstText(await call(agentB, 'pending_input_list')) as Page
    expect(page.entries.map((e) => e.id).sort()).toEqual([b, ids.approval, ids.answer].sort())
  })

  it("sessions_pending_input reads one session as the caller may see it, and hides one it may not", async () => {
    const { call, durable } = await setUp()
    // A durable session's read asks its workflow; with no Temporal configured it fails as retryable.
    const owned = await call(agentB, 'sessions_pending_input', { session_id: durable })
    expect(owned.isError).toBe(true)
    expect(JSON.stringify(owned.content)).toMatch(/Temporal/)
    const stranger = { kind: 'bearer', id: 'token:c', label: 'Agent C' } as const
    const hidden = await call(stranger, 'sessions_pending_input', { session_id: durable })
    expect(JSON.stringify(hidden.content)).toMatch(/no session/)
  })
})
