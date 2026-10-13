import { HttpResponse, http } from 'msw'
import type {
  FlowDefinition,
  FlowGrantSpec,
  FlowGrantView,
  FlowHostInterface,
  FlowScriptProblem,
  FlowPending,
  FlowResetPoints,
  FlowResetPreview,
  FlowRun,
  FlowRunView,
  FlowState,
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
 *
 * #2481 — a run's grant: in the run's view, started with one from the browser (approved
 * as sent), and a person's approve (as requested, or the edited spec), decline and
 * revoke; a second decision is the backend's 409 `flow-grant-state`. `run-ask` (an
 * agent's start) carries a requested grant and `run-done` an approved one with uses.
 *
 * #2487 — the editor: Save (`POST /workflows`, a new version), the check that stores
 * nothing, the host functions it completes, a version's source by commit, and Restore.
 * The check here is a stand-in for the server's: a call to a name that is not a host
 * function, or `sleep` given a string, is a problem on its line.
 *
 * #2485 — a flow archived (`POST /workflows/names/{name}/archive`, `unarchive`) or deleted
 * (`DELETE /workflows/names/{name}`): hidden from `GET /workflows` unless `?archived=true`,
 * and its starts refused (409 `flow-archived`, `flow-deleted`). A new version brings a
 * deleted flow back; its versions and their scripts stay readable throughout.
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
    commit: `c0ffee${version}`.padEnd(40, '0'),
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

const HOUR = 3_600_000
const GRANT_STATE = 'https://scadbuddy.dev/problems/flow-grant-state'

function grant(id: string, fields: Partial<FlowGrantView>): FlowGrantView {
  return {
    id,
    status: 'approved',
    spec: { expires_at: new Date(Date.now() + 24 * HOUR).toISOString(), sessions: false, ask_starter_session: true, rules: [] },
    used: {},
    requested_by: { kind: 'browser' },
    approved_by: 'browser',
    approved_at: at(0),
    revoked_by: null,
    revoked_at: null,
    expires_at: null,
    created_at: at(0),
    in_force: true,
    uses: [],
    ...fields,
  }
}

function defaultGrants(): Map<string, FlowGrantView> {
  const expires = new Date(Date.now() + 24 * HOUR).toISOString()
  return new Map([
    [
      'run-ask',
      grant('grant-ask', {
        status: 'requested',
        spec: {
          expires_at: expires,
          sessions: false,
          ask_starter_session: true,
          rules: [{ fn: 'queue_print', printers: [1], models: ['name-keychain'], max_prints: 12, max_calls: 12 }],
        },
        requested_by: { kind: 'agent', principal: 'p', session: 'sess-1' },
        approved_by: null,
        approved_at: null,
        in_force: false,
      }),
    ],
    [
      'run-done',
      grant('grant-done', {
        spec: {
          expires_at: expires,
          sessions: true,
          ask_starter_session: true,
          rules: [
            { fn: 'queue_print', printers: [1, 2], max_prints: 4, max_filament_g: 200 },
            { fn: 'tool', tool: 'send_to_bambuddy', max_calls: 2 },
          ],
        },
        expires_at: expires,
        used: { '0': { prints: 1, filament_g: 12.5, calls: 1 } },
        uses: [
          {
            request_id: 'req-1',
            fn: 'queue_print',
            tool: null,
            rule_index: 0,
            prints: 1,
            filament_g: 12.5,
            session_id: null,
            created_at: at(2),
          },
        ],
      }),
    ],
  ])
}

let runs = defaults()
let grants = defaultGrants()
let definitions = defaultDefinitions()
let started = 0
/** #2485: each archived or deleted flow's state, by name. */
const states = new Map<string, FlowState>()
/** Runs whose next Reset is refused: outward calls landed since the preview. */
const moved = new Set<string>()
/** Runs whose view says the workflow did not answer (`live: false`). */
const offline = new Set<string>()
/** Runs that exist only as an accepted start (`starting`, no row yet). */
const starting = new Set<string>()
let requests: { path: string; key: string | null; body: Record<string, unknown> }[] = []
/** #2477: each run's images, as the view lists them. */
const images = new Map<string, FlowRunView['images']>()
/** One grey pixel, what every image route answers. */
const PIXEL = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVR4nGO4AAAA0gDR29kOtwAAAABJRU5ErkJggg=='), (c) => c.charCodeAt(0))

