import { HttpResponse, http } from 'msw'
import type { NotifyChannel } from '../../api/types'

/**
 * #815 — the agent's notification channels (agent `src/routes/notifyChannels.ts`): ntfy and
 * Home Assistant, kept in memory. A test send always succeeds here.
 */

let channels: NotifyChannel[] = []
let next = 1

export function reset(): void {
  channels = []
  next = 1
}

/** What the mocked agent holds now (tests). */
export const notifyChannelsState = (): NotifyChannel[] => channels.map((c) => ({ ...c }))

type Body = Partial<{ kind: string; name: string; url: string; target: string | null; token: string | null; enabled: boolean }>

const last4 = (token: string) => (token.length < 12 ? '' : token.slice(-4))

function refusal(body: Body, kind: string): string | null {
  if (kind !== 'ntfy' && kind !== 'home_assistant') return 'kind must be one of ntfy, home_assistant'
  if (!body.name?.trim()) return 'name must be 1 to 100 characters'
  try {
    new URL(body.url ?? '')
  } catch {
    return 'url is not a URL'
  }
  if (kind === 'home_assistant') {
    if (!/^[a-z0-9_]{1,100}$/.test(body.target ?? '')) return 'target must be a Home Assistant notify service name, such as mobile_app_pixel'
    if (!body.token) return 'Home Assistant needs a long-lived access token'
  }
  return null
}

export const handlers = [
  http.get('/api/v1/ai/notify/channels', () => HttpResponse.json({ channels })),
  http.post('/api/v1/ai/notify/channels', async ({ request }) => {
    const body = (await request.json()) as Body
    const kind = body.kind ?? ''
    const problem = refusal(body, kind)
    if (problem) return HttpResponse.json({ detail: problem }, { status: 400 })
    const channel: NotifyChannel = {
      id: `channel-${next++}`,
      kind: kind as NotifyChannel['kind'],
      name: body.name!.trim(),
      url: body.url!,
      target: kind === 'home_assistant' ? body.target! : null,
      enabled: body.enabled ?? true,
      has_token: Boolean(body.token),
      token_last4: body.token ? last4(body.token) : '',
      last_error: null,
      last_sent_at: null,
    }
    channels = [...channels, channel]
    return HttpResponse.json(channel, { status: 201 })
  }),
  http.put('/api/v1/ai/notify/channels/:id', async ({ params, request }) => {
    const body = (await request.json()) as Body
    const found = channels.find((c) => c.id === params.id)
    if (!found) return HttpResponse.json({ detail: `no channel ${String(params.id)}` }, { status: 404 })
    const updated = { ...found, ...(body.enabled === undefined ? {} : { enabled: body.enabled }), ...(body.name ? { name: body.name } : {}) }
    channels = channels.map((c) => (c.id === found.id ? updated : c))
    return HttpResponse.json(updated)
  }),
  http.delete('/api/v1/ai/notify/channels/:id', ({ params }) => {
    const before = channels.length
    channels = channels.filter((c) => c.id !== params.id)
    return channels.length < before ? new HttpResponse(null, { status: 204 }) : HttpResponse.json({ detail: 'no channel' }, { status: 404 })
  }),
  http.post('/api/v1/ai/notify/channels/:id/test', ({ params }) =>
    channels.some((c) => c.id === params.id) ? HttpResponse.json({ ok: true }) : HttpResponse.json({ detail: 'no channel' }, { status: 404 }),
  ),
]
