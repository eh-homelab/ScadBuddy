import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { context as otelContext, trace } from '@opentelemetry/api'
import { type Client, WorkflowFailedError, WorkflowNotFoundError } from '@temporalio/client'
import { WorkflowExecutionAlreadyStartedError } from '@temporalio/common'
import type { Sql } from 'postgres'
import type { SessionBlobs } from '../sessions/blobs.js'
import { traceparentOf } from '../telemetry/trace.js'
import { TASK_QUEUE } from '../temporal/names.js'
import {
  TOOL_CALL_WORKFLOW,
  type ToolCallArgs,
  type ToolCallInput,
  type ToolCallResult,
  toolCallActivity,
  toolCallWorkflowId,
} from '../temporal/toolCall.js'
import { errorResult } from './registry.js'

// A turn's ScadBuddy tool calls as activities (unified runtime plan 2026-10-13 Step 9.2, R1 of
// #4773, decision Q1), behind the `tools_as_activities` setting. The turn's in-process server keeps
// projections.ts's names, schemas and descriptions, so neither the model nor Settings sees a
// change; only its handler differs: it starts the call's ToolCall workflow
// (`session-<uuid>-call-<tool_use_id>`, temporal/toolCall.ts), or attaches to it when it is
// already there (a re-send after a lost answer), and answers with its result. The call then runs
// on whichever agent-tools worker takes it, as `tool:<name>` (toolActivities.ts
// `toolCallActivities`), so a pod that dies while the turn waits loses the turn, not the call:
// the restarted turn is told which calls finished (`finishedToolCalls`) and reads what each
// returned with `tool_call_result` (`toolCallResult`), so the result reaches the model as a tool
// result, never in the user's message. Images come back as names in the session's blobs, never as
// bytes in a payload, and are read back here.

/** One call to run: its session and tool_use id name the workflow; `requestId` is the gate entry it was approved on, if it parked. */
export type ToolCallRequest = {
  sessionId: string
  toolUseId: string
  /** The registry tool's name. */
  tool: string
  input: Record<string, unknown>
  requestId?: string
  /** Aborted when the turn stops: the call's workflow is cancelled. */
  signal: AbortSignal
  /** The most the call may run: the turn's own limit (`turn_max_seconds`); the starter's default when absent. */
  timeoutMs?: number
}

export type ToolCallStarter = (request: ToolCallRequest) => Promise<CallToolResult>

/** The most one call may run when its turn names no limit: a turn's own default (runtimeSettings.ts `turn_max_seconds`). */
export const TOOL_CALL_TIMEOUT_MS = 7200 * 1000

/** gRPC's NOT_FOUND (https://grpc.github.io/grpc/core/md_doc_statuscodes.html): what the workflow service answers for an execution it does not have. */
export const GRPC_NOT_FOUND = 5

/** Whether the workflow service said it has no such execution (any longer). */
export function workflowNotFound(err: unknown): boolean {
  return err instanceof WorkflowNotFoundError || (err as { code?: unknown } | null)?.code === GRPC_NOT_FOUND
}

/** The deepest message of a failure chain: what the model reads of a call whose workflow failed. */
function failureMessage(err: unknown): string {
  let at: unknown = err
  let message = err instanceof Error ? err.message : String(err)
  while (at instanceof Error && at.cause !== undefined) {
    at = at.cause
    if (at instanceof Error && at.message) message = at.message
  }
  return message
}

/** A ToolCall's result as the model reads it: its text, and each image read back from the session's blobs. */
export async function callToolResult(sessionId: string, result: ToolCallResult, blobs: Pick<SessionBlobs, 'get'>): Promise<CallToolResult> {
  const content: CallToolResult['content'] = []
  for (const block of result.content) {
    if (block.type === 'text') {
      content.push({ type: 'text', text: block.text })
      continue
    }
    const stored = await blobs.get(sessionId, block.name)
    content.push(stored ? { type: 'image', data: stored.bytes.toString('base64'), mimeType: stored.mediaType } : { type: 'text', text: `[image ${block.name}: no longer kept]` })
  }
  return result.ok ? { content } : { isError: true, content }
}

/** Starts (or finds) each call's ToolCall on `client` and answers with its result; on `queue` (agent-tools by default), the workflow and its activity. */
export function toolCallStarter(deps: { client: Client; blobs: Pick<SessionBlobs, 'get'>; timeoutMs?: number; queue?: string }): ToolCallStarter {
  const queue = deps.queue ?? TASK_QUEUE
  return async (request) => {
    const workflowId = toolCallWorkflowId(request.sessionId, request.toolUseId)
    // The call's span (telemetry/turn.ts TurnTrace) is the activity's parent, as an in-process call's backend requests are its children.
    const traceparent = traceparentOf(trace.getSpan(otelContext.active()))
    const args: ToolCallArgs = {
      tool: request.tool,
      input: request.input,
      ...(request.requestId ? { requestId: request.requestId } : {}),
      ...(traceparent ? { traceparent } : {}),
    }
    const timeoutMs = request.timeoutMs ?? deps.timeoutMs ?? TOOL_CALL_TIMEOUT_MS
    const input: ToolCallInput = { activity: toolCallActivity(request.tool), queue, args, timeoutMs }
    let handle
    try {
      handle = await deps.client.workflow.start(TOOL_CALL_WORKFLOW, {
        workflowId,
        taskQueue: queue,
        args: [input],
        // A re-send of the same call finds its execution, open or closed, and its result.
        workflowIdConflictPolicy: 'USE_EXISTING',
        workflowIdReusePolicy: 'REJECT_DUPLICATE',
      })
    } catch (err) {
      if (!(err instanceof WorkflowExecutionAlreadyStartedError)) throw err
      handle = deps.client.workflow.getHandle(workflowId)
    }
    const cancel = () => void handle.cancel().catch(() => {})
    request.signal.addEventListener('abort', cancel, { once: true })
    if (request.signal.aborted) cancel()
    try {
      const result = (await handle.result()) as ToolCallResult
      return await callToolResult(request.sessionId, result, deps.blobs)
    } catch (err) {
      if (err instanceof WorkflowFailedError) return errorResult(`${request.tool} failed: ${failureMessage(err)}`)
      throw err
    } finally {
      request.signal.removeEventListener('abort', cancel)
    }
  }
}

