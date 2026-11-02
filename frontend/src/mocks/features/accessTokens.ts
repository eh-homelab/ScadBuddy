import { HttpResponse, http } from 'msw'
import type { AccessTokenCreate, AccessTokenView } from '../../api/types'

/** #4975 phase 6 — a person's access tokens: none by default. */

let counter = 0
let rows: AccessTokenView[] = []

export function reset(): void {
  counter = 0
  rows = []
}

export const handlers = [
  http.get('/api/v1/auth/access-tokens', () => HttpResponse.json(rows)),
  http.post('/api/v1/auth/access-tokens', async ({ request }) => {
    const body = (await request.json()) as AccessTokenCreate
    counter += 1
    const id = counter.toString(16).padStart(16, '0')
    const row: AccessTokenView = {
      id,
      name: body.name,
      scope: body.scope ?? 'read',
      created_at: '2026-11-02T12:00:00Z',
      expires_at: '2027-01-31T12:00:00Z',
      last_used_at: null,
      revoked_at: null,
      live: true,
    }
    rows = [row, ...rows]
    return HttpResponse.json({ token: `sbpat_${id}.${'x'.repeat(43)}`, access_token: row }, { status: 201 })
  }),
  http.delete('/api/v1/auth/access-tokens/:id', ({ params }) => {
    const row = rows.find((r) => r.id === params.id && r.live)
    if (!row) return HttpResponse.json({ detail: 'no live token of yours has that id' }, { status: 404 })
    rows = rows.map((r) => (r === row ? { ...r, live: false, revoked_at: '2026-11-02T13:00:00Z' } : r))
    return new HttpResponse(null, { status: 204 })
  }),
]
