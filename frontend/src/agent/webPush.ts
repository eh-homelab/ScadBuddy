import { useEffect, useRef } from 'react'

/**
 * #815 §3 — Web Push, so a pending request still alerts with every ScadBuddy tab closed.
 * Once notifications are granted (only ever from a click, `NotificationsOffer`), the page
 * registers `public/push-sw.js`, subscribes with the agent's VAPID key and gives the agent
 * the subscription (agent `routes/push.ts`); the agent then pushes each request waiting on the person
 * (`notify/dispatcher.ts`). The worker's scope is `/push-sw/`, so it controls no page.
 *
 * Off when embedded in Bambuddy, like the page's own notifications, and wherever the
 * browser has no service worker or push manager (an insecure origin, an old browser).
 */

export const PUSH_KEY_PATH = '/api/v1/ai/push/key'
export const PUSH_SUBSCRIPTION_PATH = '/api/v1/ai/push/subscription'
export const PUSH_WORKER_URL = '/push-sw.js'
export const PUSH_WORKER_SCOPE = '/push-sw/'

/** Whether this browser can take pushes at all. */
export function webPushSupported(): boolean {
  return typeof navigator !== 'undefined' && 'serviceWorker' in navigator && typeof PushManager !== 'undefined'
}

const keyBytes = (base64url: string): Uint8Array<ArrayBuffer> => {
  const text = atob(base64url.replaceAll('-', '+').replaceAll('_', '/'))
  const bytes = new Uint8Array(new ArrayBuffer(text.length))
  for (let i = 0; i < text.length; i++) bytes[i] = text.charCodeAt(i)
  return bytes
}

const sameKey = (a: ArrayBuffer | null, b: Uint8Array): boolean => {
  if (a === null || a.byteLength !== b.byteLength) return false
  const view = new Uint8Array(a)
  return view.every((byte, i) => byte === b[i])
}

/**
 * Subscribes this browser and hands the subscription to the agent; true once the agent has
 * it. Safe to call on every load: an existing subscription for the same key is sent again
 * (the agent replaces it), one for another key (the agent's was replaced) is renewed.
 * Never throws: push is an extra, and the page's own notifications work without it.
 */
export async function enableWebPush(): Promise<boolean> {
  if (!webPushSupported() || typeof Notification === 'undefined' || Notification.permission !== 'granted') return false
  try {
    const keyResponse = await fetch(PUSH_KEY_PATH, { headers: { Accept: 'application/json' }, cache: 'no-store' })
    if (!keyResponse.ok) return false
    const { public_key: publicKey } = (await keyResponse.json()) as { public_key?: unknown }
    if (typeof publicKey !== 'string') return false
    const key = keyBytes(publicKey)
    const registration = await navigator.serviceWorker.register(PUSH_WORKER_URL, { scope: PUSH_WORKER_SCOPE })
    let subscription = await registration.pushManager.getSubscription()
    if (subscription && !sameKey(subscription.options.applicationServerKey, key)) {
      await subscription.unsubscribe()
      subscription = null
    }
    const subscribe = () => registration.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key })
    subscription ??= await subscribe()
    let saved = await save(subscription)
    // 409: the endpoint is another person's (they used this browser and their session ended
    // without a sign-out here). A new subscription is a new endpoint; the old one, unsubscribed,
    // answers its push service 410, and the agent forgets it.
    if (saved.status === 409) {
      await subscription.unsubscribe()
      saved = await save(await subscribe())
    }
    return saved.ok
  } catch {
    return false
  }
}

function save(subscription: PushSubscription): Promise<Response> {
  return fetch(PUSH_SUBSCRIPTION_PATH, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(subscription.toJSON()),
  })
}

/**
 * Forgets this browser's subscription, at the agent and in the browser: what sign-out does
 * first (`lib/signIn.ts` `signOut`), so the person signing out is no longer pushed to here and
 * the next person on this browser can subscribe. Never throws.
 */
export async function disableWebPush(): Promise<void> {
  if (!webPushSupported()) return
  try {
    const registration = await navigator.serviceWorker.getRegistration(PUSH_WORKER_SCOPE)
    const subscription = await registration?.pushManager.getSubscription()
    if (!subscription) return
    await fetch(`${PUSH_SUBSCRIPTION_PATH}?endpoint=${encodeURIComponent(subscription.endpoint)}`, { method: 'DELETE' }).catch(() => undefined)
    await subscription.unsubscribe()
  } catch {
    // Push is an extra: a failure here never holds up the sign-out.
  }
}

/** The service worker's "open this session" after a notification click on a tab that was already open. */
export function onPushOpen(handler: (sessionId: string | null) => void): () => void {
  if (!webPushSupported()) return () => {}
  const container = navigator.serviceWorker
  const listener = (event: MessageEvent) => {
    const data = event.data as { type?: unknown; sessionId?: unknown } | null
    if (data?.type !== 'scadbuddy.openSession') return
    handler(typeof data.sessionId === 'string' ? data.sessionId : null)
  }
  container.addEventListener('message', listener)
  container.startMessages()
  return () => container.removeEventListener('message', listener)
}

/** The query parameter the worker opens a new tab with (`?assistant-session=<id>`). */
export const SESSION_PARAM = 'assistant-session'

/** The session a notification click opened this page for, removed from the address bar; null when none. */
export function takeSessionParam(): string | null {
  const url = new URL(window.location.href)
  const id = url.searchParams.get(SESSION_PARAM)
  if (id === null) return null
  url.searchParams.delete(SESSION_PARAM)
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  return id
}

/**
 * In the shell while the assistant is on and not embedded: keeps this browser subscribed
 * (when notifications are granted) and opens the session a push notification's click names,
 * from the worker's message to an open tab or from the address of a tab it opened.
 */
export function useWebPush(enabled: boolean, onOpen: (sessionId: string | null) => void): void {
  const open = useRef(onOpen)
  useEffect(() => {
    open.current = onOpen
  }, [onOpen])
  useEffect(() => {
    if (!enabled) return
    void enableWebPush()
    const named = takeSessionParam()
    if (named !== null) open.current(named)
    return onPushOpen((id) => open.current(id))
  }, [enabled])
}
