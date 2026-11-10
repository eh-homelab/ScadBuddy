import { useEffect, useState } from 'react'
import { store } from './api/client'

/**
 * Cloudflare Turnstile's script and the shop's site key (#5283, spec 2026-11-02 §5.8);
 * the widget is `components/Turnstile.tsx`.
 *
 * Explicit rendering loads `https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit`
 * and then calls `turnstile.render(container, options)`; `turnstile.remove(id)` takes a
 * widget away (https://developers.cloudflare.com/turnstile/get-started/client-side-rendering/,
 * read 2026-11-09). The script is added only when a widget mounts, and once per page.
 */

export const TURNSTILE_SCRIPT = 'https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit'

/** The forms that take the check, each the widget's `action` (backend `storefront/wire.py`). */
export type TurnstileAction = 'submit' | 'request' | 'sign_up' | 'sign_in'

export interface TurnstileOptions {
  sitekey: string
  action: TurnstileAction
  callback: (token: string) => void
  'expired-callback': () => void
  'error-callback': () => void
}

export interface TurnstileApi {
  render: (container: HTMLElement, options: TurnstileOptions) => string | undefined
  remove: (widgetId: string) => void
}

declare global {
  interface Window {
    turnstile?: TurnstileApi
  }
}

let loading: Promise<TurnstileApi> | null = null

/** Turnstile's script, added to the page on the first call and never again. */
export function loadTurnstile(): Promise<TurnstileApi> {
  if (window.turnstile) return Promise.resolve(window.turnstile)
  loading ??= new Promise<TurnstileApi>((resolve, reject) => {
    const script = document.createElement('script')
    script.src = TURNSTILE_SCRIPT
    script.async = true
    script.onload = () => (window.turnstile ? resolve(window.turnstile) : reject(new Error('no turnstile')))
    script.onerror = () => {
      loading = null
      script.remove()
      reject(new Error('turnstile did not load'))
    }
    document.head.appendChild(script)
  })
  return loading
}

let siteKeyRead: Promise<string | null> | null = null

/**
 * The shop's Turnstile site key: a string while the check is on, null while it is off,
 * undefined until read. Read once per page. A failed read counts as off for now (the edge
 * still refuses a form that needs a token) and is read again when `retryKey` changes:
 * a form passes the key it bumps after each send, so a send refused for its missing token
 * reads the key again and shows the widget.
 */
export function useTurnstileSiteKey(retryKey: number = 0): string | null | undefined {
  const [siteKey, setSiteKey] = useState<string | null | undefined>(undefined)
  useEffect(() => {
    let live = true
    siteKeyRead ??= store.turnstile().then(
      (answer) => answer.site_key ?? null,
      () => {
        siteKeyRead = null
        return null
      },
    )
    void siteKeyRead.then((key) => live && setSiteKey(key))
    return () => {
      live = false
    }
  }, [retryKey])
  return siteKey
}

/** For tests: forget the loaded script and the site key read. */
export function resetTurnstileForTests(): void {
  loading = null
  siteKeyRead = null
  delete window.turnstile
}
