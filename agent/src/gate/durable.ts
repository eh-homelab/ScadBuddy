import { type Client, WorkflowUpdateFailedError } from '@temporalio/client'
import { ApplicationFailure, WorkflowNotFoundError } from '@temporalio/common'
import type { InputOutcome, Owner } from '../sessions/protocol.js'
import { CANCEL_INPUT_UPDATE, GATE_REFUSED, INTERRUPT_SIGNAL, PENDING_INPUT_QUERY, RESPOND_UPDATE } from './names.js'
import type { PendingInputEntry } from './projection.js'
import { REFUSALS, RespondRefusal, type RefusalCode, type Role } from './validate.js'

// The agent service's side of a durable session's gate (durable-agents spec §6.6):
// the `pending_input` Query, the `respond` and `cancel_input` Updates and the
// `interrupt` Signal of the `session-<id>` workflow, which agent-durable's
// DurableSession (phase 5c) registers under the names in names.ts.
//
// The respond route is the Update's only legitimate caller: it decides who may answer
// (role.ts) and passes the responder and role; the workflow's validator checks the rest
// and refuses with an ApplicationFailure of type `GateRefused:<code>`, which comes
// back here as a RespondRefusal with that code. A workflow that does not answer in
// time (its worker is down) is DurableUnavailable; one that does not exist is
// WorkflowNotFound, which the route reports as stale.

export class DurableUnavailable extends Error {
  override name = 'DurableUnavailable'
}

/** The workflow ID of a durable session (§6.2). */
export const sessionWorkflowId = (sessionId: string): string => `session-${sessionId}`

export type RespondArgs = { request_id: string; response: unknown; responder: Owner; role: Role }
export type RespondOutcome = { kind: 'approval' | 'answer'; outcome: InputOutcome }

export type DurableGateOptions = {
  /** How long a Query or `respond` may take (default 5 s). */
  timeoutMs?: number
  /** How long `cancel_input` may take before its caller decides without it (spec: 10 s). */
  cancelTimeoutMs?: number
}

function refusalOf(err: unknown): RespondRefusal | undefined {
  const cause = err instanceof WorkflowUpdateFailedError ? err.cause : err
  if (!(cause instanceof ApplicationFailure)) return undefined
  const type = cause.type ?? ''
  if (!type.startsWith(`${GATE_REFUSED}:`)) return undefined
  const code = type.slice(GATE_REFUSED.length + 1)
  return (REFUSALS as readonly string[]).includes(code) ? new RespondRefusal(code as RefusalCode, cause.message) : undefined
}

export class DurableGate {
  readonly #client: Client
  readonly #timeoutMs: number
  readonly #cancelTimeoutMs: number

  constructor(client: Client, options: DurableGateOptions = {}) {
    this.#client = client
    this.#timeoutMs = options.timeoutMs ?? 5_000
    this.#cancelTimeoutMs = options.cancelTimeoutMs ?? 10_000
  }

  async #call<T>(timeoutMs: number, fn: () => Promise<T>): Promise<T> {
    try {
      return await this.#client.withDeadline(Date.now() + timeoutMs, fn)
    } catch (err) {
      const refusal = refusalOf(err)
      if (refusal) throw refusal
      if (err instanceof WorkflowNotFoundError) throw err
      throw new DurableUnavailable(`the session's worker is not answering; try again (${(err as Error).message})`)
    }
  }

  /** The session's parked calls, from its workflow (the source of truth). */
  async pendingInput(sessionId: string): Promise<PendingInputEntry[]> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    return this.#call(this.#timeoutMs, () => handle.query<PendingInputEntry[]>(PENDING_INPUT_QUERY))
  }

  /** Answers one entry; the workflow's validator may refuse it (RespondRefusal). */
  async respond(sessionId: string, args: RespondArgs): Promise<RespondOutcome> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    return this.#call(this.#timeoutMs, () => handle.executeUpdate<RespondOutcome, [RespondArgs]>(RESPOND_UPDATE, { args: [args] }))
  }

  /**
   * Ends the session's parked entry without a decision (interrupt, handoff, a
   * superseding send): `cancelled` when there was one, `none` otherwise. Bounded at
   * 10 s; past that it is DurableUnavailable and the caller decides (spec §6.6).
   */
  async cancelInput(sessionId: string, reason: string): Promise<'cancelled' | 'none'> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    return this.#call(this.#cancelTimeoutMs, () =>
      handle.executeUpdate<'cancelled' | 'none', [{ reason: string }]>(CANCEL_INPUT_UPDATE, { args: [{ reason }] }),
    )
  }

  /** The cancel-only `interrupt` Signal: recorded in history even while no worker runs. */
  async interrupt(sessionId: string, reason: string): Promise<void> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    await this.#call(this.#timeoutMs, () => handle.signal<[{ reason: string }]>(INTERRUPT_SIGNAL, { reason }))
  }
}
