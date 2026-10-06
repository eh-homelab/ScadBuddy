import { HttpResponse, http } from 'msw'

/**
 * #815 — the agent's list of tool calls parked on the user (`GET
 * /api/v1/ai/pending-input`, agent/src/routes/pendingInput.ts), which the header's
 * attention badge counts (`src/agent/attention.ts`). Empty unless a test parks some:
 * the mocked agent's scripted approvals and questions (`../agent.ts`) ride its
 * socket, not this route.
 */

const state = { approvals: 0, questions: 0, attention: 0, done: 0 }

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

export function reset(): void {
  state.approvals = 0
  state.questions = 0
  state.attention = 0
  state.done = 0
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

export const handlers = [
  http.get('/api/v1/ai/pending-input', () =>
    HttpResponse.json({
      entries: [
        ...Array.from({ length: state.approvals }, (_, i) => approval(i)),
        ...Array.from({ length: state.questions }, (_, i) => answer(i, false)),
        ...Array.from({ length: state.attention }, (_, i) => answer(state.questions + i, true)),
        ...Array.from({ length: state.done }, (_, i) => answer(state.questions + state.attention + i, true, true)),
      ],
    }),
  ),
]
