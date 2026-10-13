import { HttpResponse, http } from 'msw'

/**
 * The automatic Hindsight recall query's cap, in characters (`ai_settings`
 * `memory_recall_max_query_chars`), served by the agent service
 * (agent/src/routes/memorySettings.ts).
 */

const DEFAULT = 800
const MAX = 100_000

const state = { chars: DEFAULT }

export function reset(): void {
  state.chars = DEFAULT
}

const view = () => ({ recall_max_query_chars: state.chars, default: DEFAULT, max: MAX })

export const handlers = [
  http.get('/api/v1/ai/settings/memory', () => HttpResponse.json(view())),
  http.put('/api/v1/ai/settings/memory', async ({ request }) => {
    const body = (await request.json()) as { recall_max_query_chars?: unknown }
    const chars = body.recall_max_query_chars
    if (typeof chars !== 'number' || !Number.isInteger(chars) || chars < 0 || chars > MAX) {
      return HttpResponse.json({ detail: `recall_max_query_chars: an integer from 0 to ${MAX}` }, { status: 400 })
    }
    state.chars = chars
    return HttpResponse.json(view())
  }),
]
