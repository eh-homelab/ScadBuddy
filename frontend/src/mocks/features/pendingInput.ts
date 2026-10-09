import { HttpResponse, http } from 'msw'
import type { RespondBody } from '../../agent/respond'
import { mockAgentSessions } from '../agent'

/**
 * #815 — the agent's list of tool calls parked on the user (`GET
 * /api/v1/ai/pending-input`, agent/src/routes/pendingInput.ts), which the header's
 * attention badge counts (`src/agent/attention.ts`). Empty unless a test parks some:
 * the mocked agent's scripted approvals and questions (`../agent.ts`) ride its
 * socket, not this read.
 *
 * Its respond route (`POST /api/v1/ai/pending-input/{id}`, `src/agent/respond.ts`)
 * answers an entry a test listed here (it leaves the list) and what the open scripted
 * agent is parked on, and refuses one no longer waiting with a 409 that says how it
 * ended (`reason`), and a body of the wrong kind with a 400, as the agent does; every
 * request is kept for tests (`respondRequests`).
 */

const state = { approvals: 0, questions: 0, attention: 0, done: 0, summariesTruncated: false }
let responses: { id: string; body: RespondBody }[] = []
/** Listed entries already responded to, with how each ended. */
const ended = new Map<string, string>()

/** Tests: every respond the panel sent, in order. */
export function respondRequests(): readonly { id: string; body: RespondBody }[] {
  return responses
}

/** Tests: how many approvals the agent says are waiting. */
export function setPendingApprovals(n: number): void {
  state.approvals = n
}

/** Tests: how many questions, attention requests and `done` summaries the agent lists. */
export function setPendingAnswers(questions: number, attention = 0, done = 0): void {
  state.questions = questions
  state.attention = attention
  state.done = done
}

/** Tests: whether the agent says it listed only some of the `done` summaries (`summaries_truncated`). */
export function setSummariesTruncated(truncated: boolean): void {
  state.summariesTruncated = truncated
}

export function reset(): void {
  state.approvals = 0
  state.questions = 0
  state.attention = 0
  responses = []
  state.done = 0
  state.summariesTruncated = false
  ended.clear()
}

const at = (i: number) => new Date(Date.UTC(2026, 9, 1, 9, 0) + i * 1000).toISOString()
const id = (i: number) => `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`

function approval(i: number) {
  return {
    id: `approval:${id(i)}`,
    kind: 'approval',
    session_id: null,
    tool: 'send_to_bambuddy',
    summary: '{}',
    input_hash: '0'.repeat(64),
    prompt: '',
    requested_by: { kind: 'browser', id: 'browser', label: 'You' },
    responders: ['browser', 'grant'],
    created_at: at(i),
    expires_at: at(i),
  }
}

function answer(i: number, attention: boolean, done = false) {
  return {
    id: `question:${id(1000 + i)}`,
    kind: 'answer',
    session_id: id(2000 + i),
    tool: attention ? 'mcp__scadbuddy_questions__request_user_attention' : 'AskUserQuestion',
    summary: '',
    input_hash: null,
    prompt: attention ? 'The ScadBuddy tab closed; reopen it?' : 'Which colour?',
    requested_by: null,
    responders: ['browser'],
    created_at: at(i),
    expires_at: attention && !done ? at(i + 300) : null,
    ...(done
      ? { attention: { reason: 'done', on_timeout: null, summary: '**What this turn changed**\n- nothing' } }
      : attention
        ? { attention: { reason: 'tab_disconnected', on_timeout: 'proceed' } }
        : {}),
  }
}

function listed(): ReturnType<typeof approval | typeof answer>[] {
  return [
    ...Array.from({ length: state.approvals }, (_, i) => approval(i)),
    ...Array.from({ length: state.questions }, (_, i) => answer(i, false)),
    ...Array.from({ length: state.attention }, (_, i) => answer(state.questions + i, true)),
    ...Array.from({ length: state.done }, (_, i) => answer(state.questions + state.attention + i, true, true)),
  ]
}

const outcomeOf = (body: RespondBody) => (body.kind === 'answer' ? 'answered' : body.decision === 'approve' ? 'approved' : 'denied')

export const handlers = [
  http.post('/api/v1/ai/pending-input/:id', async ({ params, request }) => {
    const id = String(params.id)
    const body = (await request.json()) as RespondBody
    responses.push({ id, body })
    const entry = listed().find((e) => e.id === id)
    if (entry) {
      const how = ended.get(id)
      if (how !== undefined) return HttpResponse.json({ detail: `${id} is no longer waiting`, reason: how }, { status: 409 })
      if (body.kind !== entry.kind) return HttpResponse.json({ detail: `${id} is not ${body.kind === 'approval' ? 'an approval' : 'a question'}` }, { status: 400 })
      const outcome = outcomeOf(body)
      ended.set(id, `it was already ${outcome}`)
      return HttpResponse.json({ id, kind: body.kind, outcome })
    }
    const agent = mockAgentSessions()
    if (!agent) return HttpResponse.json({ detail: 'the assistant is not connected' }, { status: 503 })
    const result = agent.respond(id, body)
    if ('error' in result) {
      return HttpResponse.json(
        { detail: result.error, ...(result.reason === undefined ? {} : { reason: result.reason }), ...(result.status === 404 ? { stale: true } : {}) },
        { status: result.status },
      )
    }
    return HttpResponse.json({ id, kind: body.kind, outcome: outcomeOf(body) })
  }),
  http.get('/api/v1/ai/pending-input', () =>
    HttpResponse.json({ entries: listed().filter((e) => !ended.has(e.id)), summaries_truncated: state.summariesTruncated }),
  ),
]
