import { useCallback, useState } from 'react'

/** #6885 — Advanced's sections, in the order the dialog shows them. */
export type SectionId = 'nozzles' | 'quality' | 'tower' | 'filing' | 'slicer'

export const SECTION_TITLES: Record<SectionId, string> = {
  nozzles: 'Nozzles',
  quality: 'Quality and plate',
  tower: 'Supports, prime tower and print sequence',
  filing: 'Filing and copies',
  slicer: 'Slicer settings',
}

/** Where each viewer's open and closed sections are kept: this browser only. */
export const SECTIONS_KEY = 'scadbuddy.print.sections'

function readClosed(): ReadonlySet<SectionId> {
  try {
    const stored: unknown = JSON.parse(window.localStorage.getItem(SECTIONS_KEY) ?? '{}')
    if (stored === null || typeof stored !== 'object') return new Set()
    return new Set(
      Object.entries(stored)
        .filter(([id, open]) => id in SECTION_TITLES && open === false)
        .map(([id]) => id as SectionId),
    )
  } catch {
    return new Set()
  }
}

function writeClosed(closed: ReadonlySet<SectionId>) {
  try {
    const stored = Object.fromEntries((Object.keys(SECTION_TITLES) as SectionId[]).map((id) => [id, !closed.has(id)]))
    window.localStorage.setItem(SECTIONS_KEY, JSON.stringify(stored))
  } catch {
    // Blocked storage: the sections still open and close, for this visit only.
  }
}

/**
 * #6885 — which of Advanced's sections are open. Every section opens until the viewer
 * closes it; the choice is kept in this browser's storage, so the next print opens the
 * same way. A storage that cannot be read keeps every section open.
 */
export function usePrintSections() {
  const [closed, setClosed] = useState(readClosed)
  const set = useCallback((id: SectionId, open: boolean) => {
    setClosed((current) => {
      if (current.has(id) === !open) return current
      const next = new Set(current)
      if (open) next.delete(id)
      else next.add(id)
      writeClosed(next)
      return next
    })
  }, [])
  const isOpen = useCallback((id: SectionId) => !closed.has(id), [closed])
  return { isOpen, set }
}
