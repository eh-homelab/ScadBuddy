import { HttpResponse, http } from 'msw'

/**
 * #815 — the agent's Web Push routes (agent `src/routes/push.ts`). The mocked agent has no
 * VAPID key, as an agent without SCADBUDDY_SECRET_KEY_FILE has none, so the page never
 * subscribes here; the page's own notifications still work.
 */
export const handlers = [
  http.get('/api/v1/ai/push/key', () =>
    HttpResponse.json({ detail: 'Web Push needs the database and SCADBUDDY_SECRET_KEY_FILE (its keys are kept sealed)' }, { status: 503 }),
  ),
  http.put('/api/v1/ai/push/subscription', () => new HttpResponse(null, { status: 204 })),
  http.delete('/api/v1/ai/push/subscription', () => new HttpResponse(null, { status: 204 })),
]
