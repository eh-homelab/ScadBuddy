import { HttpResponse, http } from 'msw'
import { mockDestinationState, mockModels } from '../handlers'
import { state as orders } from './orders/state'

// `GET /outputs` (#4777, order operations spec 2026-11-07 §3.1): every model's saved
// outputs, newest first, with the backend's filters and paging (`api/outputs.py`
// `list_recent_outputs`). The mock's `q` matches the name or any `name=value` of the
// params; the backend matches only the params that differ from the template's defaults.

const DEFAULT_LIMIT = 50

function text(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value)
}

export const handlers = [
  http.get('/api/v1/outputs', ({ request }) => {
    const query = new URL(request.url).searchParams
    const from = query.get('from')
    const to = query.get('to')
    const slugs = query.getAll('slug')
    const tag = query.get('tag')?.toLocaleLowerCase()
    const q = query.get('q')?.toLocaleLowerCase()
    const ordered = query.get('ordered')
    const tagged = tag === undefined ? null : new Set(mockModels().filter((m) => (m.tags ?? []).some((t) => t.toLocaleLowerCase() === tag)).map((m) => m.slug))
    const named = new Set(orders.items.map((i) => i.output_id).filter((id) => id !== null))
    const listed = [...mockDestinationState().outputs]
      .filter((o) => from === null || Date.parse(o.created_at) >= Date.parse(from))
      .filter((o) => to === null || Date.parse(o.created_at) < Date.parse(to))
      .filter((o) => slugs.length === 0 || slugs.includes(o.slug))
      .filter((o) => tagged === null || tagged.has(o.slug))
      .filter((o) => !q || [o.name ?? '', ...Object.entries(o.params ?? {}).map(([k, v]) => `${k}=${text(v)}`)].some((t) => t.toLocaleLowerCase().includes(q)))
      .filter((o) => ordered === null || (ordered === 'yes') === named.has(o.id))
      .sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at) || b.id.localeCompare(a.id))
    const after = query.get('after')
    let start = 0
    if (after !== null) {
      start = listed.findIndex((o) => o.id === after) + 1
      if (start === 0) {
        return HttpResponse.json(
          { type: 'about:blank', title: 'Conflict', status: 409, detail: `no output '${after}' is listed any more to page after; list again without \`after\`` },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }
    }
    const limit = Number(query.get('limit') ?? DEFAULT_LIMIT)
    return HttpResponse.json(listed.slice(start, start + limit), { headers: { 'X-Total-Count': String(listed.length) } })
  }),
]
