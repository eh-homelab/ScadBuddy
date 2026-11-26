import { HttpResponse, http } from 'msw'

/**
 * The assistant's image settings, served by the agent service
 * (agent/src/routes/imageSettings.ts): the long edge the panel scales attached images to
 * (`ai_settings` `image_long_edge`) and the largest image a tool shows the model inline
 * (`tool_image_inline_max_bytes`, #7745).
 */

const MIN = 200
const MAX = 2576
const DEFAULT = 1568
const INLINE_MIN = 16 * 1024
const INLINE_MAX = 3_932_160
const INLINE_DEFAULT = 512 * 1024

const state = { longEdge: DEFAULT, inlineMax: INLINE_DEFAULT }

export function reset(): void {
  state.longEdge = DEFAULT
  state.inlineMax = INLINE_DEFAULT
}

const view = () => ({
  long_edge: state.longEdge,
  min: MIN,
  max: MAX,
  tool_inline_max_bytes: state.inlineMax,
  tool_inline_min: INLINE_MIN,
  tool_inline_max: INLINE_MAX,
})

const within = (value: unknown, min: number, max: number) =>
  typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max

export const handlers = [
  http.get('/api/v1/ai/settings/images', () => HttpResponse.json(view())),
  http.put('/api/v1/ai/settings/images', async ({ request }) => {
    const body = (await request.json()) as { long_edge?: unknown; tool_inline_max_bytes?: unknown }
    if (body.long_edge === undefined && body.tool_inline_max_bytes === undefined) {
      return HttpResponse.json({ detail: 'give long_edge or tool_inline_max_bytes' }, { status: 400 })
    }
    if (body.long_edge !== undefined && !within(body.long_edge, MIN, MAX)) {
      return HttpResponse.json({ detail: `long_edge: an integer from ${MIN} to ${MAX}` }, { status: 400 })
    }
    if (body.tool_inline_max_bytes !== undefined && !within(body.tool_inline_max_bytes, INLINE_MIN, INLINE_MAX)) {
      return HttpResponse.json({ detail: `tool_inline_max_bytes: an integer from ${INLINE_MIN} to ${INLINE_MAX}` }, { status: 400 })
    }
    if (body.long_edge !== undefined) state.longEdge = body.long_edge as number
    if (body.tool_inline_max_bytes !== undefined) state.inlineMax = body.tool_inline_max_bytes as number
    return HttpResponse.json(view())
  }),
]
