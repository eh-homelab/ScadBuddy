import { createContext, useCallback, useContext, useEffect, useLayoutEffect, useState, useSyncExternalStore, type ReactNode } from 'react'
import type { FieldName } from './fields'

type Drafts = Partial<Record<FieldName, string>>
type Report = { dirty: boolean; drafts: Drafts }

/**
 * #2171 — what every Settings section reports: whether it holds unsaved edits, and the
 * drafts it publishes for another section to read. One registry for every area, so the leave guard and
 * "changed elsewhere" see every section, mounted together or not.
 */
class DirtyRegistry {
  /** Each mounted reporter, in the order it first reported. */
  private reports = new Map<string, Report>()
  private listeners = new Set<() => void>()
  private dirtyIds: readonly string[] = []
  /** #7192 — what holds a leave without being a settings form, by holder: its words for the dialog. */
  private holds = new Map<string, string>()
  private heldLabels: readonly string[] = []

  subscribe = (listener: () => void) => {
    this.listeners.add(listener)
    return () => {
      this.listeners.delete(listener)
    }
  }

  set(id: string, report: Report) {
    this.reports.set(id, report)
    this.changed()
  }

  remove(id: string) {
    if (this.reports.delete(id)) this.changed()
  }

  dirtySections = (): readonly string[] => this.dirtyIds

  /** `label` holds a leave for `id`; null lets it go. Never a dirty section: no reload or agent reads it. */
  hold(id: string, label: string | null) {
    if (label === null ? !this.holds.delete(id) : this.holds.get(id) === label) return
    if (label !== null) this.holds.set(id, label)
    this.heldLabels = [...this.holds.values()]
    for (const listener of this.listeners) listener()
  }

  leaveHolds = (): readonly string[] => this.heldLabels

  anyDirty = (): boolean => this.dirtyIds.length > 0

  draft(name: FieldName): string | undefined {
    for (const report of this.reports.values()) {
      const value = report.drafts[name]
      if (value !== undefined) return value
    }
    return undefined
  }

  private changed() {
    const ids = [...this.reports].filter(([, report]) => report.dirty).map(([id]) => id)
    // The same array while the list is the same, so a reader re-renders only on a change.
    if (ids.length !== this.dirtyIds.length || ids.some((id, index) => id !== this.dirtyIds[index])) this.dirtyIds = ids
    for (const listener of this.listeners) listener()
  }
}

const DirtyContext = createContext<DirtyRegistry | null>(null)

export function SettingsDirtyProvider({ children }: { children: ReactNode }) {
  const [registry] = useState(() => new DirtyRegistry())
  return <DirtyContext.Provider value={registry}>{children}</DirtyContext.Provider>
}

function useRegistry(): DirtyRegistry {
  const registry = useContext(DirtyContext)
  if (!registry) throw new Error('Settings dirty state outside SettingsDirtyProvider')
  return registry
}

/** Reports section `id`'s unsaved state and published drafts; cleared on unmount. */
export function useReportDirty(id: string, dirty: boolean, drafts?: Drafts): void {
  const registry = useRegistry()
  const published = JSON.stringify(drafts ?? {})
  // In the commit itself: a settings event or an agent call read right after an edit sees it.
  useLayoutEffect(() => {
    registry.set(id, { dirty, drafts: JSON.parse(published) as Drafts })
  }, [registry, id, dirty, published])
  useEffect(() => () => registry.remove(id), [registry, id])
}

const NO_HOLDS: readonly string[] = []
const NO_SUBSCRIPTION = () => () => {}

/**
 * #7192 — holds the Settings leave guard while `label` is set (unsaved work that is not a
 * settings form, such as a hosted plugin's draft files), named in its dialog by `label`. It
 * is not a dirty section: the Settings reload and the agent's "unsaved" flag never see it.
 * Outside Settings (a component rendered on its own) it does nothing.
 */
export function useHoldLeave(id: string, label: string | null): void {
  const registry = useContext(DirtyContext)
  useLayoutEffect(() => {
    registry?.hold(id, label)
  }, [registry, id, label])
  useEffect(() => () => registry?.hold(id, null), [registry, id])
}

/** What holds a leave now (`useHoldLeave`), each by its label. */
export function useLeaveHolds(): readonly string[] {
  const registry = useContext(DirtyContext)
  return useSyncExternalStore(registry?.subscribe ?? NO_SUBSCRIPTION, registry?.leaveHolds ?? (() => NO_HOLDS))
}

/** The ids of the sections dirty now, in the order they first reported. */
export function useDirtySections(): readonly string[] {
  const registry = useRegistry()
  return useSyncExternalStore(registry.subscribe, registry.dirtySections)
}

/** Whether any section is dirty, read when called: for handlers and async callbacks. */
export function useAnyDirty(): () => boolean {
  return useRegistry().anyDirty
}

/** Another section's published draft of `name`, or undefined when none publishes it. */
export function usePeerDraft(name: FieldName): string | undefined {
  const registry = useRegistry()
  const read = useCallback(() => registry.draft(name), [registry, name])
  return useSyncExternalStore(registry.subscribe, read)
}
