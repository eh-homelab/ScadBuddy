import { HttpResponse, http } from 'msw'

/**
 * #1911 — the agent's switch for the assistant's `get_printer_camera` tool
 * (`ai_settings`), served by the agent service (agent/src/routes/printerCamera.ts).
 * On by default.
 */

const state = { enabled: true }

export function reset(): void {
  state.enabled = true
}

export const handlers = [
  http.get('/api/v1/ai/settings/printer-camera', () => HttpResponse.json({ enabled: state.enabled })),
  http.put('/api/v1/ai/settings/printer-camera', async ({ request }) => {
    const body = (await request.json()) as { enabled?: unknown }
    if (typeof body.enabled !== 'boolean') {
      return HttpResponse.json({ detail: 'enabled: expected boolean' }, { status: 400 })
    }
    state.enabled = body.enabled
    return HttpResponse.json({ enabled: state.enabled })
  }),
]
