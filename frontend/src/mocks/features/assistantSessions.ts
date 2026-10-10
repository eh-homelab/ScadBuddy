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
 *
 * #792 — a fork takes `up_to` (a reply's message id, #793), and `PATCH /sessions/:id`
 * renames a session or marks it done (#795), on the open mock agent as the real routes
 * do. `sessionWrites()` lists the forks and edits the panel sent, for tests.
 *
 * #1885 — `PATCH` also archives or unarchives (`archived`), and `GET /sessions?archived=true`
 * lists the open mock agent's archived sessions, as the switcher's Archived view reads them.
 *
 * #1125 — `GET /sessions?status=running` lists `setRunningSessions(n)` running sessions (none
 * by default), as the header's working indicator reads them; any other status lists none.
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
  /** #792 — every fork and edit, in order. */
  sessionWrites: [] as SessionWrite[],
  /** #1125 — how many sessions `?status=running` lists. */
  running: 0,
}

/** A fork or an edit the panel sent: the path under `/api/v1/ai` and the JSON body. */
export interface SessionWrite {
  method: 'POST' | 'PATCH'
  path: string
  body: Record<string, unknown>
}

export function sessionWrites(): readonly SessionWrite[] {
  return state.sessionWrites
}

export function reset(): void {
  state.limits = { ...DEFAULTS }
  state.writes = []
  state.sessionWrites = []
  state.running = 0
  state.resources = new Map([[EXTERNAL_SESSION_ID, [...EXTERNAL_RESOURCES]]])
  state.views = new Map([
    [EXTERNAL_SESSION_ID, view({ id: EXTERNAL_SESSION_ID, title: 'Tune the gridfinity bin', origin: 'mcp', turns: 1 })],
  ])
}
reset()

/** #1125 — how many of the user's sessions have a turn under way. */
export function setRunningSessions(n: number): void {
  state.running = n
}

/** agent `sessions/protocol.ts` SESSION_STATUSES. */
const STATUSES = ['running', 'waiting_input', 'waiting_approval', 'idle', 'done', 'failed']

/** #931 — what `GET /sessions/:id/resources` answers for one session, oldest first. */
export function setSessionResources(
  sessionId: string,
  resources: readonly SessionResource[],
  session: Partial<AiSessionView> = {},
): void {
  state.resources.set(sessionId, [...resources])
  state.views.set(sessionId, view({ ...state.views.get(sessionId), ...session, id: sessionId }))
}

