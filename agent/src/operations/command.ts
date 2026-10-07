import {
  type Client,
  isGrpcServiceError,
  WithStartWorkflowOperation,
  WorkflowUpdateFailedError,
  WorkflowUpdateRPCTimeoutOrCancelledError,
} from '@temporalio/client'
import { ApplicationFailure, WorkflowExecutionAlreadyStartedError } from '@temporalio/common'
import {
  ACCEPTED_UPDATE,
  AGENT_OPERATION_WORKFLOW,
  type OperationAnswer,
  type OperationInput,
  TASK_QUEUE,
} from '../temporal/names.js'

// The one way an agent route starts a command (spec 2026-10-01 §4.2, #1055), as the
// backend's `start_command` (backend/scadbuddy/workflows/commands.py): update-with-start
// names the execution by its key, starts it or attaches to the one running
// (`USE_EXISTING`), and waits on its `accepted` Update, which answers with the record or
// the refusal. `ALLOW_DUPLICATE_FAILED_ONLY`: a completed execution is never repeated
// (the route answers from the record), a refused one may start again.

/** Below Envoy's 15 s route timeout (§1), so no command holds a request open past it. */
export const COMMAND_ANSWER_DEADLINE_MS = 10_000
/** How much longer than the deadline a call may take before Temporal counts as unreachable. */
const CONNECT_MARGIN_MS = 2_000
/** How long the route asks Temporal whether an execution exists, once the bound passed. */
const DESCRIBE_MS = 2_000
/** The failure Temporal gives an Update whose execution completed before it answered. */
const UPDATE_OUTLIVED = 'AcceptedUpdateCompletedWorkflow'

/** The Update did not answer within the deadline; the same request attaches to the execution. */
export class CommandStillAcceptingError extends Error {
  override name = 'CommandStillAcceptingError'
}
/** Temporal did not answer at all: nothing was started. */
export class TemporalUnavailableError extends Error {
  override name = 'TemporalUnavailableError'
}
/** The execution ended before its Update answered: nothing was recorded. */
export class CommandClosedError extends Error {
  override name = 'CommandClosedError'
}
/** The ID's last execution closed and the reuse policy refuses another: answer from the record. */
export class AlreadyClosedError extends Error {
  override name = 'AlreadyClosedError'
}

class Late extends Error {}

/** What a call that outlived its bound means: the execution exists (still accepting), or Temporal is away. */
async function late(client: Client, workflowId: string): Promise<Error> {
  try {
    // A timer, not a wall-clock deadline (the host's clock may step).
    await client.connection.withAbortSignal(AbortSignal.timeout(DESCRIBE_MS), () =>
      client.workflow.getHandle(workflowId).describe(),
    )
  } catch {
    return new TemporalUnavailableError(workflowId)
  }
  return new CommandStillAcceptingError(workflowId)
}

export async function startCommand(
  client: Client,
  workflowId: string,
  input: OperationInput,
  deadlineMs = COMMAND_ANSWER_DEADLINE_MS,
): Promise<OperationAnswer> {
  const operation = new WithStartWorkflowOperation<(input: OperationInput) => Promise<unknown>>(AGENT_OPERATION_WORKFLOW, {
    workflowId,
    taskQueue: TASK_QUEUE,
    args: [input],
    workflowIdConflictPolicy: 'USE_EXISTING',
    workflowIdReusePolicy: 'ALLOW_DUPLICATE_FAILED_ONLY',
  })
  let timer: NodeJS.Timeout | undefined
  // The deadline bounds the RPCs; the outer bound also covers a connect that hangs.
  const bound = new Promise<never>((_, reject) => {
    timer = setTimeout(() => reject(new Late()), deadlineMs + CONNECT_MARGIN_MS)
  })
  try {
    return await Promise.race([
      client.connection.withAbortSignal(AbortSignal.timeout(deadlineMs), () =>
        client.workflow.executeUpdateWithStart<(input: OperationInput) => Promise<unknown>, OperationAnswer, []>(
          ACCEPTED_UPDATE,
          { startWorkflowOperation: operation },
        ),
      ),
      bound,
    ])
  } catch (err) {
    if (err instanceof Late) throw await late(client, workflowId)
    if (err instanceof WorkflowExecutionAlreadyStartedError) throw new AlreadyClosedError(workflowId)
    if (err instanceof WorkflowUpdateRPCTimeoutOrCancelledError) throw await late(client, workflowId)
    if (err instanceof WorkflowUpdateFailedError) {
      if (err.cause instanceof ApplicationFailure && err.cause.type === UPDATE_OUTLIVED) {
        throw new CommandClosedError(workflowId)
      }
      throw err
    }
    // gRPC: 1 CANCELLED (the deadline's abort signal), 4 DEADLINE_EXCEEDED, 14 UNAVAILABLE.
    const cause = (err as { cause?: unknown } | undefined)?.cause
    const grpc = isGrpcServiceError(err) ? err : isGrpcServiceError(cause) ? cause : undefined
    if (grpc) {
      if (grpc.code === 1 || grpc.code === 4) throw await late(client, workflowId)
      if (grpc.code === 14) throw new TemporalUnavailableError(workflowId)
    }
    throw err
  } finally {
    clearTimeout(timer)
  }
}
