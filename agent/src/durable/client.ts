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

/**
 * models.py `Nudge`: the send_message Update's one argument, the id of a message committed
 * to `ai_durable_inputs` (the manager's claim). The id is the Update's `updateId` too.
 */
export type DurableNudge = { id: string }

/** A validator refused the Update ("the session is busy", "No tool call … is waiting for approval"). */
export class DurableRefused extends Error {
  override name = 'DurableRefused'
}

/**
 * The send gave up: the session was stopped (DurableSendOptions.signal), or its message is
 * no longer pending (DurableSendOptions.attempt: a Stop elsewhere abandoned it, or a run
 * already took it). It may have reached a run before; the message's row says what became of it.
 */
export class DurableStopped extends Error {
  override name = 'DurableStopped'
}

/**
 * The DurableSession workflow's answers to a nudge whose message did not start
 * (agent-durable models.py). STOPPING and ABANDONED come from a Stop: the message never
 * runs. BUSY: another turn runs first, and the message stays queued in that run, which
 * starts it after the turn. UNKNOWN_INPUT: the run did not find the message after looking
 * again for a while; it is not abandoned either (lead ruling: only a Stop or a forget
 * gives a message up).
 */
export const DURABLE_STOPPING = 'the session is stopping; send again'
export const DURABLE_ABANDONED = 'this message was abandoned and will not run'
export const DURABLE_BUSY = 'the session is busy'
export const DURABLE_UNKNOWN_INPUT = 'no message with this id was committed for this session'

/** A send's refusal when the stopped execution did not close within D (DurableSendOptions). */
export const STILL_STOPPING = "this session's previous run is still stopping; send again"

/** Temporal did not answer: nothing was started. */
export class DurableUnavailable extends Error {
  override name = 'DurableUnavailable'
}

/**
 * How the execution that takes the message was found: the one running (`attached`), a
 * new one from the last execution's result (`handed_over`, after a Stop), from the latest
 * snapshot (`restored`, after a terminate or a failure), or from nothing (`fresh`).
 * `resumedFresh`: an earlier execution existed but left neither (its conversation is lost).
 * `beforeStart` is told what `describe` showed; `send` resolves with what happened. They
 * differ only when a running execution closed before the start reached it: then `send`
 * says `restored` or `fresh` where `beforeStart` was told `attached`.
 */
export type DurableSendResult = { started: 'attached' | 'handed_over' | 'restored' | 'fresh'; resumedFresh: boolean }

export type DurableSendOptions = {
  /**
   * Runs once each attempt chose its execution, before it sends anything: for a new
   * execution, the manager's `ai_durable_streams.next_offset` reset (plan ruling 9).
   * When it throws, the attempt sends nothing.
   */
  beforeStart?: (result: DurableSendResult) => Promise<void>
  /**
   * Runs as an attempt's update-with-start is sent; from then on a Stop reaches the
   * execution (`cancel`). Before it, a Stop aborts `signal` instead, and `send` gives up
   * with DurableStopped.
   */
  starting?: () => void
  signal?: AbortSignal
  /**
   * With it, `send` delivers until it has an answer (the may_have_started rule of #1316 and
   * #1066): any failure but a refusal may have reached Temporal, or not, so it sends again,
   * always with the same message id (the Update's id, which Temporal dedupes, and the
   * workflow's idempotency key), never a fresh send. Runs before every attempt: the
   * manager's heartbeat on the stream row; false when the message is no longer pending,
   * and `send` gives up (DurableStopped). Without it, one attempt.
   */
  attempt?: () => Promise<boolean>
  /** After an attempt that did not answer, before the next: the manager's `worker_pending`. */
  retrying?: (err: unknown) => void
}

/** A waiting call; `expires_at` (ISO 8601) is when the run's expiry timer denies it (workflow.py). */
export type PendingCall = { id: string; name: string; input: unknown; expires_at?: string }

export interface DurableSessions {
  /**
   * Nudges the session's workflow with a message committed to `ai_durable_inputs`,
   * starting the execution that takes it when none runs. Resolves once its turn started.
   */
  send(input: DurableSessionInput, messageId: string, options?: DurableSendOptions): Promise<DurableSendResult>
  review(sessionId: string, toolUseId: string, approved: boolean, approver: string): Promise<void>
  pending(sessionId: string): Promise<PendingCall[]>
  /** Stop: cancels the running execution; false when none is running. */
  cancel(sessionId: string): Promise<boolean>
}

/** How long a describe, a query, a review or a result may take before Temporal counts as unreachable. */
const ASK_MS = 10_000

/**
 * D: the longest an update-with-start may take before it is aborted (it then rejects with
 * WorkflowUpdateRPCTimeoutOrCancelledError, and the start may still land). agent-durable's
 * projector counts a send's mark unchanged for 2 x D as stale (projector.py SEND_DEADLINE_S).
 */
