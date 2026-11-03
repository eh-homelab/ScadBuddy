import { HttpResponse, http } from 'msw'

/**
 * #815 — the agent's Web Push routes (agent `src/routes/push.ts`). The mocked agent has no
 * VAPID key, as an agent without SCADBUDDY_SECRET_KEY_FILE has none, so the page never
 * subscribes here; the page's own notifications still work. The push-service allowlist
 * (`/api/v1/ai/settings/push-services`) is kept in memory.
 */

const DEFAULT = ['fcm.googleapis.com', 'updates.push.services.mozilla.com', '*.notify.windows.com', '*.push.apple.com', 'web.push.apple.com']
const PATTERN = /^(\*\.)?([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/

const state = { hosts: [...DEFAULT] }

export function reset(): void {
  state.hosts = [...DEFAULT]
}

/** What the mocked agent holds now (tests). */
export const pushServicesState = () => ({ hosts: [...state.hosts], default: [...DEFAULT] })

export const handlers = [
  http.get('/api/v1/ai/push/key', () =>
    HttpResponse.json({ detail: 'Web Push needs the database and SCADBUDDY_SECRET_KEY_FILE (its keys are kept sealed)' }, { status: 503 }),
  ),
  http.put('/api/v1/ai/push/subscription', () => new HttpResponse(null, { status: 204 })),
  http.delete('/api/v1/ai/push/subscription', () => new HttpResponse(null, { status: 204 })),
  http.get('/api/v1/ai/settings/push-services', () => HttpResponse.json(pushServicesState())),
  http.put('/api/v1/ai/settings/push-services', async ({ request }) => {
    const body = (await request.json()) as { hosts?: unknown }
    if (!Array.isArray(body.hosts)) return HttpResponse.json({ detail: 'hosts: a list' }, { status: 400 })
    const hosts = body.hosts.map((h) => String(h).trim().toLowerCase())
    const bad = hosts.findIndex((h) => !PATTERN.test(h))
    if (bad >= 0) return HttpResponse.json({ detail: `hosts.${bad}: must be a host name, or *. and one` }, { status: 400 })
    state.hosts = [...new Set(hosts)]
    return HttpResponse.json(pushServicesState())
  }),
]
