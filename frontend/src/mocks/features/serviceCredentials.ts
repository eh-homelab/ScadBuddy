import { HttpResponse, http } from 'msw'
import type { ServiceCaller, ServiceCredentialView } from '../../api/types'

/** #4975 phase 4 — the services' credentials: one live per caller by default. */

let counter = 0

function credential(caller: ServiceCaller): ServiceCredentialView {
  counter += 1
  return {
    id: counter.toString(16).padStart(16, '0'),
    caller,
    scopes: caller === 'print' ? ['internal.library', 'internal.outputs'] : ['api'],
    created_at: '2026-11-01T12:00:00Z',
    expires_at: '2026-12-01T12:00:00Z',
    revoked_at: null,
    last_used_at: caller === 'agent' ? '2026-11-01T12:30:00Z' : null,
    proof: 'legacy',
    pod_name: null,
    pod_uid: null,
    live: true,
  }
}

let rows: ServiceCredentialView[] = []

export function reset(): void {
  counter = 0
  rows = (['agent', 'print', 'projects'] as const).map(credential)
}
reset()

export const handlers = [
  http.get('/api/v1/auth/service-credentials', () => HttpResponse.json(rows)),
  http.post('/api/v1/auth/service-credentials/rotate', async ({ request }) => {
    const { caller } = (await request.json()) as { caller: ServiceCaller }
    rows = [credential(caller), ...rows]
    return HttpResponse.json(rows)
  }),
  http.delete('/api/v1/auth/service-credentials/:id', ({ params }) => {
    const row = rows.find((r) => r.id === params.id && r.live)
    if (!row) return HttpResponse.json({ detail: 'No live credential has that id' }, { status: 404 })
    rows = rows.map((r) => (r === row ? { ...r, live: false, revoked_at: '2026-11-02T00:00:00Z' } : r))
    if (!rows.some((r) => r.caller === row.caller && r.live)) rows = [credential(row.caller), ...rows]
    return HttpResponse.json(rows)
  }),
]
