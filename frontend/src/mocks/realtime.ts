import { ws } from 'msw'

/**
 * #266 — the mock `WS /api/v1/ws`: it confirms subscriptions the way the backend
 * does (`backend/scadbuddy/api/realtime.py`) and lets a test push events with
 * {@link emitRealtime}. Mutation handlers call it for the kinds a view follows.
 */
export const realtimeLink = ws.link('*/api/v1/ws')

/** Topics each connected client follows, so an event reaches only its followers. */
const followers = new Map<{ send(data: string): void }, Set<string>>()

export const realtimeHandler = realtimeLink.addEventListener('connection', ({ client }) => {
  const topics = new Set<string>()
  followers.set(client, topics)
  client.addEventListener('message', (message) => {
    const frame = JSON.parse(String(message.data)) as { type: string; topics?: string[] }
    const requested = frame.topics ?? []
    if (frame.type === 'subscribe') {
      for (const topic of requested) topics.add(topic)
      client.send(JSON.stringify({ type: 'subscribed', topics: requested }))
    } else if (frame.type === 'unsubscribe') {
      for (const topic of requested) topics.delete(topic)
    }
  })
  client.addEventListener('close', () => followers.delete(client))
})

let seq = 0

/** Deliver `kind` to every client following one of `topics`. */
export function emitRealtime(kind: string, topics: string[], data: Record<string, unknown> = {}) {
  for (const [client, followed] of followers) {
    const matched = topics.filter((topic) => followed.has(topic))
    if (matched.length === 0) continue
    client.send(JSON.stringify({ type: 'event', id: `mock-${++seq}`, kind, topics: matched, data }))
  }
}

/**
 * #265 — as the backend's follow of a background slice (`preview_follow/follows.py`):
 * a read that finds the slice running is followed by a change on its topic, after the
 * interval the backend reads at.
 */
export const PREVIEW_FOLLOW_MS = 2000

export function announcePreviewSlice(slicer: 'bambuddy' | 'native', jobId: number) {
  setTimeout(() => emitRealtime('preview_slice.progress', [`preview-slice:${slicer}:${jobId}`]), PREVIEW_FOLLOW_MS)
}
