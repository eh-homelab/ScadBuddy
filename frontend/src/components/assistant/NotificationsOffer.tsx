import { useState } from 'react'
import { attentionNotificationsState, requestAttentionNotifications } from '../../agent/attentionNotifications'
import { Button } from '@/components/ui/button'

/** Where "Not now" is remembered, per browser. */
export const NOTIFICATIONS_DISMISSED_KEY = 'scadbuddy.attentionNotifications.dismissed'

function dismissed(): boolean {
  try {
    return window.localStorage.getItem(NOTIFICATIONS_DISMISSED_KEY) === 'true'
  } catch {
    return false
  }
}

/**
 * #815 — offers OS notifications for attention requests while the browser has not been asked
 * yet. The permission prompt opens only from "Turn on", a click, as browsers require; once
 * answered (or "Not now"), the offer is gone.
 */
export function NotificationsOffer({ className }: { className?: string }) {
  const [state, setState] = useState(attentionNotificationsState)
  const [hidden, setHidden] = useState(dismissed)
  if (state !== 'default' || hidden) return null
  const turnOn = () => {
    void requestAttentionNotifications().then(setState, () => setState(attentionNotificationsState()))
  }
  const notNow = () => {
    setHidden(true)
    try {
      window.localStorage.setItem(NOTIFICATIONS_DISMISSED_KEY, 'true')
    } catch {
      // Remembered for this page only.
    }
  }
  return (
    <div role="region" aria-label="Notifications" className={className}>
      <p className="text-[12.5px] text-muted-foreground">
        Get a notification when the assistant needs you while this tab is in the background.
      </p>
      <div className="mt-1 flex gap-2">
        {/* Human-only: the browser bridge (#254) must never let an agent press these. */}
        <Button size="sm" data-agent-user-only="" onClick={turnOn}>
          Turn on
        </Button>
        <Button size="sm" variant="ghost" data-agent-user-only="" onClick={notNow}>
          Not now
        </Button>
      </div>
    </div>
  )
}
