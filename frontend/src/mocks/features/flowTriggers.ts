import { HttpResponse, http } from 'msw'
import type { FlowGrantSpec, FlowStandingGrant, FlowTrigger, FlowTriggerCreate } from '../../api/types'

/**
 * #2484 — flow triggers (backend `api/flow_triggers.py`). `swap` has two: an hourly
 * schedule made in the browser, with an approved standing grant and two fires, and an
 * assistant's event trigger whose standing grant it only requested. Each trigger's
 * `seen` changes with every change to it or its grant; an approval or a browser attach
 * that sends another is the backend's 409 `flow-trigger-changed`. Every write is kept,
 * with its body, for tests (`flowTriggerRequests`).
 */

const at = (minutes: number) => new Date(Date.UTC(2026, 9, 10, 9, minutes)).toISOString()
const CHANGED = 'https://scadbuddy.dev/problems/flow-trigger-changed'
/** The standing grants' spec, made at each reset: its expiry is from now, never a fixed
 *  date, since the grant editor refuses one in the past and a fixed one passed on
 *  2026-10-20 (#4447); made per reset, so a long-open `pnpm dev` tab keeps one too.
 *  Three days is inside both grant caps. */
function grantSpec(): FlowGrantSpec {
  return {
    expires_at: new Date(Date.now() + 3 * 24 * 60 * 60 * 1000).toISOString(),
    rules: [{ fn: 'queue_print', printers: [1], max_prints: 2 }],
    sessions: false,
    ask_starter_session: false,
  }
}

function standing(id: string, fields: Partial<FlowStandingGrant>): FlowStandingGrant {
  const spec = grantSpec()
  return {
    id,
    status: 'approved',
    spec,
    requested_by: { kind: 'browser' },
    approved_by: 'browser',
    approved_at: at(0),
    expires_at: spec.expires_at,
    created_at: at(0),
    in_force: true,
    ...fields,
  }
}

function defaults(): FlowTrigger[] {
  return [
    {
      id: 'trig-hourly',
      name: 'swap',
      definition_id: 'def-swap',
      version: 1,
      kind: 'schedule',
      schedule: { interval_s: 3600, offset_s: 0, time_zone: 'UTC', catchup_window_s: 300 },
      event: null,
      overlap: 'skip',
      inputs: null,
      paused: false,
      created_by: { kind: 'browser' },
      created_at: at(0),
      grant: standing('sg-1', {}),
      next_fires: [at(120), at(180)],
      fires: [
        {
          trigger_id: 'trig-hourly',
          fire_key: 'k2',
          run_id: 'run-done',
          outcome: 'started',
          fired_at: at(60),
          run_status: 'succeeded',
        },
        {
          trigger_id: 'trig-hourly',
          fire_key: 'k1',
          run_id: 'run-skipped',
          outcome: 'skipped',
          reason: 'overlap',
          fired_at: at(0),
        },
      ],
      seen: 'seen-trig-hourly-0',
    },
    {
      id: 'trig-render',
      name: 'swap',
      definition_id: 'def-swap',
      version: 1,
      kind: 'event',
      schedule: null,
      event: { kind: 'render.finished', slug: 'coaster' },
      overlap: 'allow_all',
      inputs: null,
      paused: false,
      created_by: { kind: 'agent', principal: 'p1', session: 's1' },
      created_at: at(0),
      grant: standing('sg-2', {
        status: 'requested',
        requested_by: { kind: 'agent', principal: 'p1' },
        approved_by: null,
        approved_at: null,
        expires_at: null,
        in_force: false,
      }),
      next_fires: [],
      fires: [],
      seen: 'seen-trig-render-0',
    },
  ]
}

let triggers = defaults()
let requests: { path: string; body: Record<string, unknown> | null }[] = []
let changes = 0

export function reset(): void {
  triggers = defaults()
  requests = []
  changes = 0
}

export function flowTriggerRequests(): readonly { path: string; body: Record<string, unknown> | null }[] {
  return requests
}

/** Change a trigger behind the page's back, as an assistant's edit would. */
export function changeFlowTrigger(id: string, fields: Partial<FlowTrigger>): void {
  triggers = triggers.map((t) => (t.id === id ? touched({ ...t, ...fields }) : t))
}

function touched(trigger: FlowTrigger): FlowTrigger {
  changes += 1
  return { ...trigger, seen: `seen-${trigger.id}-${changes}` }
}

