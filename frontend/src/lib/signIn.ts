import { useEffect, useSyncExternalStore } from 'react'
import { api } from '../api/client'
import type { AuthSessionView } from '../api/types'
import { isEmbedded, openExternal } from './embed'
import { clearSignedOut, noteSignedOut, resetSignedOut, setEnforced } from './signedOut'

// #4975 — signing in (spec 2026-11-01 §5.1, §6.2). In a top-level tab the page navigates to
// the login; inside Bambuddy's frame it opens a popup, which the sandbox lets escape, and
// waits for the callback page's message. One store for the page: the header and Settings
// read the same session.

export const LOGIN_PATH = '/api/v1/auth/login'
export const SIGNED_IN = 'scadbuddy:signed-in'
export const SIGN_IN_FAILED = 'scadbuddy:sign-in-failed'
/** How often the frame asks again while the popup is open, in case its message is lost. */
export const POPUP_POLL_MS = 1000

export class SignInBlockedError extends Error {
  constructor() {
    super('The browser blocked the sign-in window. Allow pop-ups for ScadBuddy and try again.')
    this.name = 'SignInBlockedError'
  }
}

/**
 * Spec 2026-11-01 §6.3: the popup signed in, yet this frame still is not, because it sits
 * on another site than its top-level page and the browser sends the `SameSite` cookie
 * only to a same-site frame. The way out is ScadBuddy in a tab of its own.
 */
export class CrossSiteFrameError extends Error {
  constructor() {
    super(
      'Signed in, but this page is embedded from another site, so the browser does not send the sign-in here. Open ScadBuddy in its own tab.',
    )
    this.name = 'CrossSiteFrameError'
  }
}

let current: AuthSessionView | null = null
let everSignedIn = false
const listeners = new Set<() => void>()

function publish(next: AuthSessionView | null) {
  current = next
  // #4975 phase 8: what waited for a sign-in goes on; in `enforce`, signed out is refused.
  if (next) setEnforced(next.mode === 'enforce')
  if (next?.signed_in) {
    everSignedIn = true
    clearSignedOut()
  } else if (next?.mode === 'enforce') {
    noteSignedOut()
  }
  for (const listener of listeners) listener()
}

/** Whether this page has been signed in since it loaded: a sign-out then may cost unsaved work. */
export function wasSignedIn(): boolean {
  return everSignedIn
}

/** Read the session again; the store keeps the last answer when the read fails. */
export async function refreshAuthSession(): Promise<AuthSessionView | null> {
  try {
    publish(await api.getAuthSession())
  } catch {
    // An older backend, or one not answering: nothing to show, nothing to change.
  }
  return current
}

/** For tests: forget the page's session. */
export function resetAuthSession() {
  current = null
  everSignedIn = false
  listeners.clear()
  resetSignedOut()
}

export function useAuthSession(): AuthSessionView | null {
  const session = useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
  )
  useEffect(() => {
    if (current === null) void refreshAuthSession()
  }, [])
  return session
}

function loginUrl(returnTo: string, popup: boolean): string {
  const query = new URLSearchParams({ return_to: returnTo })
  if (popup) query.set('popup', 'true')
  return `${LOGIN_PATH}?${query.toString()}`
}

/**
 * Where to come back to (spec 2026-11-01 §5.1 step 1): a same-origin path, a single `/`
 * followed by neither `/` nor `\\`, with no scheme or authority once percent-decoded;
 * anything else is `/`. The backend applies the same rule; this keeps the URL honest.
 */
export function returnTo(path: string): string {
  // As the backend's `auth/login_state.py` `safe_return_to`: decoded up to three times.
  let decoded = path
  try {
    for (let i = 0; i < 3; i += 1) decoded = decodeURIComponent(decoded)
  } catch {
    return '/'
  }
  if (!path.startsWith('/') || /[\r\n\t\0]/.test(path)) return '/'
  for (const candidate of [path, decoded]) {
    if (!candidate.startsWith('/') || candidate[1] === '/' || candidate.includes('\\')) return '/'
  }
  return path
}

function here(): string {
  return returnTo(`${window.location.pathname}${window.location.search}${window.location.hash}`)
}

/**
 * Sign in. Called from the click handler itself: the popup is opened before any `await`,
 * while the click still allows it (as `lib/embed.ts`'s download popup is). Resolves with
 * the session once signed in from a frame; a top-level tab navigates away instead.
 */
export function signIn(embedded = isEmbedded()): Promise<AuthSessionView | null> {
  if (!embedded) {
    window.location.assign(loginUrl(here(), false))
    return Promise.resolve(null)
  }
  const popup = window.open(loginUrl('/', true), 'scadbuddy-sign-in', 'popup,width=520,height=680')
  if (!popup) return Promise.reject(new SignInBlockedError())
  return new Promise((resolve, reject) => {
    let done = false
    const finish = (error?: Error, signedInThere = false) => {
      if (done) return
      done = true
      window.removeEventListener('message', onMessage)
      window.clearInterval(timer)
      if (error) {
        reject(error)
        return
      }
      void refreshAuthSession().then((session) => {
        // The callback said signed in; a frame that still reads signed out is cross-site.
        if (signedInThere && session && !session.signed_in) reject(new CrossSiteFrameError())
        else resolve(session)
      }, reject)
    }
    const onMessage = (event: MessageEvent) => {
      // Only the callback page, at this origin, in the window this click opened (§6.2).
      if (event.origin !== window.location.origin || event.source !== popup) return
      const data = event.data as { type?: unknown; detail?: unknown } | null
      if (data?.type === SIGNED_IN) finish(undefined, true)
      else if (data?.type === SIGN_IN_FAILED)
        finish(new Error(typeof data.detail === 'string' ? data.detail : 'Sign-in failed.'))
    }
    window.addEventListener('message', onMessage)
    // The message can be lost (the opener was replaced): ask while it is open, and once closed.
    const timer = window.setInterval(() => {
      void refreshAuthSession().then((session) => {
        if (session?.signed_in || popup.closed) finish()
      })
    }, POPUP_POLL_MS)
  })
}

/**
 * Sign out; with RP-initiated logout on (D7) the IdP's own sign-out follows, in this tab
 * or, from a frame, in a new one (an IdP's page commonly refuses to be framed).
 */
export async function signOut(): Promise<void> {
  const { end_session_url: next } = await api.logout()
  await refreshAuthSession()
  if (next) openExternal(next)
}
