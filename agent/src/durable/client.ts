import {
  type Client,
  isGrpcServiceError,
  WithStartWorkflowOperation,
  WorkflowUpdateFailedError,
  WorkflowUpdateRPCTimeoutOrCancelledError,
} from '@temporalio/client'
import type { Sql } from 'postgres'
import { isUuid } from '../harness/stateDirs.js'

// The agent service's side of durable sessions (spec 2026-10-01 §6, plan task 12, #1056):
// the DurableSession workflow that agent-durable runs (agent-durable/scadbuddy_durable/
// workflow.py), reached by name. TypeScript cannot import the Python, so its names are
// copied here (models.py).

export const DURABLE_WORKFLOW = 'DurableSession'
export const DURABLE_TASK_QUEUE = 'agent'
export const SEND_UPDATE = 'send_message'
export const REVIEW_UPDATE = 'review'
export const PENDING_QUERY = 'pending_approvals'
export const durableWorkflowId = (sessionId: string): string => `session-${sessionId}`

/** A durable approval's id in `approval.required` (plan ruling 7; translate.py `durable_approval_id`). */
export const DURABLE_APPROVAL_PREFIX = 'durable:'

export function durableApprovalId(sessionId: string, toolUseId: string): string {
  return `${DURABLE_APPROVAL_PREFIX}${sessionId}:${toolUseId}`
}

export function parseDurableApprovalId(id: string): { sessionId: string; toolUseId: string } | undefined {
  if (!id.startsWith(DURABLE_APPROVAL_PREFIX)) return undefined
  const rest = id.slice(DURABLE_APPROVAL_PREFIX.length)
  const colon = rest.indexOf(':')
  if (colon < 0) return undefined
  const sessionId = rest.slice(0, colon)
  const toolUseId = rest.slice(colon + 1)
  if (!isUuid(sessionId) || toolUseId === '') return undefined
  return { sessionId, toolUseId }
}

/** models.py `InFlight`: a call of the unanswered batch when the snapshot was taken. */
export type InFlightCall = { id: string; name: string; status: string }

/** models.py `SessionInput`. `restored` is sent only when the run resumes from a snapshot. */
export type DurableSessionInput = {
  session_id: string
  max_turns: number
  approval_expiry_seconds: number
  model: string | null
  restored?: { in_flight: InFlightCall[] }
}

/** models.py `Message`: `context` is model-only (the panel's page context). */
export type DurableMessage = { text: string; context: string | null }

/** A validator refused the Update ("the session is busy", "No tool call … is waiting for approval"). */
export class DurableRefused extends Error {
  override name = 'DurableRefused'
}

/** Temporal did not answer: nothing was started. */
export class DurableUnavailable extends Error {
  override name = 'DurableUnavailable'
}

/**
 * How the execution that takes the message was found: the one running (`attached`), a new
 * one from the last execution's result (`handed_over`, after a Stop), from the latest
 * snapshot (`restored`, after a terminate or a failure), or from nothing (`fresh`).
 * `resumedFresh`: an earlier execution existed but left neither (its conversation is lost).
 */
export type DurableSendResult = { started: 'attached' | 'handed_over' | 'restored' | 'fresh'; resumedFresh: boolean }

export type DurableSendOptions = {
  /**
   * Runs once the execution is chosen and before anything is sent: the manager's claim
   * (and, for a new execution, its `ai_durable_streams.next_offset` reset, plan ruling 9).
   * When it throws, nothing is sent.
   */
  beforeStart?: (result: DurableSendResult) => Promise<void>
}

export type PendingCall = { id: string; name: string; input: unknown }

export interface DurableSessions {
  send(input: DurableSessionInput, message: DurableMessage, options?: DurableSendOptions): Promise<DurableSendResult>
  review(sessionId: string, toolUseId: string, approved: boolean, approver: string): Promise<void>
  pending(sessionId: string): Promise<PendingCall[]>
  /** Stop: cancels the running execution; false when none is running. */
  cancel(sessionId: string): Promise<boolean>
}

/** How long a describe or a query may take before Temporal counts as unreachable. */
const ASK_MS = 10_000

/** The model-only line for calls whose results a snapshot restore lost (deviation 4, point 3). */
export function lostResultsLine(calls: { id: string; name: string }[]): string | undefined {
  if (calls.length === 0) return undefined
  return (
    "These tool calls ran after this session's last saved point, and their results were lost: " +
    calls.map((c) => `${c.name} (${c.id})`).join(', ')
  )
}

function notFound(err: unknown): boolean {
  return (err as Error | undefined)?.name === 'WorkflowNotFoundError'
}

function unreachable(err: unknown): boolean {
  const cause = (err as { cause?: unknown } | undefined)?.cause
  const grpc = isGrpcServiceError(err) ? err : isGrpcServiceError(cause) ? cause : undefined
  return grpc !== undefined && (grpc.code === 4 || grpc.code === 14)
}

export class TemporalDurableSessions implements DurableSessions {
  readonly #client: Client
  readonly #sql: Sql

  /** `sql` reads ai_durable_snapshots and the audit's tool calls for a restore. */
  constructor(client: Client, sql: Sql) {
    this.#client = client
    this.#sql = sql
  }

