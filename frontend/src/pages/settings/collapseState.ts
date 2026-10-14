import { createContext, useContext } from 'react'

/** Where each browser keeps the Settings sections it folded away (#2173). */
export const COLLAPSED_KEY = 'scadbuddy.settings.collapsed'

export function readCollapsed(): Set<string> {
  try {
    const raw = window.localStorage.getItem(COLLAPSED_KEY)
    const parsed: unknown = raw ? JSON.parse(raw) : []
    return new Set(Array.isArray(parsed) ? parsed.filter((id): id is string => typeof id === 'string') : [])
  } catch {
    return new Set()
  }
}

export function writeCollapsed(collapsed: Set<string>): void {
  try {
    window.localStorage.setItem(COLLAPSED_KEY, JSON.stringify([...collapsed]))
  } catch {
    // Private mode or blocked storage: folding still works, for this visit.
  }
}

export type Collapse = {
  collapsed: ReadonlySet<string>
  setOpen: (id: string, open: boolean) => void
  empty: ReadonlySet<string>
  setEmpty: (id: string, empty: boolean) => void
  /** Unfolds `id` and the folds around it. */
  reveal: (id: string) => void
}

export const CollapseContext = createContext<Collapse | null>(null)

/** Whether `id` is unfolded, and the way to fold or unfold it. Open outside the provider. */
export function useCollapsible(id: string): { open: boolean; toggle: () => void } {
  const context = useContext(CollapseContext)
  const open = !context?.collapsed.has(id)
  return { open, toggle: () => context?.setOpen(id, !open) }
}

/**
 * Unfolds the element `id` and every folded section around it: what the section nav, a
 * `#section` address and the Assistant's contents do first (#2173, #2769). Scrolling to
 * it is the section nav's (`useSectionNav`, #2345: the pane only). Stable, so an effect
 * that reveals runs once.
 */
export function useReveal(): (id: string) => void {
  return useContext(CollapseContext)?.reveal ?? unfoldNothing
}

function unfoldNothing(): void {}

/** The folds around `id`, itself included when it folds, innermost first. */
export function foldsAround(id: string): string[] {
  const folds: string[] = []
  let fold = document.getElementById(id)?.closest('[data-collapsible]') ?? null
  while (fold) {
    folds.push(fold.id)
    fold = fold.parentElement?.closest('[data-collapsible]') ?? null
  }
  return folds
}

