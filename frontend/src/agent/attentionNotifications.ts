import { useEffect, useRef } from 'react'
import type { AttentionNotice } from './attention'

/**
 * #815 §3 — an OS notification for each attention request (a `tab_disconnected` wait among
 * them) that opens while this page is hidden, from the badge's own read (`useAttention`'s
 * `notices`). Nothing is shown while the page is visible: the badge and the panel say it there.
 * Permission is asked only from a click (`requestAttentionNotifications`,
 * `components/assistant/NotificationsOffer.tsx`), never on load. A click on a notification
 * focuses this tab and opens the request's session in the panel.
 *
 * Only an open tab can notify: a closed one would need a service worker and Web Push, which
 * this does not add. Each notification is tagged with its request id, so several ScadBuddy
 * tabs raising the same request replace one another rather than stacking.
 */

export type NotificationsState = NotificationPermission | 'unsupported'

/** What the browser says about notifications for this origin. */
export function attentionNotificationsState(): NotificationsState {
  return typeof Notification === 'undefined' ? 'unsupported' : Notification.permission
}

/** Asks for the permission; call it only from a click, which browsers require. */
export async function requestAttentionNotifications(): Promise<NotificationsState> {
  if (typeof Notification === 'undefined') return 'unsupported'
  return Notification.requestPermission()
}

/** A notification's title by reason (the agent's `harness/attention.ts` reasons). */
export function noticeTitle(reason: string): string {
  switch (reason) {
    case 'tab_disconnected':
      return 'The assistant lost its ScadBuddy tab'
    case 'question':
      return 'The assistant has a question'
    case 'blocked':
      return 'The assistant is blocked'
    default:
      return 'The assistant needs you'
  }
}

/** The most of a message a notification's body carries. */
export const NOTICE_BODY_MAX = 200

const bodyOf = (message: string) => (message.length > NOTICE_BODY_MAX ? `${message.slice(0, NOTICE_BODY_MAX - 1)}…` : message)

/**
 * Raises a notification for each notice first seen while the page is hidden, and closes it
 * once its request is no longer listed or the page is shown again. `notices` is null while
 * unknown (no read has answered), which changes nothing. The first answer is taken as
 * already seen, so a reload does not announce what was waiting before it.
 */
export function useAttentionNotifications(
  notices: readonly AttentionNotice[] | null,
  enabled: boolean,
  onOpen: (sessionId: string | null) => void,
): void {
  const seen = useRef<Set<string> | null>(null)
  const shown = useRef(new Map<string, Notification>())
  const open = useRef(onOpen)
  useEffect(() => {
    open.current = onOpen
  }, [onOpen])

  useEffect(() => {
    if (!enabled) {
      seen.current = null
      return
    }
    if (notices === null) return
    const ids = new Set(notices.map((n) => n.id))
    for (const [id, notification] of shown.current) {
      if (!ids.has(id)) {
        notification.close()
        shown.current.delete(id)
      }
    }
    const before = seen.current
    seen.current = ids
    if (before === null) return
    if (document.visibilityState !== 'hidden' || attentionNotificationsState() !== 'granted') return
    for (const notice of notices) {
      if (before.has(notice.id)) continue
      let notification: Notification
      try {
        notification = new Notification(noticeTitle(notice.reason), { body: bodyOf(notice.message), tag: notice.id })
      } catch {
        // Android's Chrome has no constructor (only a service worker's showNotification).
        return
      }
      notification.onclick = () => {
        window.focus()
        notification.close()
        open.current(notice.sessionId)
      }
      shown.current.set(notice.id, notification)
    }
  }, [notices, enabled])

  useEffect(() => {
    const live = shown.current
    const onVisible = () => {
      if (document.visibilityState !== 'visible') return
      for (const notification of live.values()) notification.close()
      live.clear()
    }
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      document.removeEventListener('visibilitychange', onVisible)
      for (const notification of live.values()) notification.close()
      live.clear()
    }
  }, [])
}