  async #ask<T>(work: () => Promise<T>): Promise<T> {
    return this.#client.connection.withDeadline(Date.now() + ASK_MS, work)
  }

  /** The execution's status name, or undefined when the ID has none. */
  async #status(sessionId: string): Promise<string | undefined> {
    try {
      const described = await this.#ask(() => this.#client.workflow.getHandle(durableWorkflowId(sessionId)).describe())
      return described.status.name
    } catch (err) {
      if (notFound(err)) return undefined
      throw new DurableUnavailable(`Temporal did not describe ${durableWorkflowId(sessionId)}: ${(err as Error).message}`)
    }
  }

  async send(input: DurableSessionInput, message: DurableMessage, options: DurableSendOptions = {}): Promise<DurableSendResult> {
    const sessionId = input.session_id
    const workflowId = durableWorkflowId(sessionId)
    const status = await this.#status(sessionId)
    let state: unknown = null
    let start = input
    let context = message.context
    let result: DurableSendResult
    if (status === 'RUNNING') {
      result = { started: 'attached', resumedFresh: false }
    } else if (status === 'COMPLETED') {
      // A Stop's hand-over (deviation 4): the AgentState, passed back as opaque JSON.
      state = await this.#client.workflow.getHandle(workflowId).result()
      result = { started: 'handed_over', resumedFresh: false }
    } else {
      const snapshot = await this.#snapshot(sessionId)
      if (snapshot) {
        state = snapshot.state
        start = { ...input, restored: { in_flight: snapshot.inFlight } }
        const line = lostResultsLine(snapshot.lost)
        if (line) context = context ? `${context}\n\n${line}` : line
        result = { started: 'restored', resumedFresh: false }
      } else {
        result = { started: 'fresh', resumedFresh: status !== undefined }
      }
    }
    await options.beforeStart?.(result)
    const operation = new WithStartWorkflowOperation(DURABLE_WORKFLOW, {
      workflowId,
      taskQueue: DURABLE_TASK_QUEUE,
      // Every run argument, so Temporal applies the Python types (plan ruling 6).
      args: [start, state, null],
      workflowIdConflictPolicy: 'USE_EXISTING',
      workflowIdReusePolicy: 'ALLOW_DUPLICATE',
    })
    try {
      await this.#client.workflow.executeUpdateWithStart(SEND_UPDATE, {
        args: [{ text: message.text, context }],
        startWorkflowOperation: operation,
      })
    } catch (err) {
      throw this.#mapped(err)
    }
    return result
  }

  #mapped(err: unknown): unknown {
    if (err instanceof WorkflowUpdateFailedError) {
      return new DurableRefused((err.cause as Error | undefined)?.message ?? err.message)
    }
    if (err instanceof WorkflowUpdateRPCTimeoutOrCancelledError) return err
    if (unreachable(err)) return new DurableUnavailable((err as Error).message)
    return err
  }

  /** The latest snapshot, with the calls the audit saw finish after it (deviation 4, point 3). */
  async #snapshot(
    sessionId: string,
  ): Promise<{ state: unknown; inFlight: InFlightCall[]; lost: { id: string; name: string }[] } | undefined> {
    const [row] = await this.#sql<{ state: string; in_flight: InFlightCall[]; saved_at: Date }[]>`
      SELECT state, in_flight, saved_at FROM ai_durable_snapshots WHERE session_id = ${sessionId}`
    if (!row) return undefined
    const state = JSON.parse(row.state) as { recent_call_ids?: unknown }
    const known = new Set<string>([
      ...(Array.isArray(state.recent_call_ids) ? state.recent_call_ids.map(String) : []),
      ...row.in_flight.map((c) => c.id),
    ])
    const calls = await this.#sql<{ tool_use_id: string; action: string }[]>`
      SELECT tool_use_id, action FROM ai_audit
      WHERE kind = 'tool_call' AND session_id = ${sessionId} AND at > ${row.saved_at} AND tool_use_id IS NOT NULL
      ORDER BY at, id`
    const lost = new Map<string, string>()
    for (const c of calls) if (!known.has(c.tool_use_id) && !lost.has(c.tool_use_id)) lost.set(c.tool_use_id, c.action)
    return {
      state,
      inFlight: row.in_flight.map((c) => ({ id: c.id, name: c.name, status: c.status })),
      lost: [...lost].map(([id, name]) => ({ id, name })),
    }
  }

  async review(sessionId: string, toolUseId: string, approved: boolean, approver: string): Promise<void> {
    try {
      await this.#client.workflow.getHandle(durableWorkflowId(sessionId)).executeUpdate(REVIEW_UPDATE, {
        args: [toolUseId, approved, approver],
      })
    } catch (err) {
      if (notFound(err)) throw new DurableRefused(`No tool call ${toolUseId} is waiting for approval`)
      throw this.#mapped(err)
    }
  }

  async pending(sessionId: string): Promise<PendingCall[]> {
    // A closed execution has nothing waiting, and querying it would need a worker to replay it.
    if ((await this.#status(sessionId)) !== 'RUNNING') return []
    try {
      return await this.#ask(() =>
        this.#client.workflow.getHandle(durableWorkflowId(sessionId)).query<PendingCall[]>(PENDING_QUERY),
      )
    } catch (err) {
      if (notFound(err)) return []
      throw new DurableUnavailable(`Temporal did not answer ${PENDING_QUERY}: ${(err as Error).message}`)
    }
  }

  async cancel(sessionId: string): Promise<boolean> {
    if ((await this.#status(sessionId)) !== 'RUNNING') return false
    try {
      await this.#ask(() => this.#client.workflow.getHandle(durableWorkflowId(sessionId)).cancel())
      return true
    } catch (err) {
      if (notFound(err)) return false
      throw err
    }
  }
}
