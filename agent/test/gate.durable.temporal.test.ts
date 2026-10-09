import type { TestWorkflowEnvironment } from '@temporalio/testing'
import { WorkflowNotFoundError } from '@temporalio/common'
import { Worker } from '@temporalio/worker'
import { randomUUID } from 'node:crypto'
import { fileURLToPath } from 'node:url'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it } from 'vitest'
import type { Database } from '../src/db.js'
import { DurableGate, DurableUnavailable, sessionWorkflowId } from '../src/gate/durable.js'
import { durableRequestId } from '../src/gate/ids.js'
import { PendingInputSweep, temporalDescriber } from '../src/gate/sweep.js'
import { RespondRefusal } from '../src/gate/validate.js'
import { BROWSER_USER } from '../src/routes/approvals.js'
import { respond, RespondError, sessionPendingInput } from '../src/routes/pendingInput.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool } from '../src/tools/registry.js'
import { services } from './helpers/mcp.js'
import { TEST_DATABASE_URL, throwawayDatabase } from './support/postgres.js'
import { agentA, manager, tempPaths } from './support/sessions.js'
import { localTemporal, TEMPORAL_CLI, TEMPORAL_SKIP } from './support/temporal.js'

// The agent service's side of a durable session's gate (spec 2026-10-01 §6.6) against a
// Temporal dev server and a stand-in for DurableSession's handlers
// (support/gateWorkflows.ts): the Query, the respond Update and its refusals, a worker
// that is down, and the interrupt Signal that is recorded all the same.

const QUEUE = 'gate-standin'
const WORKFLOWS = fileURLToPath(new URL('./support/gateWorkflows.ts', import.meta.url))

