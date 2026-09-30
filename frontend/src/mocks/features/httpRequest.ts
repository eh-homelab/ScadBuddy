import { HttpResponse, http } from 'msw'

/**
 * #827 — the agent's switch for the assistant's `http_request` tool (`ai_settings`),
 * served by the agent service (agent/src/routes/httpRequest.ts). On by default.
 */

const state = { enabled: true }

export function reset(): void {
  state.enabled = true
}

export const handlers = [
  http.get('/api/v1/ai/settings/http-request', () => HttpResponse.json({ enabled: state.enabled })),
  http.put('/api/v1/ai/settings/http-request', async ({ request }) => {
    const body = (await request.json()) as { enabled?: unknown }
    if (typeof body.enabled !== 'boolean') {
      return HttpResponse.json({ detail: 'enabled: expected boolean' }, { status: 400 })
    }
    state.enabled = body.enabled
    return HttpResponse.json({ enabled: state.enabled })
  }),
]
