import { HttpResponse, http } from 'msw'
import type { RenderLimits } from '../../api/types'

/**
 * The agent service's `/api/v1/ai/settings/render-limits` (agent
 * `src/routes/renderLimits.ts`) for vitest and the mocked build: the same body,
 * defaults and bounds.
 */

const path = '/api/v1/ai/settings/render-limits'

/** agent `src/tools/renderLimits.ts` MAX_RENDER_*. */
export const MAX_CONCURRENT = 64
export const MAX_GLOBAL = 256
export const MAX_PER_WINDOW = 10_000

const DEFAULTS: RenderLimits = { per_principal: 4, global: 0, per_window: 0, overrides: [] }

const state = { limits: structuredClone(DEFAULTS), writes: [] as RenderLimits[] }

export function reset(): void {
  state.limits = structuredClone(DEFAULTS)
  state.writes = []
}

/** Every write, for tests. */
export function mockRenderLimitWrites(): readonly RenderLimits[] {
  return state.writes
}

const int = (value: unknown, min: number, max: number) =>
  typeof value === 'number' && Number.isInteger(value) && value >= min && value <= max

const detail = (text: string) => HttpResponse.json({ detail: text }, { status: 400 })

export const handlers = [
  http.get(path, () => HttpResponse.json(state.limits)),

  http.put(path, async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const extra = Object.keys(body).filter((k) => !['per_principal', 'global', 'per_window', 'overrides'].includes(k))
    if (extra.length) return detail(`body: unrecognized key(s) ${extra.join(', ')}`)
    if (!int(body.per_principal, 1, MAX_CONCURRENT)) return detail(`per_principal: must be from 1 to ${MAX_CONCURRENT}`)
    if (!int(body.global, 0, MAX_GLOBAL)) return detail(`global: must be from 0 to ${MAX_GLOBAL}`)
    if (!int(body.per_window, 0, MAX_PER_WINDOW)) return detail(`per_window: must be from 0 to ${MAX_PER_WINDOW}`)
    const overrides = body.overrides
    if (!Array.isArray(overrides)) return detail('overrides: expected array')
    const seen = new Set<string>()
    for (const o of overrides as { principal?: unknown; concurrent?: unknown }[]) {
      if (typeof o.principal !== 'string' || o.principal.trim() === '') return detail('overrides: principal is empty')
      if (!int(o.concurrent, 1, MAX_CONCURRENT)) return detail(`overrides: concurrent must be from 1 to ${MAX_CONCURRENT}`)
      if (seen.has(o.principal.trim())) return detail('overrides: each principal may have one override')
      seen.add(o.principal.trim())
    }
    state.limits = body as unknown as RenderLimits
    state.writes.push(state.limits)
    return HttpResponse.json(state.limits)
  }),
]
