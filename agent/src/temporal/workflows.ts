import {
  ActivityFailure,
  allHandlersFinished,
  ApplicationFailure,
  CancellationScope,
  condition,
  defineUpdate,
  isCancellation,
  proxyActivities,
  setHandler,
  upsertSearchAttributes,
} from '@temporalio/workflow'
import { defineSearchAttributeKey } from '@temporalio/common'
import {
  ACCEPTED_UPDATE,
  checkActivity,
  FAILED,
  FINISH_ACTIVITY,
  type FinishInput,
  INSERT_ACTIVITY,
  type InsertInput,
  OPERATION_CANCELLED,
  OPERATION_CANCELLED_RUNNING,
  OPERATION_UNEXPECTED,
  type OperationAnswer,
  type OperationInput,
  type OperationProblem,
  type OperationRecord,
  REFUSED,
  runActivity,
  type RunInput,
} from './names.js'

// AgentOperation: the agent's commands in the shape of spec 2026-10-01 §4.2 (#1055),
// the backend `Operation`'s order and answers (backend/scadbuddy/workflows/operation.py):
//
// 1. `agent_op.<kind>.check` makes the route's refusals and writes nothing. A refusal
//    answers the `accepted` Update and *fails* the execution, so a retry may start again.
// 2. `agent_op_insert` writes the record, retried until it lands, never cancelled.
// 3. `agent_op.<kind>.run` is the effect, with the kind's attempts; then
//    `agent_op_finish`. From the record on, every outcome completes the execution and
//    is recorded.
//
// Every kind is `done`: the Update answers once the effect ended. A change to this
// function goes behind `patched()`, and test/fixtures/agent_operation_histories/
// replays (plan ruling 9).

export const acceptedUpdate = defineUpdate<OperationAnswer>(ACCEPTED_UPDATE)

const KIND = defineSearchAttributeKey('ScadbuddyKind', 'KEYWORD')
const SUBJECT = defineSearchAttributeKey('ScadbuddySubject', 'KEYWORD')
const STATUS = defineSearchAttributeKey('ScadbuddyStatus', 'KEYWORD')

type Activities = Record<string, (input: unknown) => Promise<unknown>>

// No maximumAttempts: retried until it lands (the TypeScript SDK's unlimited default).
const RECORD_RETRY = { initialInterval: '1s', maximumInterval: '30s', backoffCoefficient: 2 }

/** The problem an activity reported (`Refused`/`Failed` details), else the unexpected one. */
function problemOf(err: unknown): OperationProblem {
  const cause = err instanceof ActivityFailure ? err.cause : err
  if (cause instanceof ApplicationFailure && (cause.type === REFUSED || cause.type === FAILED) && cause.details?.length) {
    return cause.details[0] as OperationProblem
  }
  return OPERATION_UNEXPECTED
}

export async function AgentOperation(input: OperationInput): Promise<OperationRecord> {
  // What the `accepted` Update answers with, once one of them is set.
  const answer: { done?: OperationRecord; refusal?: OperationProblem } = {}
  let updates = 0
  setHandler(acceptedUpdate, async () => {
    updates += 1
    const repeated = updates > 1
    await condition(() => answer.done !== undefined || answer.refusal !== undefined)
    return { operation: answer.done ?? null, refusal: answer.refusal ?? null, repeated }
  })
  const status = (value: string): void => {
    if (input.searchAttributes) upsertSearchAttributes([{ key: STATUS, value }])
  }
  if (input.searchAttributes) {
    upsertSearchAttributes([
      { key: KIND, value: `operation.${input.kind}` },
      { key: SUBJECT, value: input.subject },
      { key: STATUS, value: 'accepting' },
    ])
  }

  const check = proxyActivities<Activities>({
    startToCloseTimeout: '8s',
    retry: { maximumAttempts: 3, initialInterval: '1s', backoffCoefficient: 2 },
  })
  const record = proxyActivities<Activities>({ startToCloseTimeout: '60s', retry: RECORD_RETRY })
  const effect = proxyActivities<Activities>({
    startToCloseTimeout: `${input.runTimeoutS}s`,
    heartbeatTimeout: '30s',
    retry: { maximumAttempts: input.runAttempts, initialInterval: '1s', backoffCoefficient: 2 },
  })

  let checked: unknown
  try {
    checked = await check[checkActivity(input.kind)]!(input.request)
  } catch (err) {
    // Nothing was written: the execution fails, and a retry may start again. A cancel
    // answers the Update too, so it is never outlived by its execution.
    const refusal = isCancellation(err) ? OPERATION_CANCELLED : problemOf(err)
    answer.refusal = refusal
    status('refused')
    await condition(allHandlersFinished)
    if (isCancellation(err)) throw err
    throw ApplicationFailure.nonRetryable(refusal.detail, REFUSED)
  }
  // The insert may commit after a cancel: it is waited out, never abandoned, so its row
  // is ended below rather than left running.
  const op = (await CancellationScope.nonCancellable(() =>
    record[INSERT_ACTIVITY]!({ input } satisfies InsertInput),
  )) as OperationRecord
  status('running')
  let finish: FinishInput
  if (CancellationScope.current().consideredCancelled) {
    finish = { operationId: op.id, error: OPERATION_CANCELLED }
  } else {
    try {
      const result = await effect[runActivity(input.kind)]!({ request: input.request, checked } satisfies RunInput)
      finish = { operationId: op.id, result }
    } catch (err) {
      finish = { operationId: op.id, error: isCancellation(err) ? OPERATION_CANCELLED_RUNNING : problemOf(err) }
    }
  }
  const done = (await CancellationScope.nonCancellable(() => record[FINISH_ACTIVITY]!(finish))) as OperationRecord
  answer.done = done
  status(done.status)
  await condition(allHandlersFinished)
  return done
}
