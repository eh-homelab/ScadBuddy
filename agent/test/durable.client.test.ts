import {
  WithStartWorkflowOperation,
  WorkflowUpdateFailedError,
  WorkflowUpdateRPCTimeoutOrCancelledError,
} from '@temporalio/client'
import { ApplicationFailure } from '@temporalio/common'
import type { Sql } from 'postgres'
import { describe, expect, it } from 'vitest'
import {
  DURABLE_APPROVAL_PREFIX,
  DURABLE_SEND_DEADLINE_MS,
  DURABLE_TASK_QUEUE,
  DURABLE_WORKFLOW,
  durableApprovalId,
  DurableRefused,
  type DurableSessionInput,
  durableWorkflowId,
  parseDurableApprovalId,
  REVIEW_UPDATE,
  SEND_UPDATE,
  TemporalDurableSessions,
} from '../src/durable/client.js'
import { type FakeUpdate, fakeTemporalClient as fakeClient } from './support/fakeDurable.js'

// TemporalDurableSessions against a fake Client (#1056): the update-with-start it sends
// for each state of the session's workflow ID, and how a refusal comes back. The wire
// shape on a real server is test/durable.temporal.test.ts; snapshots, the offset reset
// and the lost-results line run on Postgres in test/chat.pg.test.ts.

const SID = '0b5c6a2e-8a4b-4d3c-9e1f-2a3b4c5d6e7f'
const input: DurableSessionInput = { session_id: SID, max_turns: 30, approval_expiry_seconds: 600, model: null }

/** No snapshot rows: a tagged template that answers every query with nothing. */
const noRows = ((..._args: unknown[]) => Promise.resolve([])) as unknown as Sql

function expectStart(update: FakeUpdate | undefined, state: unknown) {
  expect(update?.name).toBe(SEND_UPDATE)
  expect(update?.options.args).toEqual([{ text: 'hi', context: 'page' }])
  const operation = update?.options.startWorkflowOperation
  expect(operation).toBeInstanceOf(WithStartWorkflowOperation)
  expect(operation?.workflowTypeOrFunc).toBe(DURABLE_WORKFLOW)
  expect(operation?.options).toEqual({
    workflowId: `session-${SID}`,
    taskQueue: DURABLE_TASK_QUEUE,
    args: [input, state, null],
    workflowIdConflictPolicy: 'USE_EXISTING',
    workflowIdReusePolicy: 'ALLOW_DUPLICATE',
  })
}

