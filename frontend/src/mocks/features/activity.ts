import { HttpResponse, http } from 'msw'
import type { Activity, ActivityFacets } from '../../api/types'
import { CURSOR_HEADER } from '../../lib/readCursor'

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

/**
 * backend/scadbuddy/activity/index.py's ``subject_key`` (and its SQL twin ``_SUBJECT_KEY``):
 * one subject's key under either spelling. Change it with those two.
 */
function subjectKey(subject: string): string {
  const library = /^library-(\d+)$/.exec(subject)
  if (library) return `library:${library[1]}`
  return /^[0-9a-f]{32}$/.test(subject) ? `output:${subject}` : subject
}

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
      facets: { slug: 'box' },
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
      workflow_id: 'op-model_import-1',
      run_id: 'run-1',
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
    subjects: [{ value: 'box', count: 1, label: 'Box' }],
  }
}

let rows = defaultRows()
let facets = defaultFacets()
/** #1039 — the event the list's answers say the server had heard; none by default. */
let listCursor: string | undefined

export function seedActivity(next: Activity[]): void {
  rows = next
}

export function setActivityFacets(next: ActivityFacets): void {
  facets = next
}

export function setActivityListCursor(next: string | undefined): void {
  listCursor = next
}

export function reset(): void {
  rows = defaultRows()
  facets = defaultFacets()
  listCursor = undefined
}

const PROBLEM = 'https://scadbuddy.dev/problems/'
const TITLES: Record<number, string> = { 400: 'Bad Request', 404: 'Not Found', 422: 'Unprocessable Content' }

/** An RFC 9457 problem, as the backend answers every error. */
function problem(request: Request, status: number, detail: string, type = 'about:blank') {
  return HttpResponse.json(
    { type, title: TITLES[status] ?? 'Error', status, detail, instance: new URL(request.url).pathname },
    { status, headers: { 'Content-Type': 'application/problem+json' } },
  )
}

const unknownParameter = (request: Request, name: string) =>
  problem(request, 400, `unknown query parameter '${name}'`, `${PROBLEM}unknown-parameter`)

function encodeCursor(item: Activity): string {
  return btoa(`${item.created_at ?? ''}|${item.id}`).replaceAll('+', '-').replaceAll('/', '_')
}

/** The (created_at, id) a cursor names, or null for anything `encodeCursor` did not write. */
function decodeCursor(cursor: string): [string, string] | null {
  try {
    const raw = atob(cursor.replaceAll('-', '+').replaceAll('_', '/'))
    const [created, sep, id] = [raw.slice(0, raw.indexOf('|')), raw.includes('|'), raw.slice(raw.indexOf('|') + 1)]
    return sep && id && Number.isFinite(Date.parse(created)) && /(Z|[+-]\d\d:\d\d)$/.test(created) ? [created, id] : null
  } catch {
    return null
  }
}

const STATUSES = new Set(['queued', 'running', 'succeeded', 'failed', 'cancelled'])
const validTime = (value: string) => Number.isFinite(Date.parse(value))

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
    for (const name of url.searchParams.keys()) if (name !== 'since') return unknownParameter(request, name)
    return HttpResponse.json(facets)
  }),
  http.get('/api/v1/activity/:id', ({ request, params }) => {
    const url = new URL(request.url)
    for (const name of url.searchParams.keys()) if (name !== 'advanced') return unknownParameter(request, name)
    const advanced = ['1', 'true'].includes(url.searchParams.get('advanced') ?? '')
    const found = rows.find((r) => r.id === params.id)
    if (!found) return problem(request, 404, `no activity '${String(params.id)}'`)
    return HttpResponse.json({
      row: shape(found, advanced),
      children: rows.filter((r) => r.parent_id === found.id).map((r) => shape(r, advanced)),
      links: [],
      detail: shape(found, advanced).detail,
    })
  }),
  http.get('/api/v1/activity', ({ request }) => {
    const sp = new URL(request.url).searchParams
    const kinds = sp.getAll('kind')
    const facetFilters: [string, string][] = []
    for (const [name, value] of sp) {
      if (LIST_PARAMS.has(name)) continue
      if (!name.startsWith('facet.')) return unknownParameter(request, name)
      const facet = name.slice('facet.'.length)
      const declared = facets.kinds
        .filter((k) => kinds.includes(k.key))
        .flatMap((k) => k.facets)
        .some((f) => f.name === facet && f.filterable)
      if (!declared) {
        return problem(
          request,
          400,
          `no selected kind declares a filterable facet '${facet}'`,
          `${PROBLEM}unknown-parameter`,
        )
      }
      facetFilters.push([facet, value])
    }
    const statuses = sp.getAll('status')
    if (statuses.some((s) => !STATUSES.has(s))) return problem(request, 422, 'status is not a status')
    const limitText = sp.get('limit') ?? '50'
    const limit = Number(limitText)
    if (!/^\d+$/.test(limitText) || limit < 1 || limit > 200) {
      return problem(request, 422, 'limit must be an integer from 1 to 200')
    }
    for (const name of ['since', 'until']) {
      const value = sp.get(name)
      if (value !== null && !validTime(value)) return problem(request, 422, `${name} is not a datetime`)
    }
    const cursorText = sp.get('cursor')
    const cursor = cursorText === null ? null : decodeCursor(cursorText)
    if (cursorText !== null && cursor === null) {
      return problem(request, 400, 'cursor is not a next_cursor', `${PROBLEM}bad-cursor`)
    }
    const q = (sp.get('q') ?? '').toLowerCase()
    const since = sp.get('since')
    const until = sp.get('until')
    const at = (r: Activity) => Date.parse(r.created_at ?? '')
    const filtered = rows
      .filter((r) => kinds.length === 0 || kinds.includes(r.kind))
      .filter((r) => statuses.length === 0 || statuses.includes(r.status))
      .filter((r) => !sp.has('principal') || r.principal === sp.get('principal'))
      .filter((r) => !sp.has('session') || r.session === sp.get('session'))
      .filter((r) => !sp.has('via_tool') || r.via_tool === sp.get('via_tool'))
      .filter((r) => !sp.has('subject') || subjectKey(r.subject) === subjectKey(sp.get('subject')!))
      .filter((r) => !sp.has('parent') || r.parent_id === sp.get('parent'))
      .filter((r) => !since || at(r) >= Date.parse(since))
      .filter((r) => !until || at(r) <= Date.parse(until))
      .filter((r) => !q || `${r.title} ${r.subject}`.toLowerCase().includes(q))
      .filter((r) => facetFilters.every(([name, value]) => String(r.facets?.[name]) === value))
      .sort((a, b) => at(b) - at(a) || b.id.localeCompare(a.id))
    // Keyset: everything after the cursor's (created_at, id), newest first.
    const after = cursor
      ? filtered.filter((r) => at(r) < Date.parse(cursor[0]) || (at(r) === Date.parse(cursor[0]) && r.id < cursor[1]))
      : filtered
    const page = after.slice(0, limit)
    const advanced = ['1', 'true'].includes(sp.get('advanced') ?? '')
    return HttpResponse.json(
      {
        items: page.map((r) => shape(r, advanced)),
        next_cursor: after.length > limit ? encodeCursor(page[page.length - 1]!) : null,
      },
      listCursor === undefined ? undefined : { headers: { [CURSOR_HEADER]: listCursor } },
    )
  }),
]