function find(id: unknown) {
  return triggers.find((t) => t.id === String(id))
}

const missing = () => HttpResponse.json({ title: 'Not Found', status: 404, detail: 'No such trigger.' }, { status: 404 })
const changed = () =>
  HttpResponse.json(
    { type: CHANGED, title: 'Conflict', status: 409, detail: 'The trigger or its grant changed since it was shown.' },
    { status: 409 },
  )

async function body(request: Request): Promise<Record<string, unknown> | null> {
  const text = await request.text()
  return text ? (JSON.parse(text) as Record<string, unknown>) : null
}

function update(id: string, change: (t: FlowTrigger) => FlowTrigger): FlowTrigger {
  let out: FlowTrigger | undefined
  triggers = triggers.map((t) => (t.id === id ? (out = touched(change(t))) : t))
  return out!
}

export const handlers = [
  http.get('/api/v1/flow-triggers', ({ request }) => {
    const name = new URL(request.url).searchParams.get('name')
    return HttpResponse.json(name ? triggers.filter((t) => t.name === name) : triggers)
  }),
  http.get('/api/v1/flow-triggers/:id', ({ params }) => {
    const found = find(params['id'])
    return found ? HttpResponse.json(found) : missing()
  }),
  http.post('/api/v1/flow-triggers', async ({ request }) => {
    const sent = (await body(request)) as FlowTriggerCreate
    requests.push({ path: '/flow-triggers', body: sent as unknown as Record<string, unknown> })
    const created = touched({
      id: `trig-${triggers.length + 1}`,
      name: 'swap',
      definition_id: sent.definition_id,
      version: 1,
      kind: sent.schedule ? 'schedule' : 'event',
      schedule: sent.schedule ?? null,
      event: sent.event ?? null,
      overlap: sent.overlap,
      inputs: sent.inputs ?? null,
      paused: false,
      created_by: { kind: 'browser' },
      created_at: at(0),
      grant: null,
      next_fires: [],
      fires: [],
      seen: '',
    })
    triggers = [...triggers, created]
    return HttpResponse.json(created, { status: 201 })
  }),
  http.post('/api/v1/flow-triggers/:id/:action', async ({ request, params }) => {
    const id = String(params['id'])
    const action = String(params['action'])
    const trigger = find(id)
    if (!trigger) return missing()
    const sent = await body(request)
    requests.push({ path: `/flow-triggers/${id}/${action}`, body: sent })
    if (action === 'pause' || action === 'resume') {
      return HttpResponse.json(update(id, (t) => ({ ...t, paused: action === 'pause' })))
    }
    if (action === 'grant') {
      if (sent?.['seen'] !== trigger.seen) return changed()
      const grant = standing(`sg-${id}`, { spec: sent['spec'] as FlowGrantSpec })
      return HttpResponse.json(update(id, (t) => ({ ...t, grant })).grant)
    }
    return HttpResponse.json({ detail: 'unknown action' }, { status: 404 })
  }),
  http.post('/api/v1/flow-triggers/:id/grant/:decision', async ({ request, params }) => {
    const id = String(params['id'])
    const decision = String(params['decision'])
    const trigger = find(id)
    if (!trigger?.grant) return missing()
    const sent = await body(request)
    requests.push({ path: `/flow-triggers/${id}/grant/${decision}`, body: sent })
    const grant = trigger.grant
    let next: FlowStandingGrant
    if (decision === 'approve') {
      if (sent?.['seen'] !== trigger.seen) return changed()
      const spec = (sent['spec'] as FlowGrantSpec | undefined) ?? grant.spec
      next = { ...grant, status: 'approved', spec, approved_by: 'browser', approved_at: at(1), expires_at: spec.expires_at, in_force: true }
    } else if (decision === 'decline') {
      next = { ...grant, status: 'declined' }
    } else {
      next = { ...grant, revoked_at: at(2), revoked_by: 'browser', in_force: false }
    }
    return HttpResponse.json(update(id, (t) => ({ ...t, grant: next })).grant)
  }),
  http.delete('/api/v1/flow-triggers/:id', ({ params }) => {
    const id = String(params['id'])
    if (!find(id)) return missing()
    requests.push({ path: `/flow-triggers/${id}`, body: null })
    triggers = triggers.filter((t) => t.id !== id)
    return new HttpResponse(null, { status: 204 })
  }),
]