/** Every kind the agent looks up (agent `sessions/touched.ts` LOOKUP_TYPES); a kind missing here fails typecheck. */
const LOOKUP: Record<ResourceRef['type'], true> = {
  model: true,
  revision: true,
  preset: true,
  asset: true,
  render_job: true,
  output: true,
  print_run: true,
  print: true,
  library: true,
  font: true,
  setting: true,
  project: true,
  bambuddy_file: true,
  print_archive: true,
}
const VALID_TYPES: readonly string[] = Object.keys(LOOKUP)

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

  // #1288 — one session: a 404 for one that neither the open mock agent nor a test knows.
  http.get(`${base}/sessions/:id`, ({ params }) => {
    const id = String(params.id)
    const known = state.views.get(id) ?? (mockAgentSessions()?.has(id) ? view({ id }) : undefined)
    return known ? HttpResponse.json(known) : detail('session not found', 404)
  }),

  http.get(`${base}/sessions/:id/resources`, ({ params }) => {
    const id = String(params.id)
    const resources = state.resources.get(id) ?? (mockAgentSessions()?.has(id) ? [] : undefined)
    return resources ? HttpResponse.json({ resources }) : detail('session not found', 404)
  }),

  http.get(`${base}/resources/:type/:id/sessions`, ({ params, request }) => {
    const type = String(params.type)
    if (!VALID_TYPES.includes(type)) return detail(`resource type must be one of ${VALID_TYPES.join(', ')}`, 400)
    const ref = { type, id: String(params.id) } as ResourceRef
    const sessions = [...state.resources]
      .filter(([, rows]) => touched(rows, ref))
      .map(([id]) => state.views.get(id) ?? view({ id }))
      .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
      .slice(0, Number(new URL(request.url).searchParams.get('limit') ?? 100))
    return HttpResponse.json({ sessions })
  }),

  http.post(`${base}/sessions/:id/fork`, async ({ params, request }) => {
    const raw = await request.text()
    const body = (raw.trim() ? JSON.parse(raw) : {}) as Record<string, unknown>
    state.sessionWrites.push({ method: 'POST', path: `/sessions/${String(params.id)}/fork`, body })
    const extra = Object.keys(body).filter((k) => k !== 'title' && k !== 'up_to')
    if (extra.length) return detail(`body: unrecognized key(s) ${extra.join(', ')}`, 400)
    const agent = mockAgentSessions()
    if (!agent) return detail('the assistant is not connected', 503)
    const forked = agent.fork(String(params.id), typeof body.up_to === 'string' ? body.up_to : undefined)
    if ('error' in forked) return detail(forked.error, forked.status)
    return HttpResponse.json(
      { session: view({ id: forked.id, title: forked.title, parent_id: forked.parentId, budget_usd: forked.budgetUsd }) },
      { status: 201 },
    )
  }),

  http.patch(`${base}/sessions/:id`, async ({ params, request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const id = String(params.id)
    state.sessionWrites.push({ method: 'PATCH', path: `/sessions/${id}`, body })
    const extra = Object.keys(body).filter((k) => k !== 'title' && k !== 'done' && k !== 'archived')
    if (extra.length) return detail(`body: unrecognized key(s) ${extra.join(', ')}`, 400)
    const title = typeof body.title === 'string' ? body.title.trim() : undefined
    if (title === '' || (title?.length ?? 0) > 200) return detail('title: must be 1 to 200 characters', 400)
    if (body.done !== undefined && body.done !== true) return detail('done: must be true', 400)
    if (body.archived !== undefined && typeof body.archived !== 'boolean') return detail('archived: must be a boolean', 400)
    if (title === undefined && body.done === undefined && body.archived === undefined) {
      return detail('body: name a title, done: true, or archived', 400)
    }
    const agent = mockAgentSessions()
    if (!agent) return detail('the assistant is not connected', 503)
    const updated = agent.update(id, {
      ...(title === undefined ? {} : { title }),
      ...(body.done ? { done: true } : {}),
      ...(typeof body.archived === 'boolean' ? { archived: body.archived } : {}),
    })
    if ('error' in updated) return detail(updated.error, updated.status)
    return HttpResponse.json({
      session: view({
        id,
        title: updated.title,
        status: updated.status,
        parent_id: updated.parentId,
        cost_usd: updated.costUsd,
        budget_usd: updated.budgetUsd,
        updated_at: new Date().toISOString(),
        archived: updated.archivedAt !== null,
        archived_at: updated.archivedAt,
      }),
    })
  }),

  http.get(`${base}/sessions`, ({ request }) => {
    const query = new URL(request.url).searchParams
    const status = query.get('status')
    if (status !== null) {
      if (!STATUSES.includes(status)) return detail(`status must be one of ${STATUSES.join(', ')}`, 400)
      const n = status === 'running' ? Math.min(state.running, Number(query.get('limit') ?? 50)) : 0
      return HttpResponse.json({
        sessions: Array.from({ length: n }, (_, i) =>
          view({ id: `running-${i + 1}`, title: 'Working', status: 'running', running: true }),
        ),
      })
    }
    const archived = query.get('archived')
    if (archived !== null && !['true', 'false', 'include'].includes(archived)) {
      return detail('archived must be one of true, include, false', 400)
    }
    const agent = mockAgentSessions()
    if (!agent) return detail('the assistant is not connected', 503)
    // Only the archive view's read is modelled: the panel's main list is the socket's snapshot.
    if (archived !== 'true') return detail('the mock lists only archived sessions (?archived=true)', 400)
    return HttpResponse.json({
      sessions: agent.archived().map((s) =>
        view({
          id: s.id,
          title: s.title,
          origin: s.origin,
          status: s.status,
          parent_id: s.parentId,
          cost_usd: s.costUsd,
          budget_usd: s.budgetUsd,
          updated_at: s.archivedAt,
          archived: true,
          archived_at: s.archivedAt,
        }),
      ),
    })
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
