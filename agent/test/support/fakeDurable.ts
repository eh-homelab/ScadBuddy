import type { Client, WithStartWorkflowOperation } from '@temporalio/client'
import type {
  DurableMessage,
  DurableSendOptions,
  DurableSendResult,
  DurableSessionInput,
  DurableSessions,
  PendingCall,
} from '../../src/durable/client.js'

/**
 * DurableSessions without Temporal, for the manager's and the approval service's own
 * logic. The real client is test/durable.client.test.ts (a fake Client) and
 * test/durable.temporal.test.ts (a dev server).
 */
export class FakeDurable implements DurableSessions {
  readonly sends: { input: DurableSessionInput; message: DurableMessage }[] = []
  readonly reviews: { sessionId: string; toolUseId: string; approved: boolean; approver: string }[] = []
  readonly cancels: string[] = []
  readonly pendingCalls = new Map<string, PendingCall[]>()
  /** What `send` reports it started. */
  result: DurableSendResult = { started: 'fresh', resumedFresh: false }
  /** Thrown by `send` after its claim (a refusal, Temporal away). */
  sendError: unknown
  /** Thrown by `send` before its claim. */
  describeError: unknown
  /** `send` waits for this after its claim: no worker accepts the Update until it settles. */
  accepted: Promise<void> | undefined
  reviewError: unknown
  pendingError: unknown
  /** Whether an execution is running for `cancel`. */
  running = true

  async send(input: DurableSessionInput, message: DurableMessage, options: DurableSendOptions = {}): Promise<DurableSendResult> {
    if (this.describeError) throw this.describeError
    await options.beforeStart?.(this.result)
    this.sends.push({ input, message })
    await this.accepted
    if (this.sendError) throw this.sendError
    return this.result
  }

  async review(sessionId: string, toolUseId: string, approved: boolean, approver: string): Promise<void> {
    if (this.reviewError) throw this.reviewError
    this.reviews.push({ sessionId, toolUseId, approved, approver })
  }

  async pending(sessionId: string): Promise<PendingCall[]> {
    if (this.pendingError) throw this.pendingError
    return this.pendingCalls.get(sessionId) ?? []
  }

  async cancel(sessionId: string): Promise<boolean> {
    this.cancels.push(sessionId)
    return this.running
  }
}

export type FakeUpdate = {
  name: string
  options: { args: unknown[]; startWorkflowOperation: WithStartWorkflowOperation<never> }
}

/**
 * A Temporal Client with only what TemporalDurableSessions calls: one workflow ID in
 * `status` (undefined: not found), whose `result` a completed one answers with.
 */
export function fakeTemporalClient(options: {
  status?: string
  result?: unknown
  updateError?: Error
  reviewError?: Error
  /** Runs as the update-with-start is sent, before it answers. */
  onUpdate?: (update: FakeUpdate) => Promise<void>
  /** The execution chain (first run id) `describe` reports; `chainAfterUpdate` once a start was sent. */
  chain?: string
  chainAfterUpdate?: string
}) {
  const updates: FakeUpdate[] = []
  const reviews: { id: string; name: string; args: unknown[] }[] = []
  const cancelled: string[] = []
  const notFound = () => Object.assign(new Error('workflow not found'), { name: 'WorkflowNotFoundError' })
  const client = {
    connection: { withDeadline: <T>(_deadline: number, fn: () => Promise<T>) => fn() },
    workflow: {
      getHandle: (id: string) => ({
        describe: async () => {
          if (!options.status) throw notFound()
          const chain = (updates.length > 0 && options.chainAfterUpdate) || options.chain || 'run-1'
          return { status: { name: options.status }, runId: chain, raw: { workflowExecutionInfo: { firstRunId: chain } } }
        },
        result: async () => options.result,
        cancel: async () => {
          cancelled.push(id)
        },
        query: async () => [],
        executeUpdate: async (name: string, update: { args: unknown[] }) => {
          if (options.reviewError) throw options.reviewError
          reviews.push({ id, name, args: update.args })
        },
      }),
      executeUpdateWithStart: async (name: string, update: FakeUpdate['options']) => {
        updates.push({ name, options: update })
        await options.onUpdate?.({ name, options: update })
        if (options.updateError) throw options.updateError
      },
    },
  }
  return { client: client as unknown as Client, updates, reviews, cancelled }
}