export function reset(): void {
  runs = defaults()
  grants = defaultGrants()
  definitions = defaultDefinitions()
  started = 0
  moved.clear()
  offline.clear()
  starting.clear()
  images.clear()
  states.clear()
  requests = []
}

/** Tests: a flow archived or deleted, as `GET /workflows?archived=true` lists it. */
export function setFlowState(state: Pick<FlowState, 'name'> & Partial<FlowState>): void {
  states.set(state.name, { ...stateOf(state.name), ...state })
}

/** A version's archive and delete fields, from its flow's state. */
function flags(name: string): Pick<FlowState, 'archived_at' | 'archived_by' | 'deleted_at' | 'deleted_by'> {
  const state = states.get(name)
  return {
    archived_at: state?.archived_at ?? null,
    archived_by: state?.archived_by ?? null,
    deleted_at: state?.deleted_at ?? null,
    deleted_by: state?.deleted_by ?? null,
  }
}

const hidden = (name: string) => Boolean(states.get(name)?.archived_at || states.get(name)?.deleted_at)

function stateOf(name: string): FlowState {
  return states.get(name) ?? { name, archived_at: null, archived_by: null, deleted_at: null, deleted_by: null, deleted_commit: null }
}

function conflict(type: string, detail: string) {
  return HttpResponse.json(
    { type: `https://scadbuddy.dev/problems/${type}`, title: 'Conflict', status: 409, detail },
    { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

/** Tests: the images a run's tool calls returned. */
export function setFlowRunImages(id: string, next: FlowRunView['images']): void {
  images.set(id, next)
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

/** Tests: a run's grant, as its view shows it; null for none. */
export function setFlowRunGrant(id: string, next: FlowGrantView | null): void {
  if (next) grants.set(id, next)
  else grants.delete(id)
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
    return { id, status: 'starting', definition_id: 'def-swap', version: 1, name: 'swap', run: null, pending: [], live: false, images: [] }
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
    images: images.get(id) ?? [],
    grant: grants.get(id) ?? null,
  }
}

/** A grant route: 404 without a grant, 409 once it is decided (or revoked), else `change`. */
function decideGrant(allowed: (found: FlowGrantView) => boolean, change: (found: FlowGrantView, body: Record<string, unknown>) => void) {
  return async ({ request, params }: { request: Request; params: Record<string, string | readonly string[] | undefined> }) => {
    const body = await keep(request)
    const found = grants.get(String(params['id']))
    if (!found) return notFound('This run has no grant.')
    if (!allowed(found) || found.revoked_at) {
      const state = found.revoked_at ? 'revoked' : found.status
      return HttpResponse.json(
        {
          type: GRANT_STATE,
          title: 'Conflict',
          status: 409,
          detail: `The run's grant is ${state}: a grant is decided once, and revoking it is final.`,
        },
        { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
      )
    }
    change(found, body)
    return HttpResponse.json(found)
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

/** A version's commit, as the history route lists it. */
const commitOf = (d: FlowDefinition) => `c0ffee${d.version}`.padEnd(40, '0')

export const FLOW_HOST_FUNCTIONS: FlowHostInterface = {
  functions: [
    {
      name: 'sleep',
      signature: 'async def sleep(seconds: float) -> None',
      parameters: ['seconds'],
      doc: 'Wait `seconds` seconds, durably (a Temporal timer).',
    },
    {
      name: 'wait_for_human',
      signature: 'async def wait_for_human(question: str, timeout_s: int) -> HumanAnswer',
      parameters: ['question', 'timeout_s'],
      doc: 'Ask a person `question` and wait up to `timeout_s` seconds (10 to 86400) for the answer.',
    },
    {
      name: 'render',
      signature: 'async def render(slug: str, params: dict[str, Any]) -> RenderResult',
      parameters: ['slug', 'params'],
      doc: 'Render template `slug` with `params` and wait until it settles.',
    },
    {
      name: 'queue_print',
      signature: 'async def queue_print(source: dict[str, Any], request: dict[str, Any]) -> PrintOutcome',
      parameters: ['source', 'request'],
      doc: 'Once a person approves it, slice and queue `source` with `request`.',
    },
    {
      name: 'tool',
      signature: 'async def tool(name: str, args: dict[str, Any]) -> str',
      parameters: ['name', 'args'],
      doc: "Call ScadBuddy's agent tool `name` with `args`; returns its text.",
    },
  ],
  types: [
    { name: 'HumanAnswer', source: 'class HumanAnswer(TypedDict):\n    answer: str' },
    { name: 'RenderResult', source: 'class RenderResult(TypedDict):\n    job_id: str\n    status: str' },
  ],
  tools: [
    { name: 'list_models', tier: 'read' },
    { name: 'send_to_bambuddy', tier: 'outward' },
  ],
}

const CALLABLE = new Set([...FLOW_HOST_FUNCTIONS.functions.map((f) => f.name), 'main', 'print', 'len', 'range'])

/** The stand-in check: each line's calls to names nothing defines, and `sleep('…')`. */
export function checkFlowScript(script: string): FlowScriptProblem[] {
  if (new TextEncoder().encode(script).length > 65_536) return [{ line: null, message: 'script is over 64 KiB' }]
  const problems: FlowScriptProblem[] = []
  script.split('\n').forEach((text, i) => {
    if (/^\s*(async\s+)?def\s/.test(text)) return
    for (const [, called] of text.matchAll(/(?<![\w.])([a-z_]\w*)\(/g)) {
      if (called && !CALLABLE.has(called)) {
        problems.push({ line: i + 1, message: `error[unresolved-reference]: Name \`${called}\` used when not defined` })
      }
    }
    if (/\bsleep\(\s*["']/.test(text)) {
      problems.push({ line: i + 1, message: 'error[invalid-argument-type]: Argument to function `sleep` is incorrect' })
    }
  })
  return problems
}

function scriptProblems(problems: FlowScriptProblem[], detail: string) {
  return HttpResponse.json(
    { type: 'https://scadbuddy.dev/problems/flow-script', title: 'Unprocessable Content', status: 422, detail, problems },
    { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

/** A new version of `name`, numbered after its last, as the git history numbers it. */
function addVersion(name: string, script: string, approval: number | null): FlowDefinition {
  const version = (newestFirst(name)[0]?.version ?? 0) + 1
  const created: FlowDefinition = {
    ...definition(`def-${name}-${version}`, name, version, script, 40 + version),
    approval_timeout_s: approval,
    created_by: { kind: 'browser' },
  }
  definitions = [...definitions, created]
  // A new version brings a deleted flow back.
  const state = states.get(name)
  if (state) states.set(name, { ...state, deleted_at: null, deleted_by: null, deleted_commit: null })
  return { ...created, ...flags(name) }
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
  // As `FlowStates.newest`: each name's newest version only, by name; archived and
  // deleted flows only with `?archived=true`.
  http.get('/api/v1/workflows', ({ request }) => {
    const all = new URL(request.url).searchParams.get('archived') === 'true'
    return HttpResponse.json(
      [...new Set(definitions.map((d) => d.name))]
        .filter((name) => all || !hidden(name))
        .sort()
        .map((name) => {
          const { script: _script, ...summary } = newestFirst(name)[0]!
          return { ...summary, ...flags(name) }
        }),
    )
  }),
  http.post('/api/v1/workflows/names/:name/archive', async ({ request, params }) => {
    await keep(request)
    const name = String(params['name'])
    if (newestFirst(name).length === 0) return notFound('No such flow.')
    const state = stateOf(name)
    if (!state.archived_at) states.set(name, { ...state, archived_at: at(50), archived_by: { kind: 'browser' } })
    return HttpResponse.json(stateOf(name))
  }),
  http.post('/api/v1/workflows/names/:name/unarchive', async ({ request, params }) => {
    await keep(request)
    const name = String(params['name'])
    if (newestFirst(name).length === 0) return notFound('No such flow.')
    states.set(name, { ...stateOf(name), archived_at: null, archived_by: null })
    return HttpResponse.json(stateOf(name))
  }),
  http.delete('/api/v1/workflows/names/:name', async ({ request, params }) => {
    await keep(request)
    const name = String(params['name'])
    if (newestFirst(name).length === 0) return notFound('No such flow.')
    const state = stateOf(name)
    if (!state.deleted_at) {
      states.set(name, { ...state, deleted_at: at(51), deleted_by: { kind: 'browser' }, deleted_commit: 'de1e7e'.padEnd(40, '0') })
    }
    return HttpResponse.json(stateOf(name))
  }),
  http.get('/api/v1/workflows/names/:name/versions', ({ params }) => {
    const versions = newestFirst(String(params['name']))
    if (versions.length === 0) return notFound('No such flow.')
    return HttpResponse.json(
      versions.map((d, i) => ({
        id: d.id,
        commit: commitOf(d),
        short: `c0ffee${d.version}`,
        version: d.version,
        message: `flow ${d.name} v${d.version}`,
        created_by: d.created_by,
        created_at: d.created_at,
        current: i === 0,
      })),
    )
  }),
  http.post('/api/v1/workflows', async ({ request }) => {
    const body = (await keep(request)) as { name: string; script: string; approval_timeout?: number | 'never' | null }
    const problems = checkFlowScript(body.script)
    if (problems.length > 0) return scriptProblems(problems, 'The script does not type-check.')
    const timeout = body.approval_timeout === 'never' ? 0 : (body.approval_timeout ?? null)
    return HttpResponse.json(addVersion(body.name, body.script, timeout), { status: 201 })
  }),
  http.post('/api/v1/workflow-script/check', async ({ request }) => {
    const { script } = (await request.json()) as { script: string }
    return HttpResponse.json({ problems: checkFlowScript(script) })
  }),
  http.get('/api/v1/workflow-script/host-functions', () => HttpResponse.json(FLOW_HOST_FUNCTIONS)),
  http.get('/api/v1/workflows/names/:name/versions/:commit/source', ({ params }) => {
    const found = newestFirst(String(params['name'])).find((d) => commitOf(d) === params['commit'])
    return found
      ? new HttpResponse(found.script, { headers: { 'Content-Type': 'text/plain; charset=utf-8' } })
      : notFound('No such version of this flow.')
  }),
  http.post('/api/v1/workflows/names/:name/versions/:commit/restore', async ({ request, params }) => {
    await keep(request)
    const found = newestFirst(String(params['name'])).find((d) => commitOf(d) === params['commit'])
    if (!found) return notFound('No such version of this flow.')
    const problems = checkFlowScript(found.script)
    if (problems.length > 0) return scriptProblems(problems, 'That version no longer type-checks.')
    return HttpResponse.json(addVersion(found.name, found.script, found.approval_timeout_s ?? null), { status: 201 })
  }),
  http.get('/api/v1/workflows/:id', ({ params }) => {
    const found = definitions.find((d) => d.id === params['id'])
    return found ? HttpResponse.json({ ...found, ...flags(found.name) }) : notFound('No such flow.')
  }),
  http.post('/api/v1/workflows/:id/runs', async ({ request, params }) => {
    const body = await keep(request)
    const found = definitions.find((d) => d.id === params['id'])
    if (!found) return notFound('No such flow.')
    if (states.get(found.name)?.deleted_at) {
      return conflict('flow-deleted', 'This flow was deleted. Restore one of its versions to run it.')
    }
    if (states.get(found.name)?.archived_at) return conflict('flow-archived', 'This flow is archived. Unarchive it to run it.')
    started += 1
    const id = `run-new-${started}`
    const sent = (body['grant'] as FlowGrantSpec | undefined) ?? null
    if (sent) grants.set(id, grant(`grant-new-${started}`, { spec: sent, expires_at: sent.expires_at, approved_at: at(30), created_at: at(30) }))
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
  http.get('/api/v1/workflow-runs/:id/images/:name', ({ params }) =>
    (images.get(String(params['id'])) ?? []).some((i) => i.name === params['name'])
      ? new HttpResponse(PIXEL, { headers: { 'Content-Type': 'image/png' } })
      : HttpResponse.json({ title: 'Not Found', status: 404, detail: 'No such image.' }, { status: 404 }),
  ),
  http.post(
    '/api/v1/workflow-runs/:id/grant/approve',
    decideGrant(
      (found) => found.status === 'requested',
      (found, body) => {
        const spec = (body['spec'] as FlowGrantSpec | undefined) ?? found.spec
        Object.assign(found, {
          status: 'approved',
          spec,
          approved_by: 'browser',
          approved_at: new Date().toISOString(),
          expires_at: spec.expires_at,
          in_force: true,
        })
      },
    ),
  ),
  http.post(
    '/api/v1/workflow-runs/:id/grant/decline',
    decideGrant(
      (found) => found.status === 'requested',
      (found) => Object.assign(found, { status: 'declined' }),
    ),
  ),
  http.post(
    '/api/v1/workflow-runs/:id/grant/revoke',
    decideGrant(
      (found) => found.status !== 'declined',
      (found) => Object.assign(found, { revoked_by: 'browser', revoked_at: new Date().toISOString(), in_force: false }),
    ),
  ),
  http.post('/api/v1/workflow-runs/:id/answer', respond(() => 'answered')),
  http.post('/api/v1/workflow-runs/:id/decide', respond((body) => (body['approved'] ? 'approved' : 'denied'))),
]
