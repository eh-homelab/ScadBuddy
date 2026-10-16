import { HttpResponse, http } from 'msw'
import type { DurableSubagentsSetting } from '../../api/types'

/**
 * Where durable subagents reach ScadBuddy's tools (`ai_settings` `durable_subagents`),
 * served by the agent service (agent/src/routes/durableSubagents.ts). Loopback until set.
 */

const state: DurableSubagentsSetting = { url: null, peers: [] }

export function reset(): void {
  state.url = null
  state.peers = []
}

const CIDR = /^[0-9a-f:.]+(\/\d{1,3})?$/i

export const handlers = [
  http.get('/api/v1/ai/settings/durable-subagents', () => HttpResponse.json({ ...state })),
  http.put('/api/v1/ai/settings/durable-subagents', async ({ request }) => {
    const body = (await request.json()) as Record<string, unknown>
    const { url, peers } = body
    let href: string | null = null
    if (url !== null) {
      try {
        href = typeof url === 'string' ? new URL(url).href : null
      } catch {
        href = null
      }
      if (href === null || !/^https?:$/.test(new URL(href).protocol)) {
        return HttpResponse.json({ detail: 'url: must be an http(s) URL' }, { status: 400 })
      }
    }
    if (!Array.isArray(peers) || !peers.every((p) => typeof p === 'string' && CIDR.test(p.trim()))) {
      return HttpResponse.json({ detail: 'peers: each entry must be an IP address or CIDR range' }, { status: 400 })
    }
    // As the agent's route stores it: WHATWG's parse of what was sent.
    state.url = href
    state.peers = peers.map((p: string) => p.trim())
    return HttpResponse.json({ ...state })
  }),
]
