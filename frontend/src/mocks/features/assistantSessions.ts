/**
 * #790 — the agent service's session budget routes for vitest and the mocked build:
 * the same paths, bodies and bounds as agent `src/routes/sessionLimits.ts` and
 * `src/routes/sessions.ts` (fork, raise a budget). Fork and raise act on the open
 * scripted agent (`../agent.ts` `mockAgentSessions`), so the panel sees the new session
 * or the raised budget over its socket, as it does against the real agent.
 */
import { HttpResponse, http } from 'msw'
import type { AiSessionView, SessionLimits } from '../../api/types'
import { mockAgentSessions } from '../agent'

const base = '/api/v1/ai'

/** agent `sessions/manager.ts` MAX_SESSION_BUDGET_USD and MAX_SESSION_MAX_TURNS. */
export const MAX_BUDGET_USD = 100
export const MAX_TURNS = 200
const DEFAULTS: SessionLimits = { budget_usd: 1, max_turns: 25 }

const state = {
  limits: { ...DEFAULTS },
  /** Every limits write, for tests. */
  writes: [] as SessionLimits[],
}

export function reset(): void {
  state.limits = { ...DEFAULTS }
  state.writes = []
}

export function mockSessionLimitWrites(): readonly SessionLimits[] {
  return state.writes
}

const detail = (text: string, status: number) => HttpResponse.json({ detail: text }, { status })

function view(fields: Partial<AiSessionView> & Pick<AiSessionView, 'id'>): AiSessionView {
  return { title: '', parent_id: null, turns: 0, cost_usd: 0, budget_usd: 1, running: false, ...fields }
}

export const handlers = [
  http.get(`${base}/settings/session-limits`, () => HttpResponse.json(state.limits)),

  http.put(`${base}/settings/session-limits`, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = Object.keys(body).filter((k) => k !== 'budget_usd' && k !== 'max_turns')
    const { budget_usd, max_turns } = body
    if (extra.length) return detail(`body: unrecognized key(s) ${extra.join(', ')}`, 400)
    if (typeof budget_usd !== 'number' || budget_usd < 0.01 || budget_usd > MAX_BUDGET_USD) {
      return detail(`budget_usd: must be from 0.01 to ${MAX_BUDGET_USD}`, 400)
    }
    if (typeof max_turns !== 'number' || !Number.isInteger(max_turns) || max_turns < 1 || max_turns > MAX_TURNS) {
      return detail(`max_turns: must be an integer from 1 to ${MAX_TURNS}`, 400)
    }
    state.limits = { budget_usd: Math.round(budget_usd * 100) / 100, max_turns }
    state.writes.push(state.limits)
    return HttpResponse.json(state.limits)
  }),

  http.post(`${base}/sessions/:id/fork`, ({ params }) => {
    const agent = mockAgentSessions()
    if (!agent) return detail('the assistant is not connected', 503)
    const forked = agent.fork(String(params.id))
    if ('error' in forked) return detail(forked.error, forked.status)
    return HttpResponse.json(
      { session: view({ id: forked.id, title: forked.title, parent_id: forked.parentId, budget_usd: forked.budgetUsd }) },
      { status: 201 },
    )
  }),

  http.post(`${base}/sessions/:id/budget`, async ({ params, request }) => {
    const agent = mockAgentSessions()
    if (!agent) return detail('the assistant is not connected', 503)
    const { add_usd } = (await request.json()) as { add_usd?: unknown }
    if (typeof add_usd !== 'number' || add_usd < 0.01 || add_usd > MAX_BUDGET_USD) {
      return detail(`add_usd: must be from 0.01 to ${MAX_BUDGET_USD}`, 400)
    }
    const raised = agent.raise(String(params.id), add_usd)
    if ('error' in raised) return detail(raised.error, raised.status)
    return HttpResponse.json({
      session: view({ id: String(params.id), cost_usd: raised.costUsd, budget_usd: raised.budgetUsd }),
    })
  }),
]
