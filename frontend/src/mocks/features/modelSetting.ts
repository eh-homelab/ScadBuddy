import { HttpResponse, http } from 'msw'

/**
 * #1917 — the Claude model the assistant uses (`ai_settings.model`), served by the
 * agent service (agent/src/routes/model.ts). `null` is Claude Code's default.
 */

/** agent/src/routes/model.ts MODEL_NAME. */
const MODEL_NAME = /^[A-Za-z0-9][A-Za-z0-9._:@/[\]-]{0,127}$/

const state: { model: string | null } = { model: null }

export function reset(): void {
  state.model = null
}

export const handlers = [
  http.get('/api/v1/ai/settings/model', () => HttpResponse.json({ model: state.model })),
  http.put('/api/v1/ai/settings/model', async ({ request }) => {
    const body = (await request.json()) as { model?: unknown }
    const model = typeof body.model === 'string' ? body.model.trim() : body.model
    if (model !== null && (typeof model !== 'string' || !MODEL_NAME.test(model))) {
      return HttpResponse.json(
        { detail: 'model: must be a model name or alias, such as opus or claude-sonnet-5 (letters, digits and . _ : @ / [ ] -)' },
        { status: 400 },
      )
    }
    state.model = model
    return HttpResponse.json({ model: state.model })
  }),
]
