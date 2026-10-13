import { isUuid } from '../harness/stateDirs.js'

// The request ids of the tool-call gate (durable-agents spec §6.6, "Reads"). Opaque to
// clients; the prefix names the store. Only the respond route's prefix dispatch and the
// orphan sweep parse one, so a change to the shape is made here and in
// agent-durable's gate/ids.py, which the shared vectors pin to this.
//
//   approval:<uuid>                                   an ai_approvals row
//   question:<uuid>                                   an ai_questions row
//   durable:<session id>:<workflow run id>:<tool_use_id>
//                                                     a durable session's parked call; the
//                                                     run id makes a Reset's replayed park a
//                                                     new entry. The tool_use_id is the rest,
//                                                     colons included.
//   flow:<run id>:<workflow run id>:<call id>         a flow run's (phase 6)

export type RequestId =
  | { store: 'approval' | 'question'; rowId: string }
  | { store: 'durable'; sessionId: string; runId: string; toolUseId: string }
  | { store: 'flow'; runId: string; workflowRunId: string; callId: string }

/** The id, or undefined for anything malformed: an unknown prefix, a missing part, a non-UUID row or session. */
export function parseRequestId(id: string): RequestId | undefined {
  const colon = id.indexOf(':')
  if (colon < 0) return undefined
  const prefix = id.slice(0, colon)
  const rest = id.slice(colon + 1)
  if (prefix === 'approval' || prefix === 'question') return isUuid(rest) ? { store: prefix, rowId: rest } : undefined
  if (prefix !== 'durable' && prefix !== 'flow') return undefined
  const first = rest.indexOf(':')
  const second = first < 0 ? -1 : rest.indexOf(':', first + 1)
  if (second < 0) return undefined
  const [a, b, c] = [rest.slice(0, first), rest.slice(first + 1, second), rest.slice(second + 1)]
  if (!a || !b || !c) return undefined
  if (prefix === 'durable') return isUuid(a) ? { store: 'durable', sessionId: a.toLowerCase(), runId: b, toolUseId: c } : undefined
  return { store: 'flow', runId: a, workflowRunId: b, callId: c }
}

/** A durable session's entry id; the activity on `agent-tools` derives the same from its activity info. */
export function durableRequestId(sessionId: string, runId: string, toolUseId: string): string {
  return `durable:${sessionId.toLowerCase()}:${runId}:${toolUseId}`
}

/** A flow run's call's entry id, as the backend records its decision (flows/operations.py `request_id`). */
export function flowRequestId(runId: string, workflowRunId: string, callId: string): string {
  return `flow:${runId}:${workflowRunId}:${callId}`
}