export const DURABLE_SEND_DEADLINE_MS = 30_000

/** The first and the longest pause between delivery attempts (DurableSendOptions.attempt). */
const RETRY_FIRST_MS = 1_000
const RETRY_MAX_MS = 10_000

/** The model-only line for calls whose results a snapshot restore lost (deviation 4, point 3). */
export function lostResultsLine(calls: { id: string; name: string }[]): string | undefined {
  if (calls.length === 0) return undefined
  return (
    "These tool calls ran after this session's last saved point, and their results were lost: " +
    calls.map((c) => `${c.name} (${c.id})`).join(', ')
  )
}

/** Resolves after `ms`, or at once when `signal` aborts (a monotonic timer). */
function pause(ms: number, signal: AbortSignal | undefined): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve()
    const done = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', done)
      resolve()
    }
    const timer = setTimeout(done, ms)
    signal?.addEventListener('abort', done, { once: true })
  })
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
  readonly #sendDeadlineMs: number
  readonly #askMs: number

  /**
   * `sql` reads ai_durable_snapshots and the audit's tool calls for a restore, and writes a
   * restore's note on the message (ai_durable_inputs).
   */
  constructor(client: Client, sql: Sql, options: { sendDeadlineMs?: number; askMs?: number } = {}) {
    this.#client = client
    this.#sql = sql
    this.#sendDeadlineMs = options.sendDeadlineMs ?? DURABLE_SEND_DEADLINE_MS
    this.#askMs = options.askMs ?? ASK_MS
  }

  async #ask<T>(work: () => Promise<T>): Promise<T> {
    // A timer, not a wall-clock deadline (the host's clock may jump).
    return this.#client.connection.withAbortSignal(AbortSignal.timeout(this.#askMs), work)
  }

  /** The execution's status name, or undefined when the ID has none. */
  async #status(sessionId: string): Promise<string | undefined> {
    return (await this.#describe(sessionId))?.status
  }

  /**
   * The latest execution's status, its chain (the first run's id, which Continue-As-New
   * keeps and a new start does not), and whether a Stop was requested of it (`stopping`),
   * or undefined when the ID has none.
   */
  async #describe(sessionId: string): Promise<{ status: string; chain: string; stopping: boolean } | undefined> {
    try {
      const described = await this.#ask(() => this.#client.workflow.getHandle(durableWorkflowId(sessionId)).describe())
      return {
        status: described.status.name,
        chain: described.raw.workflowExecutionInfo?.firstRunId || described.runId,
        stopping: described.raw.workflowExtendedInfo?.cancelRequested === true,
      }
    } catch (err) {
      if (notFound(err)) return undefined
      throw new DurableUnavailable(`Temporal did not describe ${durableWorkflowId(sessionId)}: ${(err as Error).message}`)
    }
  }

  /**
   * A stopped execution still running: the plugin ends its task (and the session reads
   * `idle`) before the execution returns its state, and its send validator refuses
   * messages meanwhile (workflow.py). The send waits for it to close, up to D, and is then
   * handed over; a Stop of the session (`stopped`) ends the wait.
   */
  async #closed(
    sessionId: string,
    stopped: AbortSignal | undefined,
  ): Promise<{ status: string; chain: string; stopping: boolean } | undefined> {
    const abort = new AbortController()
    const timer = setTimeout(() => abort.abort(), this.#sendDeadlineMs)
    const stop = () => abort.abort()
    stopped?.addEventListener('abort', stop, { once: true })
    try {
      await this.#client.connection.withAbortSignal(abort.signal, () =>
        this.#client.workflow.getHandle(durableWorkflowId(sessionId)).result(),
      )
    } catch (err) {
      // Closed some other way (terminated, failed) is closed too.
      if (stopped?.aborted) throw new DurableStopped('the session was stopped before this message was sent')
      if (abort.signal.aborted) throw new DurableRefused(STILL_STOPPING)
      if (unreachable(err)) throw new DurableUnavailable((err as Error).message)
    } finally {
      clearTimeout(timer)
      stopped?.removeEventListener('abort', stop)
    }
    return this.#describe(sessionId)
  }

  async send(input: DurableSessionInput, messageId: string, options: DurableSendOptions = {}): Promise<DurableSendResult> {
    const attempt = options.attempt
    if (!attempt) return this.#attempt(input, messageId, options)
    let delay = RETRY_FIRST_MS
    for (;;) {
      if (options.signal?.aborted) throw new DurableStopped('the session was stopped before this message was sent')
      if (!(await attempt())) throw new DurableStopped('the message is no longer waiting to be sent')
      try {
        return await this.#attempt(input, messageId, options)
      } catch (err) {
        if (err instanceof DurableStopped) throw err
        // A refusal is an answer; a run still stopping is not (it closes, and then takes it).
        if (err instanceof DurableRefused && err.message !== STILL_STOPPING) throw err
        options.retrying?.(err)
      }
      await pause(delay, options.signal)
      delay = Math.min(delay * 2, RETRY_MAX_MS)
    }
  }

  async #attempt(input: DurableSessionInput, messageId: string, options: DurableSendOptions): Promise<DurableSendResult> {
    const sessionId = input.session_id
    const workflowId = durableWorkflowId(sessionId)
    let before = await this.#describe(sessionId)
    // A stopped run still closing would refuse the nudge: wait for it, up to D. A Stop
    // handled by another replica meanwhile abandoned the message: look again before sending.
    if (before?.status === 'RUNNING' && before.stopping) {
      before = await this.#closed(sessionId, options.signal)
      if (options.attempt && !(await options.attempt())) throw new DurableStopped('the message is no longer waiting to be sent')
    }
    const status = before?.status
    let state: unknown = null
    let start = input
    let note: string | null = null
    let result: DurableSendResult
    // Taken for a running execution too: USE_EXISTING ignores the start's arguments while
    // it runs, and they matter only if it closed (a Stop, a terminate) since `describe`.
    let fallback: DurableSendResult = { started: 'fresh', resumedFresh: status !== undefined }
    if (status === 'COMPLETED') {
      // A Stop's hand-over (deviation 4): the AgentState, passed back as opaque JSON.
      state = await this.#ask(() => this.#client.workflow.getHandle(workflowId).result())
      result = { started: 'handed_over', resumedFresh: false }
    } else {
      const snapshot = await this.#snapshot(sessionId)
      if (snapshot) {
        state = snapshot.state
        start = { ...input, restored: { in_flight: snapshot.inFlight } }
        note = lostResultsLine(snapshot.lost) ?? null
        fallback = { started: 'restored', resumedFresh: false }
      }
      result = status === 'RUNNING' ? { started: 'attached', resumedFresh: false } : fallback
    }
    await options.beforeStart?.(result)
    // The model-only line a restore owes the message, read by the run that loads it.
    if (result.started !== 'attached') await this.#sql`UPDATE ai_durable_inputs SET note = ${note} WHERE id = ${messageId}`
    if (options.signal?.aborted) throw new DurableStopped('the session was stopped before this message was sent')
    options.starting?.()
    const operation = new WithStartWorkflowOperation(DURABLE_WORKFLOW, {
      workflowId,
      taskQueue: DURABLE_TASK_QUEUE,
      // Every run argument, so Temporal applies the Python types (plan ruling 6).
      args: [start, state],
      workflowIdConflictPolicy: 'USE_EXISTING',
      workflowIdReusePolicy: 'ALLOW_DUPLICATE',
    })
    const nudge: DurableNudge = { id: messageId }
    // Aborted after D (a timer, not a wall-clock deadline): a hung RPC ends, and the next
    // attempt sends the same id again.
    const abort = new AbortController()
    const timer = setTimeout(() => abort.abort(), this.#sendDeadlineMs)
    try {
      await this.#client.connection.withAbortSignal(abort.signal, () =>
        this.#client.workflow.executeUpdateWithStart(SEND_UPDATE, {
          args: [nudge],
          updateId: messageId,
          startWorkflowOperation: operation,
        }),
      )
    } catch (err) {
      throw this.#mapped(err)
    } finally {
      clearTimeout(timer)
    }
    if (result.started !== 'attached') return result
    // Attached as far as `describe` knew: a new chain means the run closed in between and
    // this start made the next one, from the snapshot (or from nothing). Asked twice; if
    // Temporal cannot say, it stays `attached`: this only labels the result (the projector
    // reads a new chain from offset 0 by itself), and a guess of "new" would log that the
    // conversation was lost when it may not have been.
    const after =
      (await this.#describe(sessionId).catch(() => undefined)) ??
      (await this.#describe(sessionId).catch(() => undefined))
    return after && before && after.chain !== before.chain ? fallback : result
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
    // Under a deadline: a decision is taken on the chat socket's queue, which a dead worker
    // (the Update is never accepted) must not hold.
    const deadline = AbortSignal.timeout(this.#askMs)
    try {
      await this.#client.connection.withAbortSignal(deadline, () =>
        this.#client.workflow.getHandle(durableWorkflowId(sessionId)).executeUpdate(REVIEW_UPDATE, {
          args: [toolUseId, approved, approver],
        }),
      )
    } catch (err) {
      if (notFound(err)) throw new DurableRefused(`No tool call ${toolUseId} is waiting for approval`)
      if (deadline.aborted || err instanceof WorkflowUpdateRPCTimeoutOrCancelledError) {
        throw new DurableUnavailable(`Temporal did not take the decision on ${toolUseId} in time: ${(err as Error).message}`)
      }
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
