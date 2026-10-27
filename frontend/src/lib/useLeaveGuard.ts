import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { useLatest } from './useLatest'
import {
  createPath,
  UNSAFE_NavigationContext,
  useLocation,
  useNavigate,
  useNavigationType,
  type Navigator,
  type To,
} from 'react-router'

/** Marks the history entry pushed on top of the page while it is dirty. */
const SENTINEL = 'scadbuddyLeaveGuard'
/** `pending` for a held Back: leaving means going back past the page. */
const BACK = Symbol('back')

function onSentinel(): boolean {
  const state: unknown = window.history.state
  return typeof state === 'object' && state !== null && SENTINEL in state
}

/** The router's key of the current entry (`default` for one it never wrote). */
function entryKey(): string {
  const state: unknown = window.history.state
  const key = typeof state === 'object' && state !== null ? (state as { key?: unknown }).key : undefined
  return typeof key === 'string' ? key : 'default'
}

/** A copy of the current entry (the router's own state kept), marked as the sentinel. */
function pushSentinel() {
  const state: unknown = window.history.state
  window.history.pushState({ ...(typeof state === 'object' ? state : {}), [SENTINEL]: true }, '')
}

/**
 * Routes the router's `push` and `replace` through `hold` until the returned function
 * restores them. The history object is the router's own, shared by every `navigate()`
 * and `<Link>`; wrapping it in place is the only hook a declarative router offers.
 */
function intercept(navigator: Navigator, hold: (original: Navigator['push']) => Navigator['push']): () => void {
  const { push, replace } = navigator
  navigator.push = hold(push)
  navigator.replace = hold(replace)
  return () => {
    navigator.push = push
    navigator.replace = replace
  }
}

/**
 * #322 — an in-app "leave with unsaved changes?" guard. Bambuddy embeds ScadBuddy in a
 * sandboxed iframe without `allow-modals`, so neither `beforeunload` nor `confirm()` can
 * ask; instead, while `dirty`, a navigation to another page is held and the page shows
 * its own dialog. Navigations within the page (the section nav) pass, and with `within`
 * so does every path under that prefix. Held are:
 *
 * - clicks on in-app links, plain anchors included;
 * - every router navigation, `navigate()` calls included: the app uses a declarative
 *   `BrowserRouter`, which has no `useBlocker`, so the router's navigator is wrapped;
 * - Back: a sentinel entry is pushed on top of the page, so Back pops it without leaving
 *   the page, and the push drops any Forward entries. Each pop re-pushes it at once, so
 *   Back pressed again before the dialog is answered is held too. With `within`, a Back
 *   to an entry under that prefix passes, and the sentinel is pushed again where it lands.
 */
