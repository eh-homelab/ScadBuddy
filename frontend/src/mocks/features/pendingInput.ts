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
 * answers what the open scripted agent is parked on; every request is kept for tests
 * (`respondRequests`).
 */

const state = { approvals: 0, questions: 0, attention: 0 }
let responses: { id: string; body: RespondBody }[] = []

/** Tests: every respond the panel sent, in order. */
export function respondRequests(): readonly { id: string; body: RespondBody }[] {
  return responses
}

/** Tests: how many approvals the agent says are waiting. */
export function setPendingApprovals(n: number): void {
  state.approvals = n
}

/** Tests: how many questions and attention requests the agent says are waiting. */
export function setPendingAnswers(questions: number, attention = 0): void {
  state.questions = questions
  state.attention = attention
}

export function reset(): void {
  state.approvals = 0
  state.questions = 0
  state.attention = 0
  responses = []
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

function answer(i: number, attention: boolean) {
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
    expires_at: attention ? at(i + 300) : null,
    ...(attention ? { attention: { reason: 'tab_disconnected', on_timeout: 'proceed' } } : {}),
  }
}

export const handlers = [
  http.post('/api/v1/ai/pending-input/:id', async ({ params, request }) => {
    const id = String(params.id)
    const body = (await request.json()) as RespondBody
    responses.push({ id, body })
    const agent = mockAgentSessions()
    if (!agent) return HttpResponse.json({ detail: 'the assistant is not connected' }, { status: 503 })
    const result = agent.respond(id, body)
    if ('error' in result) {
      return HttpResponse.json({ detail: result.error, ...(result.status === 404 ? { stale: true } : {}) }, { status: result.status })
    }
    return HttpResponse.json({ id, kind: body.kind, outcome: body.kind === 'answer' ? 'answered' : body.decision === 'approve' ? 'approved' : 'denied' })
  }),
  http.get('/api/v1/ai/pending-input', () =>
    HttpResponse.json({
      entries: [
        ...Array.from({ length: state.approvals }, (_, i) => approval(i)),
        ...Array.from({ length: state.questions }, (_, i) => answer(i, false)),
        ...Array.from({ length: state.attention }, (_, i) => answer(state.questions + i, true)),
      ],
    }),
  ),
]
