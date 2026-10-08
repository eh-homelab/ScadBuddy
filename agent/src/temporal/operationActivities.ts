import { Context } from '@temporalio/activity'
import { ApplicationFailure } from '@temporalio/common'
import { randomUUID } from 'node:crypto'
import { OperationRefusal, type OperationKind } from '../operations/kinds.js'
import type { OperationStore } from '../operations/store.js'
import {
  checkActivity,
  FAILED,
  FINISH_ACTIVITY,
  type FinishInput,
  INSERT_ACTIVITY,
  type InsertInput,
  type OperationRecord,
  REFUSED,
  runActivity,
  type RunInput,
} from './names.js'

// AgentOperation's activities (workflows.ts, #1055): each kind's check and run, and the
// record's insert and finish in ai_operations. A refusal crosses as a non-retryable
// ApplicationFailure whose details[0] is the problem the route answers with. Anything
// else crosses as its type alone: its text (git's stderr, a path) is logged here, never
// written to history, and the workflow records it as unexpected. A cancellation crosses
// as it is, so the SDK reports the activity cancelled.

type Activity = (input: never) => Promise<unknown>

function crossing(err: unknown, type: string): unknown {
  if (err instanceof OperationRefusal) {
    return ApplicationFailure.create({ message: err.problem.detail, type, nonRetryable: true, details: [err.problem] })
  }
  const context = Context.current()
  if (context.cancellationSignal.aborted) return err
  const name = err instanceof Error ? err.name : typeof err
  context.log.error(`${context.info.activityType} failed`, { error: err instanceof Error ? err.message : String(err) })
  return ApplicationFailure.create({ message: `${context.info.activityType} failed unexpectedly`, type: name })
}

export function operationActivities(kinds: readonly OperationKind[], store: OperationStore): Record<string, Activity> {
  const activities: Record<string, Activity> = {
    [INSERT_ACTIVITY]: async ({ input }: InsertInput): Promise<OperationRecord> => {
      const execution = Context.current().info.workflowExecution
      if (!execution) throw new Error('agent_op_insert runs only in a workflow')
      const { workflowId, runId } = execution
      return store.insert({
        id: randomUUID().replaceAll('-', ''),
        kind: input.kind,
        subject: input.subject,
        operationKey: input.key,
        request: input.request,
        workflowId,
        workflowRunId: runId,
      })
    },
    [FINISH_ACTIVITY]: async ({ operationId, result, error }: FinishInput): Promise<OperationRecord> =>
      store.finish(operationId, error ? { error } : { result: result ?? null }),
  }
  for (const kind of kinds) {
    activities[checkActivity(kind.name)] = async (request: Record<string, unknown>) => {
      try {
        return (await kind.check(request)) ?? null
      } catch (err) {
        throw crossing(err, REFUSED)
      }
    }
    activities[runActivity(kind.name)] = async ({ request, checked }: RunInput) => {
      const context = Context.current()
      const heartbeat = setInterval(() => context.heartbeat(), 10_000)
      try {
        return (await kind.run(request, checked, context.cancellationSignal)) ?? null
      } catch (err) {
        throw crossing(err, FAILED)
      } finally {
        clearInterval(heartbeat)
      }
    }
  }
  return activities
}
