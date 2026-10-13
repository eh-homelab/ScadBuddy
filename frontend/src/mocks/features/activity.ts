import { HttpResponse, http } from 'msw'
import type { Activity, ActivityFacets } from '../../api/types'

/**
 * The activity list, its facets and one row (backend/scadbuddy/api/activity.py), over an
 * in-memory list. Like the backend, an unknown query parameter is 400 and the list is
 * newest first.
 */

const LIST_PARAMS = new Set([
  'kind', 'status', 'principal', 'session', 'via_tool', 'subject', 'since', 'until',
  'parent', 'q', 'cursor', 'limit', 'advanced',
])
const ADVANCED_FIELDS = ['workflow_id', 'run_id', 'traceparent', 'source', 'source_id', 'facets'] as const
const ADVANCED_DETAIL = new Set(['request', 'result', 'error'])

function row(id: string, fields: Partial<Activity> & Pick<Activity, 'kind' | 'status' | 'title'>): Activity {
  return {
    id,
    principal: 'browser',
    session: null,
    via_tool: null,
    subject: '',
    parent_id: null,
    detail: {},
    created_at: '2026-10-09T10:00:00Z',
    updated_at: '2026-10-09T10:00:00Z',
    finished_at: null,
    ...fields,
  }
}

function defaultRows(): Activity[] {
  return [
    row('render:r1', {
      kind: 'render',
      status: 'running',
      title: 'Render box',
      subject: 'box',
      created_at: '2026-10-09T10:02:00Z',
      updated_at: '2026-10-09T10:02:00Z',
    }),
    row('operation:o1', {
      kind: 'operation.model_import',
      status: 'failed',
      title: 'Import a model',
      principal: 'token:t1',
      via_tool: 'import_model',
      detail: { error: 'boom', summary: 'could not fetch' },
      created_at: '2026-10-09T10:01:00Z',
      updated_at: '2026-10-09T10:01:30Z',
      finished_at: '2026-10-09T10:01:30Z',
    }),
    row('agent:a1', {
      kind: 'agent.plugin_package_install',
      status: 'succeeded',
      title: 'Install a plugin package',
      created_at: '2026-10-09T10:00:00Z',
      updated_at: '2026-10-09T10:00:20Z',
      finished_at: '2026-10-09T10:00:20Z',
    }),
  ]
}

function defaultFacets(): ActivityFacets {
  return {
    kinds: [
      { key: 'render', label: 'Render', facets: [{ name: 'slug', type: 'keyword', label: 'Template', filterable: true }] },
      { key: 'operation.model_import', label: 'Import a model', facets: [] },
      { key: 'agent.plugin_package_install', label: 'Install a plugin package', facets: [] },
      // A kind the client has no code for.
      { key: 'operation.font_install', label: 'Install a font', facets: [] },
    ],
    statuses: ['queued', 'running', 'succeeded', 'failed', 'cancelled'],
    principals: [
      { value: 'browser', count: 2 },
      { value: 'token:t1', count: 1 },
    ],
    tools: [{ value: 'import_model', count: 1 }],
    subjects: [{ value: 'box', count: 1 }],
  }
}

let rows = defaultRows()
let facets = defaultFacets()

export function seedActivity(next: Activity[]): void {
  rows = next
}

export function setActivityFacets(next: ActivityFacets): void {
  facets = next
}

export function reset(): void {
  rows = defaultRows()
  facets = defaultFacets()
}

function bad(detail: string) {
  return HttpResponse.json({ detail }, { status: 400 })
}

function shape(item: Activity, advanced: boolean): Activity {
  if (advanced) return item
  const out: Record<string, unknown> = { ...item }
  for (const key of ADVANCED_FIELDS) delete out[key]
  out.detail = Object.fromEntries(Object.entries(item.detail).filter(([k]) => !ADVANCED_DETAIL.has(k)))
  return out as unknown as Activity
}

export const handlers = [
  http.get('/api/v1/activity/facets', ({ request }) => {
    const url = new URL(request.url)
    for (const name of url.searchParams.keys()) if (name !== 'since') return bad(`unknown query parameter '${name}'`)
    return HttpResponse.json(facets)
  }),
  http.get('/api/v1/activity/:id', ({ request, params }) => {
    const url = new URL(request.url)
    for (const name of url.searchParams.keys()) if (name !== 'advanced') return bad(`unknown query parameter '${name}'`)
    const advanced = ['1', 'true'].includes(url.searchParams.get('advanced') ?? '')
    const found = rows.find((r) => r.id === params.id)
    if (!found) return HttpResponse.json({ detail: `no activity '${String(params.id)}'` }, { status: 404 })
    return HttpResponse.json({
      row: shape(found, advanced),
      children: rows.filter((r) => r.parent_id === found.id).map((r) => shape(r, advanced)),
      links: [],
      detail: shape(found, advanced).detail,
    })
  }),
  http.get('/api/v1/activity', ({ request }) => {
    const sp = new URL(request.url).searchParams
    for (const name of sp.keys()) {
      if (!LIST_PARAMS.has(name) && !name.startsWith('facet.')) return bad(`unknown query parameter '${name}'`)
    }
    const kinds = sp.getAll('kind')
    const statuses = sp.getAll('status')
    const q = (sp.get('q') ?? '').toLowerCase()
    const since = sp.get('since')
    const until = sp.get('until')
    const limit = Number(sp.get('limit') ?? 50)
    const offset = Number(sp.get('cursor') ?? 0)
    const filtered = rows
      .filter((r) => kinds.length === 0 || kinds.includes(r.kind))
      .filter((r) => statuses.length === 0 || statuses.includes(r.status))
      .filter((r) => !sp.has('principal') || r.principal === sp.get('principal'))
      .filter((r) => !sp.has('session') || r.session === sp.get('session'))
      .filter((r) => !sp.has('via_tool') || r.via_tool === sp.get('via_tool'))
      .filter((r) => !sp.has('subject') || r.subject === sp.get('subject'))
      .filter((r) => !sp.has('parent') || r.parent_id === sp.get('parent'))
      .filter((r) => !since || (r.created_at ?? '') >= since)
      .filter((r) => !until || (r.created_at ?? '') <= until)
      .filter((r) => !q || `${r.title} ${r.subject}`.toLowerCase().includes(q))
      .filter((r) => {
        for (const [key, value] of sp) {
          if (!key.startsWith('facet.')) continue
          if (String(r.facets?.[key.slice('facet.'.length)]) !== value) return false
        }
        return true
      })
      .sort((a, b) => (b.created_at ?? '').localeCompare(a.created_at ?? ''))
    const page = filtered.slice(offset, offset + limit)
    const advanced = ['1', 'true'].includes(sp.get('advanced') ?? '')
    return HttpResponse.json({
      items: page.map((r) => shape(r, advanced)),
      next_cursor: offset + limit < filtered.length ? String(offset + limit) : null,
    })
  }),
]
