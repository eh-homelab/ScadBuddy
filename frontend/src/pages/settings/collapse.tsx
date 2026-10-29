import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { CollapseContext, foldsAround, readCollapsed, writeCollapsed } from './collapseState'

/**
 * The Settings page's folded sections, remembered per browser. With
 * `known`, the stored list keeps only those ids, so a renamed or removed fold's id does
 * not stay in it forever (#3415). It is every fold the page can render, not those it
 * renders now: a group shown only with the agent's database keeps its fold without it.
 */
export function SettingsCollapseProvider({ children, known }: { children: ReactNode; known?: readonly string[] }) {
  const [collapsed, setCollapsed] = useState(readCollapsed)
  const knownIds = useMemo(() => (known ? new Set(known) : undefined), [known])
  const [empty, setEmptyIds] = useState<ReadonlySet<string>>(new Set())
  // Pruned once on mount too, so a browser that never toggles a fold drops stale ids
  // as well (#3415 review).
  useEffect(() => {
    if (!knownIds) return
    const stored = readCollapsed()
    const kept = new Set([...stored].filter((fold) => knownIds.has(fold)))
    if (kept.size !== stored.size) writeCollapsed(kept)
  }, [knownIds])
  const setOpen = useCallback((id: string, open: boolean) => {
    setCollapsed((current) => {
      if (current.has(id) !== open) return current
      const next = new Set(current)
      if (open) next.delete(id)
      else next.add(id)
      writeCollapsed(knownIds ? new Set([...next].filter((fold) => knownIds.has(fold))) : next)
      return next
    })
  }, [knownIds])
  const setEmpty = useCallback((id: string, isEmpty: boolean) => {
    setEmptyIds((current) => {
      if (current.has(id) === isEmpty) return current
      const next = new Set(current)
      if (isEmpty) next.add(id)
      else next.delete(id)
      return next
    })
  }, [])
  const reveal = useCallback(
    (id: string) => {
      for (const fold of foldsAround(id)) setOpen(fold, true)
    },
    [setOpen],
  )
  const value = useMemo(
    () => ({ collapsed, setOpen, empty, setEmpty, reveal }),
    [collapsed, setOpen, empty, setEmpty, reveal],
  )
  return <CollapseContext.Provider value={value}>{children}</CollapseContext.Provider>
}
