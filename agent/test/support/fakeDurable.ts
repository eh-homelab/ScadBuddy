import { type Client, type WithStartWorkflowOperation, WorkflowUpdateRPCTimeoutOrCancelledError } from '@temporalio/client'
import type {
  DurableSendOptions,
  DurableSendResult,
  DurableSessionInput,
  DurableSessions,
  PendingCall,
} from '../../src/durable/client.js'
import { DurableStopped } from '../../src/durable/client.js'

/**
 * DurableSessions without Temporal, for the manager's and the approval service's own
 * logic. The real client is test/durable.client.test.ts (a fake Client) and
 * test/durable.temporal.test.ts (a dev server).
 */
export class FakeDurable implements DurableSessions {
  readonly sends: { input: DurableSessionInput; messageId: string }[] = []
  readonly reviews: { sessionId: string; toolUseId: string; approved: boolean; approver: string }[] = []
  readonly cancels: string[] = []
  readonly pendingCalls = new Map<string, PendingCall[]>()
  /** What `send` reports it started. */
  result: DurableSendResult = { started: 'fresh', resumedFresh: false }
  /** Thrown by an attempt (a refusal; any other error is retried when `send` has `attempt`). */
  sendError: unknown
  /** Thrown by the first `failures` attempts before they send (Temporal away). */
  attemptError: unknown
  failures = 0
  /** `send` waits for this before it sends: a stopped run still closing. A Stop ends the wait. */
  closing: Promise<void> | undefined
  /** `send` waits for this after it sent: no worker accepts the Update until it settles. */
  accepted: Promise<void> | undefined
  reviewError: unknown
  pendingError: unknown
  /** Sessions whose Query never answers. */
  readonly hangPending = new Set<string>()
  pendingAsked = 0
  /** Whether an execution is running for `cancel`. */
  running = true
  attempts = 0

  async send(input: DurableSessionInput, messageId: string, options: DurableSendOptions = {}): Promise<DurableSendResult> {
    for (;;) {
      if (options.signal?.aborted) throw new DurableStopped('the session was stopped before this message was sent')
      if (options.attempt && !(await options.attempt())) throw new DurableStopped('the message is no longer waiting to be sent')
      this.attempts += 1
      try {
        return await this.#attempt(input, messageId, options)
      } catch (err) {
        if (!options.attempt || err === this.sendError || err instanceof DurableStopped) throw err
        options.retrying?.(err)
      }
      await new Promise((resolve) => setTimeout(resolve, 5))
    }
  }

  async #attempt(input: DurableSessionInput, messageId: string, options: DurableSendOptions): Promise<DurableSendResult> {
    if (this.failures > 0) {
      this.failures -= 1
      throw this.attemptError
    }
    if (this.closing) {
      const signal = options.signal
      await Promise.race([
        this.closing,
        new Promise<void>((resolve) => signal?.addEventListener('abort', () => resolve(), { once: true })),
      ])
      if (options.attempt && !(await options.attempt())) throw new DurableStopped('the message is no longer waiting to be sent')
    }
    await options.beforeStart?.(this.result)
    if (options.signal?.aborted) throw new DurableStopped('the session was stopped before this message was sent')
    options.starting?.()
    this.sends.push({ input, messageId })
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
    if (this.hangPending.has(sessionId)) await new Promise(() => {})
    this.pendingAsked += 1
    return this.pendingCalls.get(sessionId) ?? []
  }

  async cancel(sessionId: string): Promise<boolean> {
    this.cancels.push(sessionId)
    return this.running
  }
}

export type FakeUpdate = {
  name: string
  options: { args: unknown[]; updateId?: string; startWorkflowOperation: WithStartWorkflowOperation<never> }
}

/**
 * A Temporal Client with only what TemporalDurableSessions calls: one workflow ID in
 * `status` (undefined: not found), whose `result` a completed one answers with.
 */
/** Rejects as an aborted gRPC call does once `signal` aborts. */
async function aborted(signal: AbortSignal): Promise<never> {
  await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
  throw Object.assign(new Error('1 CANCELLED: the call was cancelled'), { code: 1 })
}

export function fakeTemporalClient(options: {
  status?: string
  result?: unknown
  updateError?: Error
  /** Thrown by the first update-with-starts, one each, before `updateError`. */
  updateErrors?: Error[]
  reviewError?: Error
  /** A completed execution's `result`, and `review`, never answer by themselves: only their abort signal ends them. */
  hangResult?: boolean
  hangReview?: boolean
  /** Runs as the update-with-start is sent, before it answers. */
  onUpdate?: (update: FakeUpdate) => Promise<void>
  /** The execution chain (first run id) `describe` reports; `chainAfterUpdate` once a start was sent. */
  chain?: string
  chainAfterUpdate?: string
  /** `describe` calls (1-based) that fail as an unreachable Temporal would. */
  failDescribes?: number[]
  /** The update-with-start never answers by itself: only its abort signal ends it, as gRPC does. */
  hangUpdate?: boolean
}) {
  const updates: FakeUpdate[] = []
  const reviews: { id: string; name: string; args: unknown[] }[] = []
  const cancelled: string[] = []
  let described = 0
  const signals: AbortSignal[] = []
  const notFound = () => Object.assign(new Error('workflow not found'), { name: 'WorkflowNotFoundError' })
  const client = {
    connection: {
      withDeadline: <T>(_deadline: number, fn: () => Promise<T>) => fn(),
      withAbortSignal: <T>(signal: AbortSignal, fn: () => Promise<T>) => {
        signals.push(signal)
        return fn()
      },
    },
    workflow: {
      getHandle: (id: string) => ({
        describe: async () => {
          described += 1
          if (options.failDescribes?.includes(described)) throw Object.assign(new Error('14 UNAVAILABLE'), { code: 14 })
          if (!options.status) throw notFound()
          const chain = (updates.length > 0 && options.chainAfterUpdate) || options.chain || 'run-1'
          return { status: { name: options.status }, runId: chain, raw: { workflowExecutionInfo: { firstRunId: chain } } }
        },
        result: async () => {
          const signal = signals.at(-1)
          if (options.hangResult && signal) await aborted(signal)
          return options.result
        },
        cancel: async () => {
          cancelled.push(id)
        },
        query: async () => [],
        executeUpdate: async (name: string, update: { args: unknown[] }) => {
          const signal = signals.at(-1)
          if (options.hangReview && signal) await aborted(signal)
          if (options.reviewError) throw options.reviewError
          reviews.push({ id, name, args: update.args })
        },
      }),
      executeUpdateWithStart: async (name: string, update: FakeUpdate['options']) => {
        updates.push({ name, options: update })
        await options.onUpdate?.({ name, options: update })
        const next = options.updateErrors?.shift()
        if (next) throw next
        if (options.updateError) throw options.updateError
        const signal = signals.at(-1)
        if (options.hangUpdate && signal) {
          await new Promise<void>((resolve) => signal.addEventListener('abort', () => resolve(), { once: true }))
          throw new WorkflowUpdateRPCTimeoutOrCancelledError('Workflow update call timeout or cancelled')
        }
      },
    },
  }
  return { client: client as unknown as Client, updates, reviews, cancelled, signals }
}
