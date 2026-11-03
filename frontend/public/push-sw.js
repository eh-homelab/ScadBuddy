/* ScadBuddy's push service worker (#815 §3): shows an attention request pushed by the agent
 * (agent/src/notify/dispatcher.ts) when no ScadBuddy tab is in view, closed tabs included.
 * Registered by src/agent/webPush.ts under the scope /push-sw/, so it controls no page and
 * never stands in the way of the app's own fetches (or msw's worker in the mocked build).
 *
 * A push carries {request_id, session_id, reason} and nothing the conversation said, so the
 * notification names the reason only; a click opens ScadBuddy at that request's session.
 * Titles mirror src/agent/attentionNotifications.ts `noticeTitle`. */

const TITLES = {
  tab_disconnected: 'The assistant lost its ScadBuddy tab',
  question: 'The assistant has a question',
  blocked: 'The assistant is blocked',
  approval: 'The assistant needs your approval',
}
/** What to do, by reason (the agent's notify/dispatcher.ts `noticeBody`). */
const BODIES = { approval: 'Open ScadBuddy to decide it.' }

self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))

self.addEventListener('push', (event) => {
  let data = {}
  try {
    data = event.data ? event.data.json() : {}
  } catch {
    data = {}
  }
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      // A tab in view shows it in the badge and the panel, and raises its own while hidden.
      if (windows.some((w) => w.visibilityState === 'visible')) return
      const id = typeof data.request_id === 'string' ? data.request_id : undefined
      await self.registration.showNotification(TITLES[data.reason] || 'The assistant needs you', {
        body: BODIES[data.reason] || 'Open ScadBuddy to answer it.',
        icon: '/logo-512.png',
        // One per request: a page's own notification for it (same tag) replaces this, and the reverse.
        ...(id ? { tag: id } : {}),
        data: { sessionId: typeof data.session_id === 'string' ? data.session_id : null },
      })
    })(),
  )
})

self.addEventListener('notificationclick', (event) => {
  event.notification.close()
  const sessionId = event.notification.data && event.notification.data.sessionId
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true })
      const tab = windows.find((w) => new URL(w.url).origin === self.location.origin)
      if (tab) {
        await tab.focus()
        tab.postMessage({ type: 'scadbuddy.openSession', sessionId })
        return
      }
      const url = new URL('/', self.location.origin)
      if (sessionId) url.searchParams.set('assistant-session', sessionId)
      await self.clients.openWindow(url.href)
    })(),
  )
})
