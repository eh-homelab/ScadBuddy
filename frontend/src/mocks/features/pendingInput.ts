import { HttpResponse, http } from 'msw'

/**
 * #815 — the agent's list of pending approvals (`GET /api/v1/ai/approvals`,
 * agent/src/routes/approvals.ts), which the header's attention badge counts
 * (`src/agent/attention.ts`). Empty unless a test parks some: the mocked agent's
 * scripted approvals (`../agent.ts`) ride its socket, not this route.
 */

const state = { pending: 0 }

/** Tests: how many approvals the agent says are waiting. */
export function setPendingApprovals(n: number): void {
  state.pending = n
}

export function reset(): void {
  state.pending = 0
}

function approval(i: number) {
  const at = new Date(Date.UTC(2026, 9, 1, 9, 0) + i * 1000).toISOString()
  return {
    id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`,
    session_id: null,
    tool_use_id: `toolu_${i}`,
    tool: 'send_to_bambuddy',
    input_summary: '{}',
    input_hash: '0'.repeat(64),
    tier: 'outward',
    requested_by: { kind: 'browser', id: 'browser', label: 'You' },
    created_at: at,
    expires_at: at,
    decision: null,
    decided_by: null,
    decided_at: null,
    reason: null,
    used: false,
    voided: false,
  }
}

export const handlers = [
  http.get('/api/v1/ai/approvals', () =>
    HttpResponse.json({ approvals: Array.from({ length: state.pending }, (_, i) => approval(i)) }),
  ),
]