describe('TemporalDurableSessions', () => {
  it('names the workflow and its queue as agent-durable does', () => {
    expect(DURABLE_WORKFLOW).toBe('DurableSession')
    expect(DURABLE_TASK_QUEUE).toBe('agent')
    expect(durableWorkflowId(SID)).toBe(`session-${SID}`)
  })

  it('attaches to a running execution with no state', async () => {
    const { client, updates } = fakeClient({ status: 'RUNNING' })
    const result = await new TemporalDurableSessions(client, noRows).send(input, { text: 'hi', context: 'page' })
    expect(result).toEqual({ started: 'attached', resumedFresh: false })
    expectStart(updates[0], null)
  })

  it('reports a new run when the running one closed before the start reached it', async () => {
    const { client, updates } = fakeClient({ status: 'RUNNING', chain: 'run-1', chainAfterUpdate: 'run-2' })
    const seen: unknown[] = []
    const result = await new TemporalDurableSessions(client, noRows).send(
      input,
      { text: 'hi', context: 'page' },
      { beforeStart: async (r) => void seen.push(r) },
    )
    expect(seen).toEqual([{ started: 'attached', resumedFresh: false }])
    expect(result).toEqual({ started: 'fresh', resumedFresh: true })
    expectStart(updates[0], null)
  })

  it('asks again when the describe after the start fails, and keeps attached when Temporal cannot say', async () => {
    const retried = fakeClient({ status: 'RUNNING', chain: 'run-1', chainAfterUpdate: 'run-2', failDescribes: [2] })
    expect(await new TemporalDurableSessions(retried.client, noRows).send(input, { text: 'hi', context: 'page' })).toEqual({
      started: 'fresh',
      resumedFresh: true,
    })
    const unknown = fakeClient({ status: 'RUNNING', chain: 'run-1', chainAfterUpdate: 'run-2', failDescribes: [2, 3] })
    expect(await new TemporalDurableSessions(unknown.client, noRows).send(input, { text: 'hi', context: 'page' })).toEqual({
      started: 'attached',
      resumedFresh: false,
    })
  })

  it('gives the update-with-start a deadline: an RPC that hangs is aborted after it', async () => {
    const { client, signals } = fakeClient({ status: 'RUNNING', hangUpdate: true })
    const durable = new TemporalDurableSessions(client, noRows, { sendDeadlineMs: 50 })
    await expect(durable.send(input, { text: 'hi', context: 'page' })).rejects.toBeInstanceOf(
      WorkflowUpdateRPCTimeoutOrCancelledError,
    )
    // The describes before it carry timeout signals of their own; the update's is the last.
    expect(signals.at(-1)?.aborted).toBe(true)
    expect(DURABLE_SEND_DEADLINE_MS).toBe(30_000)
  })

  it('starts an unknown ID with no state, and says it is fresh rather than resumed', async () => {
    const { client, updates } = fakeClient({})
    const result = await new TemporalDurableSessions(client, noRows).send(input, { text: 'hi', context: 'page' })
    expect(result).toEqual({ started: 'fresh', resumedFresh: false })
    expectStart(updates[0], null)
  })

  it("hands a completed execution's result to the next one", async () => {
    const state = { session_id: 'claude-1', checkpoint: 'e-9', fork_next: true, pending: {} }
    const { client, updates } = fakeClient({ status: 'COMPLETED', result: state })
    const seen: unknown[] = []
    const result = await new TemporalDurableSessions(client, noRows).send(
      input,
      { text: 'hi', context: 'page' },
      { beforeStart: async (r) => void seen.push(r) },
    )
    expect(result).toEqual({ started: 'handed_over', resumedFresh: false })
    expect(seen).toEqual([result])
    expectStart(updates[0], state)
  })

  it('says a closed execution with neither a result nor a snapshot resumed fresh', async () => {
    const { client, updates } = fakeClient({ status: 'TERMINATED' })
    expect(await new TemporalDurableSessions(client, noRows).send(input, { text: 'hi', context: 'page' })).toEqual({
      started: 'fresh',
      resumedFresh: true,
    })
    expectStart(updates[0], null)
  })

  it('sends nothing when the claim before it throws', async () => {
    const { client, updates } = fakeClient({ status: 'RUNNING' })
    const durable = new TemporalDurableSessions(client, noRows)
    await expect(
      durable.send(input, { text: 'hi', context: null }, { beforeStart: () => Promise.reject(new Error('busy')) }),
    ).rejects.toThrow('busy')
    expect(updates).toEqual([])
  })

  it("turns a validator's refusal into DurableRefused with its message", async () => {
    const refused = new WorkflowUpdateFailedError('Workflow Update failed', ApplicationFailure.create({ message: 'the session is busy' }))
    const { client } = fakeClient({ status: 'RUNNING', updateError: refused })
    const sent = new TemporalDurableSessions(client, noRows).send(input, { text: 'hi', context: null })
    await expect(sent).rejects.toBeInstanceOf(DurableRefused)
    await expect(sent).rejects.toThrow('the session is busy')
  })

  it('reviews with the approver string, and a refused review is DurableRefused', async () => {
    const { client, reviews } = fakeClient({ status: 'RUNNING' })
    await new TemporalDurableSessions(client, noRows).review(SID, 'toolu_1', true, 'browser:browser')
    expect(reviews).toEqual([{ id: `session-${SID}`, name: REVIEW_UPDATE, args: ['toolu_1', true, 'browser:browser'] }])
    const refused = new WorkflowUpdateFailedError('Workflow Update failed', ApplicationFailure.create({ message: 'Tool call toolu_1 was already decided' }))
    const again = new TemporalDurableSessions(fakeClient({ status: 'RUNNING', reviewError: refused }).client, noRows)
    await expect(again.review(SID, 'toolu_1', true, 'browser:browser')).rejects.toThrow(
      new DurableRefused('Tool call toolu_1 was already decided'),
    )
  })

  it('cancels only a running execution', async () => {
    const running = fakeClient({ status: 'RUNNING' })
    expect(await new TemporalDurableSessions(running.client, noRows).cancel(SID)).toBe(true)
    expect(running.cancelled).toEqual([`session-${SID}`])
    const closed = fakeClient({ status: 'COMPLETED' })
    expect(await new TemporalDurableSessions(closed.client, noRows).cancel(SID)).toBe(false)
    expect(closed.cancelled).toEqual([])
    expect(await new TemporalDurableSessions(fakeClient({}).client, noRows).cancel(SID)).toBe(false)
  })
})

describe('durable approval ids', () => {
  it('round-trips', () => {
    const id = durableApprovalId(SID, 'toolu_01ABC:x')
    expect(id).toBe(`${DURABLE_APPROVAL_PREFIX}${SID}:toolu_01ABC:x`)
    expect(parseDurableApprovalId(id)).toEqual({ sessionId: SID, toolUseId: 'toolu_01ABC:x' })
  })

  it('rejects ids without the prefix, with a non-uuid session, or with no tool use', () => {
    expect(parseDurableApprovalId(`${SID}:toolu_1`)).toBeUndefined()
    expect(parseDurableApprovalId('3f2b9c4e-1d2a-4b5c-8d9e-0f1a2b3c4d5e')).toBeUndefined()
    expect(parseDurableApprovalId('durable:not-a-uuid:toolu_1')).toBeUndefined()
    expect(parseDurableApprovalId(`durable:${SID}`)).toBeUndefined()
    expect(parseDurableApprovalId(`durable:${SID}:`)).toBeUndefined()
  })
})
