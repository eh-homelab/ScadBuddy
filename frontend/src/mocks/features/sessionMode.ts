import { HttpResponse, http } from 'msw'
import type { SessionModeSetting } from '../../api/types'

/**
 * Plan 5d — the mode new assistant sessions get by default (`ai_settings`
 * `session_mode`), served by the agent service (agent/src/routes/sessionMode.ts).
 * Durable until set; durable sessions can start unless a test says not.
 */

const state: { mode: SessionModeSetting['mode']; available: boolean } = { mode: 'durable', available: true }

export function reset(): void {
  state.mode = 'durable'
  state.available = true
}

/** Tests: whether a durable session could start now. */
export function setDurableAvailable(available: boolean): void {
  state.available = available
}

const view = (): SessionModeSetting =>
  state.available
    ? { mode: state.mode, durable_available: true }
    : {
        mode: state.mode,
        durable_available: false,
        durable_unavailable_reason: 'no durable session worker (agent-durable) polls Temporal\'s "agent" queue',
      }

export const handlers = [
  http.get('/api/v1/ai/settings/session-mode', () => HttpResponse.json(view())),
  http.put('/api/v1/ai/settings/session-mode', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    if ((body.mode !== 'classic' && body.mode !== 'durable') || Object.keys(body).length !== 1) {
      return HttpResponse.json({ detail: 'mode: Invalid option: expected one of "classic"|"durable"' }, { status: 400 })
    }
    state.mode = body.mode
    return HttpResponse.json(view())
  }),
]
