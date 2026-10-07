import { useContext, useEffect, useRef, useState } from 'react'
import { createPath, UNSAFE_NavigationContext, useLocation, useNavigate, type Navigator, type To } from 'react-router'

/** Marks the history entry pushed on top of the page while it is dirty. */
const SENTINEL = 'scadbuddyLeaveGuard'
/** `pending` for a held Back: leaving means going back past the page. */
const BACK = Symbol('back')

function onSentinel(): boolean {
  const state: unknown = window.history.state
  return typeof state === 'object' && state !== null && SENTINEL in state
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
 * its own dialog. Navigations within the page (the section nav) pass. Held are:
 *
 * - clicks on in-app links, plain anchors included;
 * - every router navigation, `navigate()` calls included: the app uses a declarative
 *   `BrowserRouter`, which has no `useBlocker`, so the router's navigator is wrapped;
 * - Back: a sentinel entry is pushed on top of the page, so Back pops it without leaving
 *   the page, and the push drops any Forward entries. Each pop re-pushes it at once, so
 *   Back pressed again before the dialog is answered is held too.
 */
export function useLeaveGuard(dirty: boolean) {
  const navigate = useNavigate()
  const location = useLocation()
  const { navigator } = useContext(UNSAFE_NavigationContext)
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
      if (url.pathname === location.pathname) return
      event.preventDefault()
      event.stopPropagation()
      setPending(`${url.pathname}${url.search}${url.hash}`)
    }
    // Capture, so it runs before the router's own link handler.
    document.addEventListener('click', onClick, true)
    return () => document.removeEventListener('click', onClick, true)
  }, [dirty, location.pathname])

  useEffect(() => {
    if (!dirty) return
    return intercept(navigator, (original) => (to: To, state?: unknown, opts?: Parameters<Navigator['push']>[2]) => {
      const path = typeof to === 'string' ? to : createPath(to)
      if (bypass.current || new URL(path, window.location.href).pathname === location.pathname) {
        original.call(navigator, to, state, opts)
        return
      }
      setPending(path)
    })
  }, [dirty, navigator, location.pathname])

  useEffect(() => {
    if (!dirty) return
    if (!onSentinel()) pushSentinel()
    const onPop = () => {
      if (leavingBack.current) {
        leavingBack.current = false
        return
      }
      if (onSentinel()) return
      // Re-armed at once, not when the dialog is answered: a second Back before then
      // pops this new sentinel instead of leaving the page.
      pushSentinel()
      setPending(BACK)
    }
    window.addEventListener('popstate', onPop)
    return () => {
      window.removeEventListener('popstate', onPop)
      // Saved or discarded: take the sentinel off, so one Back leaves again.
      if (onSentinel()) window.history.back()
    }
  }, [dirty])

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
