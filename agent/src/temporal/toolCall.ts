import { proxyActivities } from '@temporalio/workflow'

// One tool call of a session's turn (unified runtime plan 2026-10-13 Step 9.2, decision Q1): a
// short workflow of its own, `session-<uuid>-call-<tool_use_id>`, on the shared `agent-tools`
// queue, whose one activity runs the tool (`tool:<name>`, toolActivities.ts `toolCallActivities`)
// on `queue`. So the session's own history carries none of the call, and neither of Temporal's
// per-execution Update limits (10 in flight, 2000 in all) bounds a turn's calls. The id is the
// call's, so a re-send after a lost answer finds the same execution and its result. Its payloads
// are sealed under the session's key (payloadCodec.ts `subjectOf`, Step 9.1). In the workflow
// bundle: no Node imports.

export const TOOL_CALL_WORKFLOW = 'ToolCall'

/** The workflow id of a session's call: its tool_use id, as Claude Code gave it. */
export const toolCallWorkflowId = (sessionId: string, toolUseId: string): string => `session-${sessionId.toLowerCase()}-call-${toolUseId}`

export type ToolCallInput = {
  /** The activity that runs the call: `tool:<name>`. */
  activity: string
  /** Its queue: `agent-tools`, or a replica's own for a call whose server lives on that pod. */
  queue: string
  /** The activity's one argument. */
  args: unknown
  /** The most the call may run. */
  timeoutMs: number
  /** How long it may wait to be taken: set for a replica's own queue, which nothing polls once the pod is gone. */
  startTimeoutMs?: number
}

/** What `tool:<name>` is given: the call as the model made it (the approved input, if it parked) and its gate entry. */
export type ToolCallArgs = {
  /** The registry tool's name. */
  tool: string
  input: unknown
  /** The gate entry the call parked on and a person approved (`durable:<session>:<run>:<tool_use_id>`); absent for a call that never parked. */
  requestId?: string
  /** The call's span in the turn's trace (telemetry/turn.ts), the activity's parent; absent when it is not sampled. */
  traceparent?: string
}

/** A result block: text, or an image kept in the session's blobs (sessions/blobs.ts), by name, never its bytes. */
export type ToolCallBlock = { type: 'text'; text: string } | { type: 'image'; name: string; mimeType: string }

/** What `tool:<name>` returns: whether the call succeeded, and what the model reads of it. */
export type ToolCallResult = { ok: boolean; content: ToolCallBlock[] }

/** The tool that reads what a session's finished ToolCall returned (tools/toolCalls.ts), which the restart message names. */
export const TOOL_CALL_RESULT_TOOL = 'tool_call_result'

/** The activity that runs a registry tool's call. */
export const toolCallActivity = (tool: string): string => `tool:${tool}`

/** How often a running call heartbeats (toolActivities.ts DEFAULT_HEARTBEAT_MS is 10 s): a cancel or a dead worker is seen within this. */
const HEARTBEAT_TIMEOUT = '30s'

export async function ToolCall(input: ToolCallInput): Promise<unknown> {
  const run = proxyActivities<Record<string, (args: unknown) => Promise<unknown>>>({
    taskQueue: input.queue,
    startToCloseTimeout: input.timeoutMs,
    ...(input.startTimeoutMs === undefined ? {} : { scheduleToStartTimeout: input.startTimeoutMs }),
    heartbeatTimeout: HEARTBEAT_TIMEOUT,
    // Once: the call may have acted before its worker went away (a write's Idempotency-Key answers a repeat, but nothing else does).
    retry: { maximumAttempts: 1 },
  })
  return run[input.activity]!(input.args)
}
