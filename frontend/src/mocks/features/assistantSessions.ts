/**
 * #790 — the agent service's session budget routes for vitest and the mocked build:
 * the same paths, bodies and bounds as agent `src/routes/sessionLimits.ts` and
 * `src/routes/sessions.ts` (fork, raise a budget). Fork and raise act on the open
 * scripted agent (`../agent.ts` `mockAgentSessions`), so the panel sees the new session
 * or the raised budget over its socket, as it does against the real agent.
 *
 * #931 — what a session touched (`GET /sessions/:id/resources`) is a list per session
 * id that tests set with `setSessionResources`; the desktop agent's session
 * (`EXTERNAL_SESSION_ID`) starts with what its script did, so the mocked build shows
 * the panel filled. Any other session the open mock agent knows touched nothing; one it
 * does not know is a 404, as the real route refuses a session it cannot find or show.
 *
 * #931 — the reverse, `GET /resources/:type/:id/sessions`, is read off the same lists: a
 * session is listed when one of its rows matches, a `model` by the row's model and any
 * other kind by its id, as agent `sessions/manager.ts` `listQuery` does. Its title is
 * what `setSessionResources` was given.
 */
import { HttpResponse, http } from 'msw'
import type { AiSessionView, ResourceRef, SessionLimits, SessionResource } from '../../api/types'
import { EXTERNAL_SESSION_ID, mockAgentSessions } from '../agent'

const base = '/api/v1/ai'

/** agent `sessions/manager.ts` MAX_SESSION_BUDGET_USD and MAX_SESSION_MAX_TURNS. */
export const MAX_BUDGET_USD = 100
export const MAX_TURNS = 200
const DEFAULTS: SessionLimits = { budget_usd: 1, max_turns: 25 }

/** What the desktop agent's scripted session (`../agent.ts`) changed. */
const EXTERNAL_RESOURCES: readonly SessionResource[] = [
  {
    type: 'revision',
    id: '3f9c2a1b7d4e',
    action: 'created',
    model: 'gridfinity-bin',
    before: null,
    after: '3f9c2a1b7d4e',
    tool: 'update_source',
    at: '2026-10-03T09:00:00.000Z',
  },
  {
    type: 'preset',
    id: 'preset-tall',
    action: 'created',
    model: 'gridfinity-bin',
    before: null,
    after: null,
    tool: 'save_preset',
    at: '2026-10-03T09:00:05.000Z',
  },
]

const state = {
  limits: { ...DEFAULTS },
  /** Every limits write, for tests. */
  writes: [] as SessionLimits[],
  resources: new Map<string, SessionResource[]>(),
  /** The session each resource list belongs to, as the reverse lookup shows it. */
  views: new Map<string, AiSessionView>(),
}

export function reset(): void {
  state.limits = { ...DEFAULTS }
  state.writes = []
  state.resources = new Map([[EXTERNAL_SESSION_ID, [...EXTERNAL_RESOURCES]]])
  state.views = new Map([
    [EXTERNAL_SESSION_ID, view({ id: EXTERNAL_SESSION_ID, title: 'Tune the gridfinity bin', origin: 'mcp', turns: 1 })],
  ])
}
reset()

/** #931 — what `GET /sessions/:id/resources` answers for one session, oldest first. */
export function setSessionResources(
  sessionId: string,
  resources: readonly SessionResource[],
  session: Partial<AiSessionView> = {},
): void {
  state.resources.set(sessionId, [...resources])
  state.views.set(sessionId, view({ ...state.views.get(sessionId), ...session, id: sessionId }))
}

const VALID_TYPES: readonly string[] = ['model', 'revision', 'preset', 'asset', 'render_job', 'output', 'print_run', 'print']

function touched(rows: readonly SessionResource[], ref: ResourceRef): boolean {
  return rows.some((r) => (ref.type === 'model' ? r.model === ref.id : r.type === ref.type && r.id === ref.id))
}

export function mockSessionLimitWrites(): readonly SessionLimits[] {
  return state.writes
}

const detail = (text: string, status: number) => HttpResponse.json({ detail: text }, { status })

function view(fields: Partial<AiSessionView> & Pick<AiSessionView, 'id'>): AiSessionView {
  return {
    title: '',
    origin: 'chat',
    status: 'idle',
    updated_at: '2026-10-03T09:00:05.000Z',
    parent_id: null,
    turns: 0,
    cost_usd: 0,
    budget_usd: 1,
    running: false,
    ...fields,
  }
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

  http.get(`${base}/sessions/:id/resources`, ({ params }) => {
    const id = String(params.id)
    const resources = state.resources.get(id) ?? (mockAgentSessions()?.has(id) ? [] : undefined)
    return resources ? HttpResponse.json({ resources }) : detail('session not found', 404)
  }),

  http.get(`${base}/resources/:type/:id/sessions`, ({ params }) => {
    const type = String(params.type)
    if (!VALID_TYPES.includes(type)) return detail(`resource type must be one of ${VALID_TYPES.join(', ')}`, 400)
    const ref = { type, id: String(params.id) } as ResourceRef
    const sessions = [...state.resources]
      .filter(([, rows]) => touched(rows, ref))
      .map(([id]) => state.views.get(id) ?? view({ id }))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    return HttpResponse.json({ sessions })
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
