import { HttpResponse, http } from 'msw'
import type {
  FlowDefinition,
  FlowPending,
  FlowResetPoints,
  FlowResetPreview,
  FlowRun,
  FlowRunView,
  FlowStep,
} from '../../api/types'

/**
 * #1057 — flow runs (backend `api/flows.py`): the list, one run's view, and a person's
 * answer and decision. An answer or decision to a call no longer waiting is the
 * backend's 409 `stale-entry`. Every answer, decision, start, reset and delete is kept,
 * with its `Idempotency-Key`, for tests (`flowRequests`).
 *
 * #2350 — the registered flows (`GET /workflows`, newest of each name; `GET /workflows/{id}`;
 * #2503's `GET /workflows/names/{name}/versions`), a run started
 * from one, a run forgotten, and its reset points, preview and Reset. A step's reset
 * point is its `history_length + 1`, which runs it and every later step again; one more
 * after the last ended step runs none again.
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
    ended_history_length: ended ? 10 * seq + 5 : null,
  }
}

const SWAP_SCRIPT = `async def main():
    # Render the box, then ask before swapping the colour.
    await render("box", {"width": 20})
    answer = await wait_for_human("Swap to pink?", 600)
    return answer["answer"]
`

function definition(id: string, name: string, version: number, script: string, minutes: number): FlowDefinition {
  return {
    id,
    name,
    version,
    approval_timeout_s: null,
    created_by: { kind: 'agent', principal: 'p', session: 'sess-1' },
    created_at: at(minutes),
    script,
  }
}

function defaultDefinitions(): FlowDefinition[] {
  return [
    definition('def-swap', 'swap', 1, SWAP_SCRIPT, 0),
    definition('def-plates-1', 'plates', 1, 'async def main():\n    return 1\n', 0),
    {
      ...definition('def-plates-2', 'plates', 2, 'async def main():\n    await queue_print({"output_id": "o1"}, PLAN)\n', 1),
      created_by: { kind: 'browser' },
    },
  ]
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
      definition_id: 'def-plates-2',
      name: 'plates',
      version: 2,
      status: 'waiting',
      steps: [step(1, 'queue_print', 'running', false)],
      waiting_on: [{ call_id: 'call-1', kind: 'approval', fn: 'queue_print', since: at(1) }],
      created_at: at(2),
    }),
    run('run-done', {
      status: 'succeeded',
      steps: [step(1, 'render', 'succeeded'), step(2, 'queue_print', 'succeeded'), step(3, 'sleep', 'succeeded')],
      result: 'result: 7',
      created_at: at(1),
    }),
  ]
}

let runs = defaults()
let definitions = defaultDefinitions()
let started = 0
/** Runs whose next Reset is refused: outward calls landed since the preview. */
const moved = new Set<string>()
/** Runs whose view says the workflow did not answer (`live: false`). */
const offline = new Set<string>()
/** Runs that exist only as an accepted start (`starting`, no row yet). */
const starting = new Set<string>()
let requests: { path: string; key: string | null; body: Record<string, unknown> }[] = []

