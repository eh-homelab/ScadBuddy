import { HttpResponse, http } from 'msw'

/**
 * The long edge the assistant panel scales attached images to (`ai_settings`
 * `image_long_edge`), served by the agent service (agent/src/routes/imageSettings.ts).
 */

const MIN = 200
const MAX = 2576
const DEFAULT = 1568

const state = { longEdge: DEFAULT }

export function reset(): void {
  state.longEdge = DEFAULT
}

const view = () => ({ long_edge: state.longEdge, min: MIN, max: MAX })

export const handlers = [
  http.get('/api/v1/ai/settings/images', () => HttpResponse.json(view())),
  http.put('/api/v1/ai/settings/images', async ({ request }) => {
    const body = (await request.json()) as { long_edge?: unknown }
    const edge = body.long_edge
    if (typeof edge !== 'number' || !Number.isInteger(edge) || edge < MIN || edge > MAX) {
      return HttpResponse.json({ detail: `long_edge: an integer from ${MIN} to ${MAX}` }, { status: 400 })
    }
    state.longEdge = edge
    return HttpResponse.json(view())
  }),
]
