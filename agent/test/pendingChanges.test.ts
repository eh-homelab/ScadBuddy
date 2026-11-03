import { Hono } from 'hono'
import { describe, expect, it } from 'vitest'
import { PENDING_CHANNEL, PendingChanges } from '../src/gate/changes.js'
import { originPolicy } from '../src/http/origins.js'
import { PENDING_CHANGES_PATH, registerPendingInputRoutes } from '../src/routes/pendingInput.js'

// GET /api/v1/ai/pending-input/changes (#815): a tick whenever anything parked on a person
// may have opened or closed, so the badge re-reads at once instead of on its 15 s poll.

const UI_READ = { host: 'scadbuddy.example', 'x-forwarded-proto': 'https', 'sec-fetch-site': 'same-origin' }

function fakeListener() {
  const channels = new Map<string, (payload: string) => void>()
  const reconnects: (() => void)[] = []
  return {
    listenAlso: (channel: string, fn: (payload: string) => void) => void channels.set(channel, fn),
    onReconnect: (fn: () => void) => void reconnects.push(fn),
    notify: (channel: string) => channels.get(channel)?.(''),
    reconnect: () => reconnects.forEach((fn) => fn()),
  }
}

function setup(changes: PendingChanges | undefined) {
  const app = new Hono()
  const shutdown = new AbortController()
  registerPendingInputRoutes(app, {
    sessions: undefined,
    ready: () => Promise.resolve(true),
    remoteAddress: () => '10.0.0.7',
    origins: originPolicy('https://scadbuddy.example', '10.0.0.0/8'),
    changes,
    shutdown: shutdown.signal,
  })
  return { app, shutdown }
}

async function nextChunk(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<string> {
  const { value } = await reader.read()
  return new TextDecoder().decode(value)
}

describe('PendingChanges', () => {
  it('listens on the pending channel and tells every subscriber of a NOTIFY and of a reconnect', () => {
    const listener = fakeListener()
    const changes = new PendingChanges(listener)
    const heard: string[] = []
    const off = changes.subscribe(() => heard.push('a'))
    changes.subscribe(() => heard.push('b'))
    listener.notify(PENDING_CHANNEL)
    listener.reconnect()
    off()
    listener.notify(PENDING_CHANNEL)
    expect(heard).toEqual(['a', 'b', 'a', 'b', 'b'])
  })

  it('keeps telling the others when one subscriber throws', () => {
    const listener = fakeListener()
    const changes = new PendingChanges(listener)
    const heard: string[] = []
    changes.subscribe(() => {
      throw new Error('boom')
    })
    changes.subscribe(() => heard.push('ok'))
    listener.notify(PENDING_CHANNEL)
    expect(heard).toEqual(['ok'])
  })
})

describe('GET /api/v1/ai/pending-input/changes', () => {
  it('sends a change at once, then one per NOTIFY, and nothing that names an entry', async () => {
    const listener = fakeListener()
    const { app, shutdown } = setup(new PendingChanges(listener))
    const res = await app.request(PENDING_CHANGES_PATH, { headers: UI_READ })
    expect(res.status).toBe(200)
    expect(res.headers.get('content-type')).toContain('text/event-stream')
    const reader = res.body!.getReader()
    expect(await nextChunk(reader)).toBe('event: changed\ndata: \n\n')
    listener.notify(PENDING_CHANNEL)
    expect(await nextChunk(reader)).toBe('event: changed\ndata: \n\n')
    shutdown.abort()
    expect((await reader.read()).done).toBe(true)
  })

  it('is refused from another site, and 503 without the event bus', async () => {
    const { app } = setup(new PendingChanges(fakeListener()))
    const cross = await app.request(PENDING_CHANGES_PATH, { headers: { ...UI_READ, 'sec-fetch-site': 'cross-site' } })
    expect(cross.status).toBe(403)
    const off = await setup(undefined).app.request(PENDING_CHANGES_PATH, { headers: UI_READ })
    expect(off.status).toBe(503)
  })
})