describe.skipIf(!TEMPORAL_CLI)(`a durable session's gate${TEMPORAL_SKIP}`, () => {
  let env: TestWorkflowEnvironment
  let worker: Worker
  let running: Promise<void>
  beforeAll(async () => {
    env = await localTemporal()
    worker = await Worker.create({ connection: env.nativeConnection, taskQueue: QUEUE, workflowsPath: WORKFLOWS })
    running = worker.run()
  }, 120_000)
  afterAll(async () => {
    worker?.shutdown()
    await running?.catch(() => {})
    await env?.teardown()
  })

  async function park(sessionId = randomUUID(), queue = QUEUE) {
    const requestId = durableRequestId(sessionId, 'run-1', 'toolu_1')
    await env.client.workflow.start('gateStandIn', { workflowId: sessionWorkflowId(sessionId), taskQueue: queue, args: [requestId, 'approval'] })
    return { sessionId, requestId }
  }

  it('reads the parked entry, takes a response once, and passes the refusals on with their codes', async () => {
    const gate = new DurableGate(env.client)
    const { sessionId, requestId } = await park()
    expect(await gate.pendingInput(sessionId)).toEqual([expect.objectContaining({ id: requestId, kind: 'approval' })])
    const args = { request_id: requestId, response: { kind: 'approval', decision: 'approve' }, responder: BROWSER_USER, role: 'browser' as const }
    expect(await gate.respond(sessionId, args)).toEqual({ kind: 'approval', outcome: 'approved' })
    await expect(gate.respond(sessionId, args)).rejects.toMatchObject({ code: 'resolved' })
    await expect(gate.respond(sessionId, { ...args, request_id: `${requestId}x` })).rejects.toBeInstanceOf(RespondRefusal)
    expect(await gate.pendingInput(sessionId)).toEqual([])
    expect(await gate.cancelInput(sessionId, 'interrupted')).toBe('none')
  }, 60_000)

  it('cancels the parked entry with cancel_input', async () => {
    const gate = new DurableGate(env.client)
    const { sessionId } = await park()
    expect(await gate.cancelInput(sessionId, 'a new turn')).toBe('cancelled')
    expect(await gate.pendingInput(sessionId)).toEqual([])
  }, 60_000)

  it('says a session with no workflow is not found', async () => {
    await expect(new DurableGate(env.client).pendingInput(randomUUID())).rejects.toBeInstanceOf(WorkflowNotFoundError)
  }, 60_000)

  it('is unavailable, within its bound, while no worker answers; the interrupt Signal is recorded all the same', async () => {
    const gate = new DurableGate(env.client, { timeoutMs: 1_500, cancelTimeoutMs: 1_500 })
    const { sessionId, requestId } = await park(randomUUID(), 'gate-nobody-polls')
    const started = Date.now()
    await expect(
      gate.respond(sessionId, { request_id: requestId, response: { kind: 'approval', decision: 'deny' }, responder: BROWSER_USER, role: 'browser' }),
    ).rejects.toBeInstanceOf(DurableUnavailable)
    await expect(gate.cancelInput(sessionId, 'interrupted')).rejects.toBeInstanceOf(DurableUnavailable)
    expect(Date.now() - started).toBeLessThan(15_000)
    await gate.interrupt(sessionId, 'the user pressed stop')
    const history = await env.client.workflow.getHandle(sessionWorkflowId(sessionId)).fetchHistory()
    expect(history.events?.some((e) => e.workflowExecutionSignaledEventAttributes?.signalName === 'interrupt')).toBe(true)
    await env.client.workflow.getHandle(sessionWorkflowId(sessionId)).terminate('test')
  }, 60_000)

  describe.skipIf(!TEST_DATABASE_URL)('through the respond route and sessions_approve', () => {
    let db: Database
    let drop: () => Promise<void>
    beforeEach(async () => {
      ;({ db, drop } = await throwawayDatabase())
      expect(await db.ready()).toBe(true)
    })
    afterEach(async () => {
      await drop()
    })

    async function durableSession(owner = BROWSER_USER) {
      const m = manager({ sql: db.sql, paths: await tempPaths(), approvalGrants: async () => true })
      m.durable = new DurableGate(env.client)
      const sessionId = randomUUID()
      await db.sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
        VALUES (${sessionId}, 'mcp', ${owner.kind}, ${owner.id}, ${owner.label}, ${owner.kind}, ${owner.id}, 'running', 10, 1, 'durable')`
      const { requestId } = await park(sessionId)
      return { m, sessionId, requestId }
    }

    it('the browser user approves a durable entry through respond, and the per-session read asks the workflow', async () => {
      const { m, sessionId, requestId } = await durableSession(agentA)
      expect((await sessionPendingInput(m, BROWSER_USER, sessionId))?.entries).toEqual([expect.objectContaining({ id: requestId })])
      expect(await respond(m, BROWSER_USER, requestId, { kind: 'approval', decision: 'approve' })).toEqual({
        id: requestId,
        kind: 'approval',
        outcome: 'approved',
      })
      await expect(respond(m, BROWSER_USER, requestId, { kind: 'approval', decision: 'approve' })).rejects.toMatchObject({ status: 409 })
    }, 60_000)

    it('a grant holder that owns the session is the owner, and sessions_approve is refused before any Update', async () => {
      const { m, sessionId, requestId } = await durableSession(agentA)
      const tool = ALL_TOOLS.find((t) => t.name === 'sessions_approve')!
      const result = await runTool(tool, { approval_id: requestId }, {
        ...services({ sessions: m }),
        principal: { id: agentA.id, kind: 'bearer', tiers: ['read', 'write', 'outward'] },
        progress: async () => {},
        signal: new AbortController().signal,
      })
      expect(result.isError).toBe(true)
      expect(JSON.stringify(result.content)).toMatch(/approval grant is for approving another/)
      // Still parked: the refusal was the workflow's validator's, nothing was resolved.
      expect(await m.durable!.pendingInput(sessionId)).toHaveLength(1)
    }, 60_000)

    it("the orphan sweep removes a terminated run's row, and keeps a running one's", async () => {
      const { m, sessionId } = await durableSession()
      const live = await park()
      await db.sql`
        INSERT INTO ai_sessions (id, origin, owner_kind, owner_id, owner_label, creator_kind, creator_id, status, max_turns, budget_usd, mode)
        VALUES (${live.sessionId}, 'chat', 'browser', 'browser', 'You', 'browser', 'browser', 'running', 10, 1, 'durable')`
      const runOf = async (id: string) => (await env.client.workflow.getHandle(sessionWorkflowId(id)).describe()).runId
      for (const [id, run] of [
        [sessionId, await runOf(sessionId)],
        [live.sessionId, await runOf(live.sessionId)],
      ] as const) {
        await db.sql`
          INSERT INTO ai_pending_input (request_id, session_id, workflow_id, workflow_run_id, kind, tool, summary, responders, created_at, expires_at)
          VALUES (${durableRequestId(id, run, 'toolu_1')}, ${id}, ${sessionWorkflowId(id)}, ${run}, 'approval', 'print_output', '{}',
                  ${['browser', 'grant']}, now() - interval '1 hour', now() + interval '1 hour')`
      }
      await env.client.workflow.getHandle(sessionWorkflowId(sessionId)).terminate('test')
      const sweep = new PendingInputSweep({ sql: db.sql, events: m.events, describe: temporalDescriber(env.client) })
      expect(await sweep.sweep()).toBe(1)
      const left = await db.sql<{ session_id: string }[]>`SELECT session_id FROM ai_pending_input`
      expect(left.map((r) => r.session_id)).toEqual([live.sessionId])
      await env.client.workflow.getHandle(sessionWorkflowId(live.sessionId)).terminate('test')
    }, 60_000)

    it('a durable id for a classic session, an unknown session or a malformed id is stale', async () => {
      const { m, sessionId } = await durableSession()
      await db.sql`UPDATE ai_sessions SET mode = 'classic' WHERE id = ${sessionId}`
      for (const id of [durableRequestId(sessionId, 'run-1', 'toolu_1'), durableRequestId(randomUUID(), 'r', 't'), 'durable:x:y:z', 'flow:a:b:c']) {
        const err = await respond(m, BROWSER_USER, id, { kind: 'approval', decision: 'deny' }).catch((e: unknown) => e)
        expect(err, id).toBeInstanceOf(RespondError)
        expect((err as RespondError).status, id).toBe(404)
      }
    }, 60_000)
  })
})