/** The tool_use ids of a session's calls that have a `tool.call` and no `tool.result` in its events, since its seq `afterSeq`. */
async function openCalls(sql: Sql, sessionId: string, afterSeq: number): Promise<{ id: string; name: string }[]> {
  const rows = await sql<{ type: string; id: string; name: string | null }[]>`
    SELECT event::jsonb->>'type' AS type, event::jsonb->>'id' AS id, event::jsonb->>'name' AS name
      FROM ai_session_events
     WHERE session_id = ${sessionId} AND seq > ${afterSeq} AND event::jsonb->>'type' IN ('tool.call', 'tool.result')
     ORDER BY seq`
  const open = new Map<string, string>()
  for (const row of rows) {
    if (row.type === 'tool.call') open.set(row.id, row.name ?? '')
    else open.delete(row.id)
  }
  return [...open].map(([id, name]) => ({ id, name }))
}

/** A ToolCall that completed before a restart, for the restart message (turnHost.ts): which call, and whether it succeeded. Never what it returned. */
export type FinishedCall = { id: string; name: string; ok: boolean }

/**
 * The calls of turn `turnId` that were in flight when its placement was lost (a `tool.call` with no
 * `tool.result`) and whose ToolCall completed (plan Step 9.2): the restart message names them in
 * place of "unknown", and the model reads each one's result with `tool_call_result`. A call whose
 * workflow is still open, failed, or was never started (an in-process call) is left out: the
 * message says those are to be checked.
 */
export async function finishedToolCalls(deps: { sql: Sql; client: Client }, sessionId: string, turnId: string): Promise<FinishedCall[]> {
  const [start] = await deps.sql<{ seq: string }[]>`
    SELECT seq FROM ai_session_events
     WHERE session_id = ${sessionId} AND event::jsonb->>'type' = 'user.turn' AND event::jsonb->>'turnId' = ${turnId}
     ORDER BY seq DESC LIMIT 1`
  const calls = await openCalls(deps.sql, sessionId, start ? Number(start.seq) : 0)
  const out: FinishedCall[] = []
  for (const call of calls) {
    const handle = deps.client.workflow.getHandle(toolCallWorkflowId(sessionId, call.id))
    try {
      const { status } = await handle.describe()
      if (status.name !== 'COMPLETED') continue
      const result = (await handle.result()) as ToolCallResult
      out.push({ id: call.id, name: call.name, ok: result.ok })
    } catch (err) {
      if (!(err instanceof WorkflowNotFoundError)) throw err
    }
  }
  return out
}

/** What a session's completed ToolCall returned, as the model reads a tool result (`tool_call_result`); undefined while it runs, or when it never ran or failed. */
export async function toolCallResult(deps: { client: Client; blobs: Pick<SessionBlobs, 'get'> }, sessionId: string, toolUseId: string): Promise<CallToolResult | undefined> {
  const handle = deps.client.workflow.getHandle(toolCallWorkflowId(sessionId, toolUseId))
  try {
    if ((await handle.describe()).status.name !== 'COMPLETED') return undefined
    return await callToolResult(sessionId, (await handle.result()) as ToolCallResult, deps.blobs)
  } catch (err) {
    if (workflowNotFound(err)) return undefined
    throw err
  }
}

/**
 * Terminates a session's ToolCalls still open (forget.ts): every call its events show with no
 * result, and every execution of its id prefix still running, which a call started just before
 * its `tool.call` event was written is, all at once.
 */
export async function terminateOpenToolCalls(deps: { sql: Sql; client: Client }, sessionId: string, reason: string): Promise<void> {
  const ids = new Set((await openCalls(deps.sql, sessionId, 0)).map((call) => toolCallWorkflowId(sessionId, call.id)))
  const prefix = toolCallWorkflowId(sessionId, '')
  for await (const execution of deps.client.workflow.list({ query: `WorkflowId STARTS_WITH "${prefix}" AND ExecutionStatus = "Running"` })) {
    ids.add(execution.workflowId)
  }
  await Promise.all(
    [...ids].map(async (id) => {
      try {
        await deps.client.workflow.getHandle(id).terminate(reason)
      } catch (err) {
        // Never started (an in-process call), or closed already.
        if (!workflowNotFound(err)) throw err
      }
    }),
  )
}
