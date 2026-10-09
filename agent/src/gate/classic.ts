import { event, type InputEntry, type InputOutcome, type Owner, type QuestionView, type ServerEvent } from '../sessions/protocol.js'

// A classic session's parked calls in the gate's one entry shape (durable-agents spec
// §6.6, "Classic sessions"): an ai_approvals row (`approval:<id>`) or an ai_questions
// row (`question:<id>`). The pending-input reads list them, and the stores log
// `input.requested` / `input.resolved` with them beside the cards' own events.

/** The columns of an approval its entry needs. */
export type ApprovalShape = {
  id: string
  sessionId: string | null
  tool: string
  inputSummary: string
  inputHash: string
  requestedBy: Owner
  createdAt: string
  expiresAt: string
}

/** The columns of a question (or attention request) its entry needs. */
export type QuestionShape = {
  id: string
  sessionId: string
  tool: string
  questions: readonly Pick<QuestionView, 'question'>[]
  attentionReason: string | null
  onTimeout: string | null
  summary: string | null
  createdAt: string
  expiresAt: string | null
}

export function approvalEntry(a: ApprovalShape): InputEntry {
  return {
    id: `approval:${a.id}`,
    kind: 'approval',
    session_id: a.sessionId,
    tool: a.tool,
    summary: a.inputSummary,
    input_hash: a.inputHash,
    prompt: '',
    requested_by: a.requestedBy,
    responders: ['browser', 'grant'],
    created_at: a.createdAt,
    expires_at: a.expiresAt,
  }
}

export function questionEntry(q: QuestionShape): InputEntry {
  return {
    id: `question:${q.id}`,
    kind: 'answer',
    session_id: q.sessionId,
    tool: q.tool,
    summary: '',
    input_hash: null,
    prompt: q.questions.map((v) => v.question).join('\n'),
    requested_by: null,
    responders: ['browser'],
    created_at: q.createdAt,
    expires_at: q.expiresAt,
    ...(q.attentionReason
      ? { attention: { reason: q.attentionReason, on_timeout: q.onTimeout, ...(q.summary === null ? {} : { summary: q.summary }) } }
      : {}),
  }
}

/** `input.requested` for an entry of `sessionId`. */
export function inputRequested(sessionId: string, entry: InputEntry): ServerEvent {
  return event({ type: 'input.requested', sessionId, entry })
}

/** `input.resolved`; a question's `reconnected` (#815 §2) is a cancellation by the system, with its reason. */
export function inputResolved(
  sessionId: string,
  id: string,
  kind: InputEntry['kind'],
  outcome: InputOutcome | 'reconnected',
  reason?: string | null,
): ServerEvent {
  return event({
    type: 'input.resolved',
    sessionId,
    id,
    kind,
    outcome: outcome === 'reconnected' ? 'cancelled' : outcome,
    ...(reason ? { reason } : {}),
  })
}
