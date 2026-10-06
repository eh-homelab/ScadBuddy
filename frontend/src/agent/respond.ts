import type { Question } from './chat/protocol'

/**
 * #815 — the panel's one way to answer anything the assistant waits on: `POST
 * /api/v1/ai/pending-input/{request_id}` (agent `src/routes/pendingInput.ts`,
 * durable-agents spec §6.6 `respond`). The request id is the pending-input read's
 * (`approval:<id>` or `question:<id>`), and the body names the entry's kind; the agent
 * refuses one that is not the entry's, or that answers an entry no longer pending.
 */

export const respondPath = (requestId: string) => `/api/v1/ai/pending-input/${encodeURIComponent(requestId)}`

export type RespondBody =
  | { kind: 'approval'; decision: 'approve' | 'deny' }
  | { kind: 'answer'; answers: Record<string, string> }
  | { kind: 'answer'; choice: string }
  | { kind: 'answer'; text: string }

/** The body for an approval card's decision. */
export function decisionBody(approve: boolean): RespondBody {
  return { kind: 'approval', decision: approve ? 'approve' : 'deny' }
}

/**
 * The body for a question card's answers (one per question, in order). An attention
 * request has one question: a picked option is its `choice`, anything else the user's
 * own `text`.
 */
export function answerBody(questions: readonly Question[], answers: readonly string[], attention: boolean): RespondBody {
  if (attention) {
    const answer = answers[0] ?? ''
    return questions[0]?.options.some((o) => o.label === answer) ? { kind: 'answer', choice: answer } : { kind: 'answer', text: answer }
  }
  return { kind: 'answer', answers: Object.fromEntries(questions.map((q, i) => [q.question, answers[i] ?? ''])) }
}

/** What the agent recorded for a response. */
export type RespondOutcome = 'approved' | 'denied' | 'answered'

/**
 * A refused response; `status` is undefined when the agent was not reached. `reason`
 * is the agent's account of how a 409's entry ended ("it was already answered",
 * "nobody replied in time …"), when it gave one (#1400).
 */
export class RespondError extends Error {
  readonly status: number | undefined
  readonly reason: string | undefined
  /** A 404 the agent itself marked stale; a bare 404 (a proxy, an older replica) is not. */
  readonly stale: boolean
  constructor(message: string, status?: number, reason?: string, stale = false) {
    super(message)
    this.status = status
    this.reason = reason
    this.stale = stale
  }
  /**
   * The entry is stale (the agent's own 404), no longer pending (409) or expired
   * (410): answering again cannot help (#1403).
   */
  get settled(): boolean {
    return (this.status === 404 && this.stale) || this.status === 409 || this.status === 410
  }
}

/** Sends a response and returns what the agent recorded; rejects (RespondError) when it is refused or unreachable. */
export async function respond(requestId: string, body: RespondBody): Promise<RespondOutcome> {
  let res: Response
  try {
    res = await fetch(respondPath(requestId), {
      method: 'POST',
      headers: { 'content-type': 'application/json', accept: 'application/json' },
      body: JSON.stringify(body),
    })
  } catch {
    throw new RespondError('The assistant could not be reached; try again.')
  }
  if (res.ok) return ((await res.json().catch(() => ({}))) as { outcome?: RespondOutcome }).outcome ?? fallback(body)
  let detail = `HTTP ${res.status}`
  let reason: string | undefined
  let stale = false
  try {
    const parsed = (await res.json()) as { detail?: unknown; reason?: unknown; stale?: unknown }
    if (typeof parsed.detail === 'string') detail = parsed.detail
    if (typeof parsed.reason === 'string') reason = parsed.reason
    stale = parsed.stale === true
  } catch {
    // Not JSON: keep the status.
  }
  throw new RespondError(detail, res.status, reason, stale)
}

function fallback(body: RespondBody): RespondOutcome {
  return body.kind === 'answer' ? 'answered' : body.decision === 'approve' ? 'approved' : 'denied'
}
