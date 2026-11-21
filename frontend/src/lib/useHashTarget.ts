import { useEffect, useRef } from 'react'
import { useLocation } from 'react-router'

/** The element id the address's hash names, or null (a malformed escape names none). */
function hashId(hash: string): string | null {
  if (hash.length < 2) return null
  try {
    return decodeURIComponent(hash.slice(1))
  } catch {
    return null
  }
}

/**
 * #6869 — a row a link names by anchor (`filamentLinks` `invoiceLine`, `supplierProduct`):
 * scrolled into view once, when `ready` (its rows have rendered). Returns the named id, so the
 * row can mark itself.
 */
export function useHashTarget(ready: boolean): string | null {
  const { hash } = useLocation()
  const id = hashId(hash)
  const scrolled = useRef<string | null>(null)
  useEffect(() => {
    if (!ready || !id || scrolled.current === id) return
    const row = document.getElementById(id)
    if (!row) return
    scrolled.current = id
    row.scrollIntoView?.({ block: 'center' })
  }, [ready, id])
  return id
}
