// Names and shapes shared by the agent service's Temporal code and its workflow bundle,
// which may import nothing that touches Node (spec 2026-10-01 §4.3, #1055).

/** The agent service's task queue: every tool as an activity, and the agent's commands. */
export const TASK_QUEUE = 'agent-tools'

/** The agent's command workflow (§4.2), its first Update, and its record activities. */
export const AGENT_OPERATION_WORKFLOW = 'AgentOperation'
export const ACCEPTED_UPDATE = 'accepted'
export const INSERT_ACTIVITY = 'agent_op_insert'
export const FINISH_ACTIVITY = 'agent_op_finish'
export const checkActivity = (kind: string): string => `agent_op.${kind}.check`
export const runActivity = (kind: string): string => `agent_op.${kind}.run`

/** ApplicationFailure types: a check's refusal, a run's failure (details[0] is the problem). */
export const REFUSED = 'Refused'
export const FAILED = 'Failed'

/** The problem a refused or failed command answers with: the route's status and body. */
export type OperationProblem = {
  status: number
  title: string
  detail: string
  type?: string
  extensions?: Record<string, unknown>
}

export type OperationInput = {
  kind: string
  subject: string
  /** `operation_key`: what a repeat of the same request is found by. */
  key: string
  request: Record<string, unknown>
  runAttempts: number
  runTimeoutS: number
  searchAttributes: boolean
}

/** ai_operations' row as the workflow passes it (operations/store.ts `Operation`). */
export type OperationRecord = {
  id: string
  kind: string
  subject: string
  status: 'running' | 'succeeded' | 'failed'
  result: unknown
  error: OperationProblem | null
  created_at: string
  finished_at: string | null
}

export type OperationAnswer = {
  operation: OperationRecord | null
  refusal: OperationProblem | null
  repeated: boolean
}

export type InsertInput = { input: OperationInput }
export type RunInput = { request: Record<string, unknown>; checked: unknown }
export type FinishInput = { operationId: string; result?: unknown; error?: OperationProblem }

const CONFLICT = { status: 409, title: 'Conflict' }
/** A command cancelled before its record: nothing was written or done. */
export const OPERATION_CANCELLED: OperationProblem = {
  ...CONFLICT,
  detail: 'This was cancelled before it started. Nothing was done; try again.',
}
/** A command cancelled while its effect ran: it may have been done. */
export const OPERATION_CANCELLED_RUNNING: OperationProblem = {
  ...CONFLICT,
  detail: 'This was cancelled while it was running, so it may have been done. Check before trying again.',
}
/** A command whose execution ended without recording how (terminated, say). */
export const OPERATION_LOST: OperationProblem = {
  status: 500,
  title: 'Internal Server Error',
  detail: 'ScadBuddy stopped running this before it recorded how it ended, so it may have been done. Check before trying again.',
}
/** Anything a command's activity did not describe: never the error's own text. */
export const OPERATION_UNEXPECTED: OperationProblem = {
  status: 500,
  title: 'Internal Server Error',
  detail: "ScadBuddy's agent service failed unexpectedly while doing this; see its logs.",
}
