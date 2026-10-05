/**
 * #1056 — the agent service's session-mode default for vitest and the mocked build:
 * `GET/PUT /api/v1/ai/settings/session-mode` as agent `src/routes/sessionMode.ts`
 * answers them (`{ mode }`; a value other than classic or durable is a 400 `{ detail }`).
 * Starts as `durable`, so a test sees the picker take the server's default rather than
 * a hard-coded one.
 */
import { HttpResponse, http } from 'msw'
import type { SessionMode } from '../../api/types'

const base = '/api/v1/ai/settings/session-mode'

const state = { mode: 'durable' as SessionMode, writes: [] as SessionMode[] }

export function reset(): void {
  state.mode = 'durable'
  state.writes = []
}

/** Tests: every mode a PUT stored, in order. */
export function mockSessionModeWrites(): readonly SessionMode[] {
  return state.writes
}

export const handlers = [
  http.get(base, () => HttpResponse.json({ mode: state.mode })),

  http.put(base, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    if (body.mode !== 'classic' && body.mode !== 'durable') {
      return HttpResponse.json({ detail: 'mode: Invalid option: expected one of "classic"|"durable"' }, { status: 400 })
    }
    state.mode = body.mode
    state.writes.push(body.mode)
    return HttpResponse.json({ mode: state.mode })
  }),
]
