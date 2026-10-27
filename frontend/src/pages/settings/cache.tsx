import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from 'react'
import { api } from '../../api/client'
import type { Settings } from '../../api/types'
import { setBambuddyLinks } from '../../lib/bambuddyLinks'
import { useSubscription } from '../../lib/realtime'
import { useAsync } from '../../lib/useAsync'
import { useAnyDirty } from './dirty'
import type { FieldName } from './fields'
import type { Edits } from './seed'

/**
 * A section's form, as the cache seeds it: `begin` takes its edit counts before a request,
 * `seed` gives it the named fields of the next settings, keeping any typed into since.
 */
export type Seeder = { begin: () => Edits; seed: (names: 'all' | readonly FieldName[], since: Edits) => void }

export type SettingsCache = {
  settings: Settings | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
  /** Adds a section's seeder; the function returned removes it. */
  register: (seeder: Seeder) => () => void
  /** Runs a settings write: shows its answer and seeds every seeder with `names` of it. */
  write: (
    names: 'all' | readonly FieldName[] | ((next: Settings) => readonly FieldName[]),
    request: () => Promise<Settings>,
  ) => Promise<Settings>
  /** Runs work that writes the settings: their events meanwhile are this tab's own. */
  own: <T>(work: () => Promise<T>) => Promise<T>
  /** The settings changed elsewhere while a section held unsaved edits. */
  changedElsewhere: boolean
  loadLatest: () => void
  keepMine: () => void
}

const CacheContext = createContext<SettingsCache | null>(null)

/**
 * #2171 — the one `GET /settings` every Settings area reads, and how it follows changes
 * made elsewhere (#269): an untouched page follows them; while any section holds unsaved
 * edits the page is never overwritten, and says so. Inside {@link SettingsDirtyProvider}.
 */
export function SettingsCacheProvider({ children }: { children: ReactNode }) {
  const state = useAsync(() => api.getSettings(), [])
  const { setData, refresh } = state
  const anyDirty = useAnyDirty()
  const seeders = useRef(new Set<Seeder>())
  /** How many writes of this tab's own are in flight. */
  const owning = useRef(0)
  const [changedElsewhere, setChangedElsewhere] = useState(false)

  const register = useCallback((seeder: Seeder) => {
    seeders.current.add(seeder)
    return () => {
      seeders.current.delete(seeder)
    }
  }, [])

  /** Every seeder with its edit counts taken now. */
  const begin = useCallback(() => [...seeders.current].map((seeder) => [seeder, seeder.begin()] as const), [])
  const seedAll = useCallback(
    (begun = begin()) => {
      // A section that unmounted meanwhile has nothing left to seed.
      for (const [seeder, since] of begun) if (seeders.current.has(seeder)) seeder.seed('all', since)
    },
    [begin],
  )

  const own = useCallback(async <T,>(work: () => Promise<T>): Promise<T> => {
    owning.current++
    try {
      return await work()
    } finally {
      owning.current--
    }
  }, [])

  const write = useCallback<SettingsCache['write']>(
    (names, request) =>
      own(async () => {
        const begun = begin()
        const next = await request()
        setData(next)
        setBambuddyLinks(next)
        const seeded = typeof names === 'function' ? names(next) : names
        // A section that unmounted meanwhile has nothing left to seed.
        for (const [seeder, since] of begun) if (seeders.current.has(seeder)) seeder.seed(seeded, since)
        return next
      }),
    [own, begin, setData],
  )

  useSubscription('settings', (signal) => {
    // Saves, resets and Test connection write the settings: that event is this tab's own.
    if (signal === 'resync' || owning.current > 0) return
    if (anyDirty()) {
      setChangedElsewhere(true)
      return
    }
    // The edit counts are taken now, before the read is sent (#767): a field typed into
    // meanwhile keeps its draft even before its section reports dirty. Whether to follow
    // is decided when the answer lands, not when the event came: typing may have started since.
    const begun = begin()
    refresh(() => {
      if (!anyDirty()) {
        seedAll(begun)
        return true
      }
      setChangedElsewhere(true)
      return false
    })
  })

  const loadLatest = useCallback(() => {
    setChangedElsewhere(false)
    seedAll()
    refresh()
  }, [seedAll, refresh])
  const keepMine = useCallback(() => setChangedElsewhere(false), [])

  const value = useMemo<SettingsCache>(
    () => ({
      settings: state.data,
      error: state.error,
      loading: state.loading,
      reload: state.reload,
      register,
      write,
      own,
      changedElsewhere,
      loadLatest,
      keepMine,
    }),
    [state.data, state.error, state.loading, state.reload, register, write, own, changedElsewhere, loadLatest, keepMine],
  )
  return <CacheContext.Provider value={value}>{children}</CacheContext.Provider>
}

export function useSettingsCache(): SettingsCache {
  const cache = useContext(CacheContext)
  if (!cache) throw new Error('useSettingsCache outside SettingsCacheProvider')
  return cache
}

/** The settings, for a component rendered only once they are loaded ({@link SettingsLoaded}). */
export function useLoadedSettings(): Settings {
  const settings = useContext(CacheContext)?.settings
  if (!settings) throw new Error('useLoadedSettings outside a loaded settings cache')
  return settings
}

/** Renders `children` only once the settings are loaded. */
export function SettingsLoaded({ children }: { children: ReactNode }) {
  return useSettingsCache().settings ? children : null
}
