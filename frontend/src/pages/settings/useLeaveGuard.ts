import { useEffect, useState } from 'react'
import { useLocation, useNavigate } from 'react-router'

/**
 * #322 — an in-app "leave with unsaved changes?" guard. Bambuddy embeds ScadBuddy in a
 * sandboxed iframe without `allow-modals`, so neither `beforeunload` nor `confirm()` can
 * ask; instead a click on an in-app link to another page is held while `dirty`, and the
 * page shows its own dialog. Links within the page (the section nav) pass.
 */
export function useLeaveGuard(dirty: boolean) {
  const navigate = useNavigate()
  const location = useLocation()
  const [pending, setPending] = useState<string | null>(null)

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

  return {
    pending,
    stay: () => setPending(null),
    leave: () => {
      const to = pending
      setPending(null)
      if (to !== null) void navigate(to)
    },
  }
}