export function reset(): void {
  runs = defaults()
  definitions = defaultDefinitions()
  started = 0
  moved.clear()
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

/** Tests: the registered flow versions. */
export function setFlowDefinitions(next: FlowDefinition[]): void {
  definitions = next
}

/** Tests: the run's next Reset finds outward calls added since its preview (409). */
export function moveFlowRunPastPreview(id: string): void {
  moved.add(id)
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

const newestFirst = (name: string) =>
  definitions.filter((d) => d.name === name).sort((a, b) => b.version - a.version)

const AS_OF = 100

function points(found: FlowRun): FlowResetPoints {
  const ended = found.steps.filter((s) => s.ended_history_length != null)
  const last = ended[ended.length - 1]
  return {
    workflow_run_id: found.workflow_run_id,
    as_of_event_id: AS_OF,
    points: [
      ...found.steps.map((s) => ({ event_id: s.history_length + 1, first_step: s.seq })),
      ...(last ? [{ event_id: (last.ended_history_length ?? 0) + 2, first_step: null }] : []),
    ],
  }
}

function preview(found: FlowRun, eventId: number): FlowResetPreview {
  const point = points(found).points.find((p) => p.event_id === eventId)
  const first = point?.first_step ?? Infinity
  return {
    event_id: eventId,
    workflow_run_id: found.workflow_run_id,
    as_of_event_id: moved.has(found.id) ? AS_OF + 10 : AS_OF,
    valid: point !== undefined,
    calls: found.steps
      .filter((s) => s.outward && s.seq >= first)
      .map((s) => ({ fn: s.fn, call_id: s.call_id, scheduled_event_id: s.history_length + 2 })),
  }
}

const notFound = (detail: string) => HttpResponse.json({ title: 'Not Found', status: 404, detail }, { status: 404 })

async function keep(request: Request): Promise<Record<string, unknown>> {
  const text = request.method === 'DELETE' ? '' : await request.text()
  const body = text ? (JSON.parse(text) as Record<string, unknown>) : {}
  requests.push({ path: new URL(request.url).pathname, key: request.headers.get('Idempotency-Key'), body })
  return body
}

export const handlers = [
  // As `FlowStore.list_definitions`: each name's newest version only, by name.
  http.get('/api/v1/workflows', () =>
    HttpResponse.json(
      [...new Set(definitions.map((d) => d.name))].sort().map((name) => {
        const { script: _script, ...summary } = newestFirst(name)[0]!
        return summary
      }),
    ),
  ),
  http.get('/api/v1/workflows/names/:name/versions', ({ params }) => {
    const versions = newestFirst(String(params['name']))
    if (versions.length === 0) return notFound('No such flow.')
    return HttpResponse.json(
      versions.map((d, i) => ({
        id: d.id,
        commit: `c0ffee${d.version}`.padEnd(40, '0'),
        short: `c0ffee${d.version}`,
        version: d.version,
        message: `flow ${d.name} v${d.version}`,
        created_by: d.created_by,
        created_at: d.created_at,
        current: i === 0,
      })),
    )
  }),
  http.get('/api/v1/workflows/:id', ({ params }) => {
    const found = definitions.find((d) => d.id === params['id'])
    return found ? HttpResponse.json(found) : notFound('No such flow.')
  }),
  http.post('/api/v1/workflows/:id/runs', async ({ request, params }) => {
    await keep(request)
    const found = definitions.find((d) => d.id === params['id'])
    if (!found) return notFound('No such flow.')
    started += 1
    const id = `run-new-${started}`
    runs = [
      run(id, { definition_id: found.id, name: found.name, version: found.version, status: 'running', created_at: at(30) }),
      ...runs,
    ]
    return HttpResponse.json({ id, status: 'starting', repeated: false }, { status: 202 })
  }),
  http.delete('/api/v1/workflow-runs/:id', async ({ request, params }) => {
    await keep(request)
    const before = runs.length
    runs = runs.filter((r) => r.id !== params['id'])
    return runs.length < before ? new HttpResponse(null, { status: 204 }) : notFound('No such flow run.')
  }),
  http.get('/api/v1/workflow-runs/:id/reset-points', ({ params }) => {
    const found = runs.find((r) => r.id === params['id'])
    return found ? HttpResponse.json(points(found)) : notFound('No such flow run.')
  }),
  http.get('/api/v1/workflow-runs/:id/reset-preview', ({ request, params }) => {
    const found = runs.find((r) => r.id === params['id'])
    const eventId = Number(new URL(request.url).searchParams.get('event_id'))
    return found ? HttpResponse.json(preview(found, eventId)) : notFound('No such flow run.')
  }),
  http.post('/api/v1/workflow-runs/:id/reset', async ({ request, params }) => {
    const body = await keep(request)
    const found = runs.find((r) => r.id === params['id'])
    if (!found) return notFound('No such flow run.')
    const eventId = Number(body['event_id'])
    if (moved.has(found.id)) {
      const next = preview(found, eventId)
      moved.delete(found.id)
      return HttpResponse.json(
        {
          type: 'https://scadbuddy.dev/problems/flow-reset-changed',
          title: 'Conflict',
          status: 409,
          detail: 'The run made more outward calls since that preview; check the new one.',
          preview: next,
        },
        { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
      )
    }
    const first = points(found).points.find((p) => p.event_id === eventId)?.first_step ?? Infinity
    found.steps = found.steps.filter((s) => s.seq < first)
    found.status = 'running'
    found.result = null
    found.workflow_run_id = `${found.workflow_run_id}-reset`
    return HttpResponse.json({ run_id: found.id, workflow_run_id: found.workflow_run_id, event_id: eventId })
  }),
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
