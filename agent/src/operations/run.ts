import type { Client } from '@temporalio/client'
import { randomUUID } from 'node:crypto'
import {
  OPERATION_LOST,
  type OperationAnswer,
  type OperationProblem,
  type OperationRecord,
} from '../temporal/names.js'
import {
  AlreadyClosedError,
  CommandClosedError,
  CommandStillAcceptingError,
  startCommand,
  TemporalUnavailableError,
} from './command.js'
import type { OperationKind } from './kinds.js'
import { canonicalJson, type Operation, operationKey, type OperationStore } from './store.js'

// Running an agent command from a route (spec 2026-10-01 §4.2, #1055), as the backend's
// `run_operation` (backend/scadbuddy/api/operations.py): the request is keyed by its
// kind, subject, body and the client's `Idempotency-Key` (without one each request is
// its own command). The record is read first, so a repeat answers from it and starts
// nothing. Otherwise AgentOperation is started (or attached to). Every kind is `done`:
// the route's body once the effect ended, or 202 with the operation past the deadline.

/** Problem types a client recognises (the backend's, so `command()` treats them alike). */
export const STILL_ACCEPTING_PROBLEM = 'https://scadbuddy.dev/problems/command-still-accepting'
export const TEMPORAL_UNAVAILABLE_PROBLEM = 'https://scadbuddy.dev/problems/temporal-unavailable'

/** The most a command's request may carry inline, as the backend's `MAX_REQUEST_BYTES`. */
export const MAX_REQUEST_BYTES = 128 * 1024

/** How a command's request ended, for the route to answer. */
export type CommandOutcome =
  | { status: 'done'; result: unknown }
  | { status: 'running'; operation: Operation }
  | { status: 'problem'; problem: OperationProblem; retryAfter?: number }

/** What the routes run commands through; tests may stand in for it. */
export interface Commands {
  run(kind: string, request: Record<string, unknown>, idempotencyKey: string | undefined): Promise<CommandOutcome>
  /** One operation; a running one whose execution has ended is recorded as lost. */
  get(id: string): Promise<Operation | undefined>
}

function stillAccepting(): CommandOutcome {
  return {
    status: 'problem',
    problem: {
      status: 503,
      title: 'Service Unavailable',
      type: STILL_ACCEPTING_PROBLEM,
      detail: 'ScadBuddy is still checking this request. Send it again to follow it.',
    },
    retryAfter: 2,
  }
}

function temporalUnavailable(): CommandOutcome {
  return {
    status: 'problem',
    problem: {
      status: 503,
      title: 'Service Unavailable',
      type: TEMPORAL_UNAVAILABLE_PROBLEM,
      detail: "ScadBuddy cannot reach Temporal, where the agent's commands run. Nothing was done; try again shortly.",
    },
    retryAfter: 5,
  }
}

/** The route's answer for a recorded operation: its body, its problem, or a 202. */
export function answerOf(op: Operation | OperationRecord, repeated: boolean): CommandOutcome {
  if (op.status === 'succeeded') return { status: 'done', result: op.result }
  if (op.status === 'failed') return { status: 'problem', problem: op.error ?? OPERATION_LOST }
  return { status: 'running', operation: { ...op, repeated } }
}

export type AgentCommandsOptions = {
  client: Client
  store: OperationStore
  kinds: readonly OperationKind[]
  searchAttributes: boolean
  deadlineMs?: number
}

export class AgentCommands implements Commands {
  readonly #options: AgentCommandsOptions
  readonly #kinds: Map<string, OperationKind>

  constructor(options: AgentCommandsOptions) {
    this.#options = options
    this.#kinds = new Map(options.kinds.map((k) => [k.name, k]))
  }

  async run(kindName: string, request: Record<string, unknown>, idempotencyKey: string | undefined): Promise<CommandOutcome> {
    const kind = this.#kinds.get(kindName)
    if (!kind) throw new Error(`no command kind ${kindName}`)
    const size = Buffer.byteLength(canonicalJson(request))
    if (size > MAX_REQUEST_BYTES) {
      return {
        status: 'problem',
        problem: {
          status: 413,
          title: 'Content Too Large',
          detail: `This request is ${size} bytes; at most ${MAX_REQUEST_BYTES} are accepted here.`,
        },
      }
    }
    const { store, client } = this.#options
    const subject = kind.subject(request)
    const key = operationKey(kind.name, subject, request, idempotencyKey ?? randomUUID().replaceAll('-', ''))
    const recorded = await store.find(key)
    if (recorded) return answerOf(recorded, true)
    let answer: OperationAnswer
    try {
      answer = await startCommand(
        client,
        `op-${kind.name}-${key}`,
        {
          kind: kind.name,
          subject,
          key,
          request,
          runAttempts: kind.runAttempts,
          runTimeoutS: kind.runTimeoutS,
          searchAttributes: this.#options.searchAttributes,
        },
        this.#options.deadlineMs,
      )
    } catch (err) {
      if (err instanceof AlreadyClosedError) {
        const op = await store.find(key)
        if (op) return answerOf(op, true)
        // Its record was pruned while Temporal still keeps the execution: it may have run.
        return {
          status: 'problem',
          problem: {
            status: 409,
            title: 'Conflict',
            detail: 'This request already ran, and its record has since been deleted, so it may have been done. Check before sending it again.',
          },
        }
      }
      if (err instanceof CommandStillAcceptingError) {
        const op = await store.find(key)
        return op ? answerOf(op, false) : stillAccepting()
      }
      if (err instanceof CommandClosedError) return stillAccepting()
      if (err instanceof TemporalUnavailableError) return temporalUnavailable()
      throw err
    }
    if (answer.refusal) return { status: 'problem', problem: answer.refusal }
    return answerOf(answer.operation!, answer.repeated)
  }

  async get(id: string): Promise<Operation | undefined> {
    const op = await this.#options.store.get(id)
    if (op?.status !== 'running') return op
    // Terminated in the Temporal UI, say, after its record: say so rather than run forever.
    const execution = await this.#options.store.execution(id)
    if (!execution || (await this.#running(execution.workflowId, execution.workflowRunId))) return op
    return this.#options.store.finish(id, { error: OPERATION_LOST })
  }

  async #running(workflowId: string, runId: string): Promise<boolean> {
    try {
      const described = await this.#options.client.workflow.getHandle(workflowId, runId).describe()
      return described.status.name === 'RUNNING'
    } catch (err) {
      if ((err as Error).name === 'WorkflowNotFoundError') return false
      // Temporal cannot say: leave the record as it is.
      return true
    }
  }
}
