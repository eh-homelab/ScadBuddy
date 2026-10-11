import { HttpResponse, http } from 'msw'
import type { FlowPending, FlowRun, FlowRunView, FlowStep } from '../../api/types'

/**
 * #1057 — flow runs (backend `api/flows.py`): the list, one run's view, and a person's
 * answer and decision. An answer or decision to a call no longer waiting is the
 * backend's 409 `stale-entry`. Every answer and decision is kept, with its
 * `Idempotency-Key`, for tests (`flowRequests`).
 */

const at = (minutes: number) => new Date(Date.UTC(2026, 9, 10, 9, minutes)).toISOString()
const STALE = 'https://scadbuddy.dev/problems/stale-entry'

function step(seq: number, fn: string, status: FlowStep['status'], ended = true): FlowStep {
  return {
    seq,
    fn,
    call_id: `call-${seq}`,
    status,
    outward: fn === 'queue_print',
    started_at: at(seq),
    ended_at: ended ? at(seq + 1) : null,
    history_length: 10 * seq,
  }
}

function run(id: string, fields: Partial<FlowRun>): FlowRun {
  return {
    id,
    definition_id: 'def-swap',
    version: 1,
    name: 'swap',
    status: 'running',
    waiting_on: [],
    steps: [],
    result: null,
    result_truncated: false,
    approval_timeout_s: 0,
    workflow_id: `flow-${id}`,
    workflow_run_id: `wr-${id}`,
    started_by: { kind: 'browser' },
    created_at: at(0),
    updated_at: at(0),
    ...fields,
  }
}

function defaults(): FlowRun[] {
  return [
    run('run-ask', {
      status: 'waiting',
      steps: [step(1, 'render', 'succeeded'), step(2, 'wait_for_human', 'running', false)],
      waiting_on: [{ call_id: 'call-2', kind: 'answer', fn: 'wait_for_human', prompt: 'Swap to pink?', since: at(2) }],
      started_by: { kind: 'agent', principal: 'p', session: 'sess-1' },
      created_at: at(3),
    }),
    run('run-print', {
      name: 'plates',
      version: 2,
      status: 'waiting',
      steps: [step(1, 'queue_print', 'running', false)],
      waiting_on: [{ call_id: 'call-1', kind: 'approval', fn: 'queue_print', since: at(1) }],
      created_at: at(2),
    }),
    run('run-done', { status: 'succeeded', steps: [step(1, 'sleep', 'succeeded')], result: 'result: 7', created_at: at(1) }),
  ]
}

let runs = defaults()
/** Runs whose view says the workflow did not answer (`live: false`). */
const offline = new Set<string>()
/** Runs that exist only as an accepted start (`starting`, no row yet). */
const starting = new Set<string>()
let requests: { path: string; key: string | null; body: Record<string, unknown> }[] = []

export function reset(): void {
  runs = defaults()
  offline.clear()
  starting.clear()
  requests = []
}

/** Tests: the runs listed (newest first) and viewed. */
export function setFlowRuns(next: FlowRun[]): void {
  runs = next
}

/** Tests: a run whose live status is unavailable, or one only just started. */
export function setFlowRunOffline(id: string): void {
  offline.add(id)
}
export function setFlowRunStarting(id: string): void {
  starting.add(id)
}

/** Tests: the run moved on, so what its page shows is no longer waiting. */
export function moveFlowRunOn(id: string): void {
  const found = runs.find((r) => r.id === id)
  if (found) found.waiting_on = []
}

/** Tests: every answer and decision sent, in order. */
export function flowRequests(): readonly { path: string; key: string | null; body: Record<string, unknown> }[] {
  return requests
}

function view(id: string): FlowRunView | undefined {
  if (starting.has(id)) {
    return { id, status: 'starting', definition_id: 'def-swap', version: 1, name: 'swap', run: null, pending: [], live: false }
  }
  const found = runs.find((r) => r.id === id)
  if (!found) return undefined
  const pending: FlowPending[] = found.waiting_on.map(({ call_id, kind, fn, prompt, since }) => ({ call_id, kind, fn, prompt, since }))
  return {
    id,
    status: found.status,
    definition_id: found.definition_id,
    version: found.version,
    name: found.name,
    run: found,
    pending,
    live: !offline.has(id),
  }
}

/** The call leaves what the run waits on, and the run moves on. */
function resolve(id: string, callId: string): boolean {
  const found = runs.find((r) => r.id === id)
  if (!found?.waiting_on.some((w) => w.call_id === callId)) return false
  found.waiting_on = found.waiting_on.filter((w) => w.call_id !== callId)
  if (found.waiting_on.length === 0) found.status = 'running'
  return true
}

function respond(outcome: (body: Record<string, unknown>) => string) {
  return async ({ request, params }: { request: Request; params: Record<string, string | readonly string[] | undefined> }) => {
    const id = String(params['id'])
    const body = (await request.json()) as Record<string, unknown>
    requests.push({ path: new URL(request.url).pathname, key: request.headers.get('Idempotency-Key'), body })
    if (!resolve(id, String(body['call_id']))) {
      return HttpResponse.json(
        { type: STALE, title: 'Conflict', status: 409, detail: 'That question or approval is no longer waiting: the run moved on. Reload it.' },
        { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
      )
    }
    return HttpResponse.json({ run_id: id, call_id: body['call_id'], outcome: outcome(body) })
  }
}

export const handlers = [
  http.get('/api/v1/workflow-runs', ({ request }) => {
    const session = new URL(request.url).searchParams.get('session')
    return HttpResponse.json(session ? runs.filter((r) => r.started_by['session'] === session) : runs)
  }),
  http.get('/api/v1/workflow-runs/:id', ({ params }) => {
    const found = view(String(params['id']))
    return found
      ? HttpResponse.json(found)
      : HttpResponse.json({ title: 'Not Found', status: 404, detail: 'No such flow run.' }, { status: 404 })
  }),
  http.post('/api/v1/workflow-runs/:id/answer', respond(() => 'answered')),
  http.post('/api/v1/workflow-runs/:id/decide', respond((body) => (body['approved'] ? 'approved' : 'denied'))),
]
