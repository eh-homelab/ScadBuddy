import { type Client, WorkflowUpdateFailedError } from '@temporalio/client'
import { randomUUID } from 'node:crypto'
import { ApplicationFailure, WorkflowNotFoundError } from '@temporalio/common'
import type { InputOutcome, Owner } from '../sessions/protocol.js'
import { responseHash } from './canonical.js'
import { OWNER_CHANGED_SIGNAL } from '../temporal/session/names.js'
import { CANCEL_INPUT_UPDATE, END_SIGNAL, GATE_REFUSED, INTERRUPT_SIGNAL, PENDING_INPUT_QUERY, RESPOND_UPDATE } from './names.js'
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

/**
 * A `respond` the validator took whose handler then failed (its `resolve_input` could
 * not be written): the entry is pending again, so it is worth sending again.
 */
class HandlerFailed extends DurableUnavailable {
  override name = 'HandlerFailed'
}

/** The workflow ID of a durable session (§6.2). */
export const sessionWorkflowId = (sessionId: string): string => `session-${sessionId}`

/** The responder: a principal, or for role `parent` the starting session (#2482). */
export type RespondArgs = { request_id: string; response: unknown; responder: Omit<Owner, 'kind'> & { kind: Owner['kind'] | 'session' }; role: Role }
export type RespondOutcome = { kind: 'approval' | 'answer'; outcome: InputOutcome }

export type DurableGateOptions = {
  /** How long a Query or `respond` may take (default 5 s). */
  timeoutMs?: number
  /** How long `cancel_input` may take before its caller decides without it (spec: 10 s). */
  cancelTimeoutMs?: number
}

/**
 * The `respond` Update's ID (#3717): the same request, response, responder and role
 * always give the same ID, so a retry after a 503 joins the Update the workflow
 * already accepted and gets its outcome; any difference is a new Update, which the
 * validator still judges. A hash, so the ID stays far inside Temporal's length limit
 * whatever the request id or response holds.
 */
export function respondUpdateId(args: RespondArgs): string {
  return `respond-${responseHash([args.request_id, args.response, args.responder, args.role])}`
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

  /** `respond` alone says an Update that failed in its handler is HandlerFailed: worth sending again. */
  async #call<T>(timeoutMs: number, fn: () => Promise<T>, { handlerFailed = false }: { handlerFailed?: boolean } = {}): Promise<T> {
    try {
      return await this.#client.withDeadline(Date.now() + timeoutMs, fn)
    } catch (err) {
      const refusal = refusalOf(err)
      if (refusal) throw refusal
      if (err instanceof WorkflowNotFoundError) throw err
      if (handlerFailed && err instanceof WorkflowUpdateFailedError) throw new HandlerFailed(`the session could not record that; try again (${(err as Error).message})`)
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
    const send = (updateId: string) =>
      this.#call(this.#timeoutMs, () => handle.executeUpdate<RespondOutcome, [RespondArgs]>(RESPOND_UPDATE, { args: [args], updateId }), { handlerFailed: true })
    const id = respondUpdateId(args)
    try {
      return await send(id)
    } catch (err) {
      if (!(err instanceof HandlerFailed)) throw err
      // Temporal keeps a completed Update's outcome under its ID, a failed one too, so
      // the same body would get this failure back for the rest of the run (#3723 review).
      // A fresh ID is judged by the validator again; the entry is pending again.
      return await send(`${id}-${randomUUID()}`)
    }
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

  /**
   * An AgentSession session's new owner (unified runtime spec §5.6): what its gate checks a
   * response against from now on. DurableSession has no such Signal; only a unified session's
   * workflow is sent it.
   */
  async ownerChanged(sessionId: string, owner: Owner): Promise<void> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    await this.#call(this.#timeoutMs, () => handle.signal<[{ owner: Owner }]>(OWNER_CHANGED_SIGNAL, { owner }))
  }

  /**
   * The session was marked done (#1056): its workflow completes once no turn runs. A
   * workflow already gone has nothing left to end.
   */
  async end(sessionId: string): Promise<void> {
    const handle = this.#client.workflow.getHandle(sessionWorkflowId(sessionId))
    try {
      await this.#call(this.#timeoutMs, () => handle.signal<[Record<string, never>]>(END_SIGNAL, {}))
    } catch (err) {
      if (!(err instanceof WorkflowNotFoundError)) throw err
    }
  }
}