export function useLeaveGuard(dirty: boolean, options?: { within?: string }) {
  const navigate = useNavigate()
  const location = useLocation()
  const { navigator } = useContext(UNSAFE_NavigationContext)
  const within = options?.within
  /** The page's own path, or with `within` any path under that prefix. */
  const inside = (pathname: string) =>
    pathname === location.pathname || (within !== undefined && (pathname === within || pathname.startsWith(`${within}/`)))
  const [pending, setPending] = useState<string | typeof BACK | null>(null)
  const bypass = useRef(false)
  /** Set while a confirmed Back leaves, so its own `popstate` is not held again. */
  const leavingBack = useRef(false)

  useEffect(() => {
    if (!dirty) return
    const onClick = (event: MouseEvent) => {
      if (event.defaultPrevented || event.button !== 0) return
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      const anchor = (event.target as Element | null)?.closest?.('a[href]')
      if (!(anchor instanceof HTMLAnchorElement)) return
      if (anchor.target && anchor.target !== '_self') return
      if (anchor.hasAttribute('download')) return
      const url = new URL(anchor.href, window.location.href)
      if (url.origin !== window.location.origin) return
      if (inside(url.pathname)) return
      event.preventDefault()
      event.stopPropagation()
      setPending(`${url.pathname}${url.search}${url.hash}`)
    }
    // Capture, so it runs before the router's own link handler.
    document.addEventListener('click', onClick, true)
    return () => document.removeEventListener('click', onClick, true)
  }, [dirty, location.pathname, within])

  useEffect(() => {
    if (!dirty) return
    return intercept(navigator, (original) => (to: To, state?: unknown, opts?: Parameters<Navigator['push']>[2]) => {
      const path = typeof to === 'string' ? to : createPath(to)
      if (bypass.current || inside(new URL(path, window.location.href).pathname)) {
        original.call(navigator, to, state, opts)
        return
      }
      setPending(path)
    })
  }, [dirty, navigator, location.pathname, within])

  // With `within`: each entry seen here, and the one it was pushed over, so a Back from
  // the sentinel knows whether the entry beneath the page's is still under the prefix.
  const navigationType = useNavigationType()
  const entries = useRef(new Map<string, { pathname: string; before: string | null }>())
  const shownKey = useRef<string | null>(null)
  useEffect(() => {
    if (within === undefined) return
    const previous = shownKey.current
    shownKey.current = location.key
    if (entries.current.has(location.key)) return
    // A replaced entry keeps the one it was pushed over; one reached by Back or a reload
    // before this page mounted has none known.
    let before: string | null = null
    if (navigationType === 'PUSH') before = previous
    else if (navigationType === 'REPLACE' && previous) before = entries.current.get(previous)?.before ?? null
    entries.current.set(location.key, { pathname: location.pathname, before })
  }, [within, location.key, location.pathname, navigationType])
  /** The entries a sentinel was pushed over. */
  const armed = useRef(new Set<string>())
  /** Set while a Back of our own steps past the page's entry to the one under `within` before it. */
  const stepping = useRef(false)

  // Saved or discarded: the sentinel comes off with a `history.back()` of our own, whose
  // popstate comes a task later. Typing again in between makes the page dirty before it
  // lands (#3671), so it must not be taken for the user's Back: the listener stays for
  // the page's life and knows that pop by `ownBack`.
  const dirtyNow = useLatest(dirty)
  const ownBack = useRef(false)
  /** Pushes the sentinel over the current entry, which is then known to lie beneath one. */
  const arm = useCallback(() => {
    armed.current.add(entryKey())
    pushSentinel()
  }, [])
  useEffect(() => {
    const under = (pathname: string) =>
      within !== undefined && (pathname === within || pathname.startsWith(`${within}/`))
    const onPop = () => {
      if (ownBack.current) {
        ownBack.current = false
        armed.current.delete(entryKey())
        if (dirtyNow.current && !onSentinel()) arm()
        return
      }
      const step = stepping.current
      stepping.current = false
      if (!dirtyNow.current) return
      if (leavingBack.current) {
        leavingBack.current = false
        return
      }
      if (onSentinel()) return
      if (within !== undefined) {
        const key = entryKey()
        if (!step && armed.current.has(key)) {
          // Back from the sentinel onto the page's own entry: the user means the one
          // before it. Under the prefix, go on to it without asking.
          const before = entries.current.get(key)?.before
          const previous = before ? entries.current.get(before) : undefined
          if (previous && under(previous.pathname)) {
            stepping.current = true
            window.history.back()
            return
          }
        } else if (under(window.location.pathname)) {
          arm()
          return
        }
      }
      // Re-armed at once, not when the dialog is answered: a second Back before then
      // pops this new sentinel instead of leaving the page.
      arm()
      setPending(BACK)
    }
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [dirtyNow, within, arm])

  useEffect(() => {
    if (!dirty) return
    if (!onSentinel()) arm()
    return () => {
      // Saved or discarded: take the sentinel off, so one Back leaves again.
      if (onSentinel()) {
        ownBack.current = true
        window.history.back()
      }
    }
  }, [dirty, arm])

  const go = (to: string) => {
    bypass.current = true
    try {
      // Replacing the sentinel, so Back from `to` comes to this page, not to it.
      void navigate(to, { replace: onSentinel() })
    } finally {
      bypass.current = false
    }
  }

  return {
    pending,
    stay: () => setPending(null),
    leave: () => {
      const to = pending
      setPending(null)
      if (to === BACK) {
        // Past the re-armed sentinel and the page's own entry.
        leavingBack.current = true
        window.history.go(-2)
        return
      }
      if (to === null) return
      go(to)
    },
    /**
     * #997 — leaves for `to` without asking: for a page that has just saved, whose
     * `dirty` has not caught up yet in this render.
     */
    leaveTo: go,
  }
}
