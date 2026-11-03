import { renderHook, waitFor } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { server } from '../mocks/server'
import { enableWebPush, PUSH_KEY_PATH, PUSH_SUBSCRIPTION_PATH, PUSH_WORKER_SCOPE, PUSH_WORKER_URL, SESSION_PARAM, useWebPush } from './webPush'

// #815 §3 — the page's half of Web Push: subscribe once notifications are granted, hand the
// subscription to the agent, and open the session a notification click names.

const KEY = 'BP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vCYLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A8'
const SUBSCRIPTION = { endpoint: 'https://push.example.net/send/abc', expirationTime: null, keys: { p256dh: 'p', auth: 'a' } }

function fakeWorker() {
  const registered: { url: string; scope: string | undefined }[] = []
  const subscribed: unknown[] = []
  const messages = new EventTarget() as EventTarget & { startMessages: () => void; register: unknown }
  messages.startMessages = () => {}
  messages.register = (url: string, options?: { scope?: string }) => {
    registered.push({ url, scope: options?.scope })
    return Promise.resolve({
      pushManager: {
        getSubscription: () => Promise.resolve(null),
        subscribe: (options: unknown) => {
          subscribed.push(options)
          return Promise.resolve({ options: { applicationServerKey: null }, toJSON: () => SUBSCRIPTION, unsubscribe: () => Promise.resolve(true) })
        },
      },
    })
  }
  Object.defineProperty(navigator, 'serviceWorker', { value: messages, configurable: true })
  vi.stubGlobal('PushManager', class {})
  return { registered, subscribed, messages }
}

let permission: NotificationPermission = 'granted'
beforeEach(() => {
  permission = 'granted'
  vi.stubGlobal('Notification', { get permission() { return permission } })
})
afterEach(() => {
  vi.unstubAllGlobals()
  Reflect.deleteProperty(navigator, 'serviceWorker')
})

describe('enableWebPush', () => {
  it('registers the worker at its own scope, subscribes with the agent key and gives the agent the subscription', async () => {
    const worker = fakeWorker()
    const puts: unknown[] = []
    server.use(
      http.get(PUSH_KEY_PATH, () => HttpResponse.json({ public_key: KEY })),
      http.put(PUSH_SUBSCRIPTION_PATH, async ({ request }) => {
        puts.push(await request.json())
        return new HttpResponse(null, { status: 204 })
      }),
    )
    expect(await enableWebPush()).toBe(true)
    expect(worker.registered).toEqual([{ url: PUSH_WORKER_URL, scope: PUSH_WORKER_SCOPE }])
    const [options] = worker.subscribed as { userVisibleOnly: boolean; applicationServerKey: Uint8Array }[]
    expect(options!.userVisibleOnly).toBe(true)
    expect(options!.applicationServerKey).toHaveLength(65)
    expect(puts).toEqual([SUBSCRIPTION])
  })

  it('does nothing until notifications are granted, or without push in the browser', async () => {
    permission = 'default'
    const worker = fakeWorker()
    expect(await enableWebPush()).toBe(false)
    expect(worker.registered).toEqual([])
    permission = 'granted'
    Reflect.deleteProperty(navigator, 'serviceWorker')
    expect(await enableWebPush()).toBe(false)
  })

  it('is false, not an error, when the agent has no push (503)', async () => {
    fakeWorker()
    server.use(http.get(PUSH_KEY_PATH, () => HttpResponse.json({ detail: 'no KEK' }, { status: 503 })))
    expect(await enableWebPush()).toBe(false)
  })
})

describe('useWebPush', () => {
  it("opens the session a notification click names, from the worker's message or the page's address", async () => {
    const worker = fakeWorker()
    server.use(http.get(PUSH_KEY_PATH, () => HttpResponse.json({ detail: 'off' }, { status: 503 })))
    window.history.replaceState(null, '', `/models?${SESSION_PARAM}=s-1&x=1`)
    const opened: (string | null)[] = []
    const { unmount } = renderHook(() => useWebPush(true, (id) => opened.push(id)))
    await waitFor(() => expect(opened).toEqual(['s-1']))
    expect(window.location.pathname + window.location.search).toBe('/models?x=1')

    worker.messages.dispatchEvent(new MessageEvent('message', { data: { type: 'scadbuddy.openSession', sessionId: 's-2' } }))
    worker.messages.dispatchEvent(new MessageEvent('message', { data: { type: 'other' } }))
    expect(opened).toEqual(['s-1', 's-2'])
    unmount()
    worker.messages.dispatchEvent(new MessageEvent('message', { data: { type: 'scadbuddy.openSession', sessionId: 's-3' } }))
    expect(opened).toEqual(['s-1', 's-2'])
  })

  it('does nothing while off (embedded, or the assistant unavailable)', () => {
    const worker = fakeWorker()
    window.history.replaceState(null, '', `/?${SESSION_PARAM}=s-1`)
    const opened: (string | null)[] = []
    renderHook(() => useWebPush(false, (id) => opened.push(id)))
    expect(opened).toEqual([])
    expect(worker.registered).toEqual([])
    window.history.replaceState(null, '', '/')
  })
})
