import { useEffect, useRef, useState } from 'react'
import { useLatest } from '../../lib/useLatest'
import { loadTurnstile, type TurnstileAction } from '../turnstile'

/**
 * Cloudflare Turnstile, the storefront's bot check (#5283, spec 2026-11-02 §5.8). The
 * edge forwards the widget's token to the CRUD service, which checks it with Cloudflare
 * under a secret the page never sees (`backend/scadbuddy/storefront/turnstile.py`).
 *
 * Cloudflare's facts this rests on, read 2026-11-09:
 * - `turnstile.render(container, {sitekey, action, callback, 'error-callback'})` draws a
 *   widget (https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/).
 * - `action` is at most 32 characters of letters, digits, `_` and `-`; `expired-callback`
 *   runs when a token expires; `refresh-expired` defaults to `auto`
 *   (https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/widget-configurations/).
 * - A token is good for one check and 300 seconds
 *   (https://developers.cloudflare.com/turnstile/get-started/server-side-validation/), so a
 *   form asks for a new one after each send (`resetKey`).
 *
 * The script loads only when a widget mounts (`../turnstile.ts`); the page's CSP names
 * Cloudflare only while the check is on (`backend/scadbuddy/storefront/static.py`).
 */
interface Props {
  siteKey: string
  action: TurnstileAction
  /** The widget's token, or null when it expired or failed. */
  onToken: (token: string | null) => void
  /** A new value draws a new widget, for a new token after a send. */
  resetKey?: number
}

export function Turnstile({ siteKey, action, onToken, resetKey = 0 }: Props) {
  const container = useRef<HTMLDivElement>(null)
  const report = useLatest(onToken)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    let live = true
    let widgetId: string | undefined
    // A new widget (a new resetKey) tries the script again: drop the last failure.
    setFailed(false)
    report.current(null)
    loadTurnstile().then(
      (turnstile) => {
        if (!live || !container.current) return
        widgetId = turnstile.render(container.current, {
          sitekey: siteKey,
          action,
          callback: (token) => report.current(token),
          'expired-callback': () => report.current(null),
          // True: the error is handled here (no token), so Turnstile logs no warning of
          // its own (https://developers.cloudflare.com/turnstile/troubleshooting/client-side-errors/,
          // "Error handling").
          'error-callback': () => {
            report.current(null)
            return true
          },
        })
      },
      () => live && setFailed(true),
    )
    return () => {
      live = false
      if (widgetId !== undefined) window.turnstile?.remove(widgetId)
    }
  }, [siteKey, action, resetKey, report])

  return (
    <div className="sf-field">
      <div ref={container} data-testid="turnstile" />
      {failed && (
        <p className="sf-hint sf-hint-bad" role="alert">
          The check that you are not a robot did not load. Reload the page and try again.
        </p>
      )}
    </div>
  )
}
