import { useEffect, useState } from 'react'
import { flushSync } from 'react-dom'

/**
 * #2173 — Settings sections and the Assistant's groups fold from their headings. What is
 * folded is remembered per browser; a nav or `#id` jump unfolds what it lands in.
 */

const COLLAPSED_KEY = 'scadbuddy.settings.collapsed'
const REVEAL = 'scadbuddy:settings-reveal'

function readCollapsed(): Set<string> {
  try {
    const raw: unknown = JSON.parse(window.localStorage.getItem(COLLAPSED_KEY) ?? '[]')
    return new Set(Array.isArray(raw) ? raw.filter((id): id is string => typeof id === 'string') : [])
  } catch {
    return new Set()
  }
}

function writeCollapsed(id: string, collapsed: boolean) {
  const ids = readCollapsed()
  if (collapsed) ids.add(id)
  else ids.delete(id)
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...ids]))
  } catch {
    // Storage refused: folding still works, just not across reloads.
  }
}

/** Whether `id` is unfolded, and a setter that remembers it; unfolds itself when revealed. */
export function useUnfolded(id: string): [boolean, (open: boolean) => void] {
  const [open, setOpen] = useState(() => !readCollapsed().has(id))
  useEffect(() => {
    const onReveal = (event: Event) => {
      if ((event as CustomEvent<string[]>).detail.includes(id)) {
        setOpen(true)
        writeCollapsed(id, false)
      }
    }
    window.addEventListener(REVEAL, onReveal)
    return () => window.removeEventListener(REVEAL, onReveal)
  }, [id])
  return [
    open,
    (next: boolean) => {
      setOpen(next)
      writeCollapsed(id, !next)
    },
  ]
}

/**
 * Unfolds the element `id` names and every foldable around it, then scrolls to it.
 * Synchronous, so the scroll lands on laid-out content.
 */
export function revealSetting(id: string, scroll: ScrollIntoViewOptions = { block: 'start' }) {
  const target = document.getElementById(id)
  if (!target) return
  const ids: string[] = []
  for (let at: Element | null = target; at; at = at.parentElement) {
    if (at instanceof HTMLElement && at.dataset.foldable !== undefined) ids.push(at.id)
  }
  if (ids.length > 0) flushSync(() => window.dispatchEvent(new CustomEvent(REVEAL, { detail: ids })))
  target.scrollIntoView?.(scroll)
}
