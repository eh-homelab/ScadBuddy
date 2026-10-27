import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from 'react'
import { api, ApiError } from '../../api/client'
import type { Settings } from '../../api/types'
import { setBambuddyLinks } from '../../lib/bambuddyLinks'
import { useSubscription } from '../../lib/realtime'
import { useAsync } from '../../lib/useAsync'
import { useAnyDirty } from './dirty'
import type { FieldName } from './fields'
import type { Edits } from './seed'
import { patchFields } from './values'

/**
 * A section's form, as the cache seeds it: `begin` takes its edit counts before a request,
 * `seed` gives it the named fields of the next settings, keeping any typed into since.
 * `confirm` says a read has confirmed what is stored: the section drops its
 * {@link UNCONFIRMED} notes.
 */
export type Seeder = {
  begin: () => Edits
  seed: (names: 'all' | readonly FieldName[], since: Edits) => void
  confirm: () => void
}

/** #4818 review — a write whose answer was lost, and the settings could not be read again. */
export const UNCONFIRMED = 'Could not confirm what the server stored. Reload the page to see the saved settings.'

/** How a write is numbered and followed up (#4776, #4803, #4816). */
export type WriteOptions = {
  /** The fields the request writes: a late answer is still the truth for those it wrote last. */
  written: readonly FieldName[]
  /**
   * Called when the write's answer was lost and the read that would confirm what is stored
   * failed too: the caller shows {@link UNCONFIRMED} where it reports the write's failure.
   */
  unconfirmed: () => void
}

export type SettingsCache = {
  settings: Settings | undefined
  error: Error | undefined
  loading: boolean
  reload: () => void
  /** Adds a section's seeder; the function returned removes it. */
  register: (seeder: Seeder) => () => void
  /**
   * Runs a settings write: caches its answer and seeds every seeder with `names` of it.
   * Returns what is cached, which for a late answer is the cache with its own fields patched in.
   */
  write: (
    names: 'all' | readonly FieldName[] | ((next: Settings) => readonly FieldName[]),
    request: () => Promise<Settings>,
    options: WriteOptions,
  ) => Promise<Settings>
  /** Runs work that writes the settings: their events meanwhile are this tab's own. */
  own: <T>(work: () => Promise<T>) => Promise<T>
  /** The settings changed elsewhere while a section held unsaved edits. */
  changedElsewhere: boolean
  loadLatest: () => void
  keepMine: () => void
}

const CacheContext = createContext<SettingsCache | null>(null)

/** Each seeder with the edit counts it had when a request was sent. */
type Begun = readonly (readonly [Seeder, Edits])[]

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
  const begin = useCallback((): Begun => [...seeders.current].map((seeder) => [seeder, seeder.begin()] as const), [])

  const own = useCallback(async <T,>(work: () => Promise<T>): Promise<T> => {
    owning.current++
    try {
      return await work()
    } finally {
      owning.current--
    }
  }, [])

  // #4776, #4803: every write (a save, a reset) and every read of the settings after the
  // first is numbered as sent. Only the newest answer to land is cached whole; an older one
  // landing after it would take the settings back a step.
  const writeSent = useRef(0)
  const writeLanded = useRef(0)
  /**
   * #4837 — the notes of lost writes that nothing has confirmed what is stored for yet. A
   * newer read supersedes a lost write's reread, and `useAsync` then drops its `failed`; so
   * every read says them when it fails. Whatever learns what is stored lets them go: a
   * write's answer cached whole, and any read that lands, taken or refused. A refused read
   * still returned what is stored, whether a newer write landed first (whose cached answer
   * let them go already) or the form is edited.
   */
  const unconfirmedWrites = useRef<(() => void)[]>([])
  /** Whether the answer to `sent` is the newest yet; it is then the newest landed. */
  const landed = useCallback((sent: number): boolean => {
    if (sent <= writeLanded.current) return false
    writeLanded.current = sent
    return true
  }, [])
  /** The write each field was last sent in: a late answer is still the truth for these. */
  const fieldWriter = useRef(new Map<FieldName, number>())
  /** Each write in flight: the writer each of its fields had before it was sent. */
  const priorWriter = useRef(new Map<number, Map<FieldName, number | undefined>>())
  /**
   * The settings as last cached, read when an answer lands: the render's `state.data` can
   * be a step behind when two answers land together. Any new `setData` caller must update
   * `cached` too, or the next late answer is patched into stale settings.
   */
  const cached = useRef<Settings | undefined>(undefined)
  useEffect(() => {
    if (state.data && cached.current === undefined) cached.current = state.data
  }, [state.data])

  /**
   * Caches a write's answer and returns what is cached. The newest answer is cached whole.
   * A late one is not: a newer answer has landed. Yet the server may have applied the
   * writes in another order than they were sent, so that newer answer can lack this
   * write's fields. The late answer's own fields, the ones no newer write was sent for,
   * are patched into the cache from it. `patchFields` copies only those fields, their
   * `has_*` flags and their `sources`, not values the server derives from a write (e.g.
   * `library_backend` and `store_backend` on a Connection fallback).
   */
  const cacheWrite = useCallback(
    (sent: number, next: Settings, written: readonly FieldName[]): Settings => {
      priorWriter.current.delete(sent)
      if (landed(sent) || !cached.current) {
        // The whole of what is stored, newer than any lost write's (#4837 review).
        unconfirmedWrites.current = []
        cached.current = next
        setData(next)
        return next
      }
      const own = written.filter((name) => fieldWriter.current.get(name) === sent)
      const patched = patchFields(cached.current, next, own)
      cached.current = patched
      setData(patched)
      return patched
    },
    [landed, setData],
  )
  /** Numbers a write as sent, and marks it the last one sent for each of `written`. */
  const sendWrite = useCallback((written: readonly FieldName[]): number => {
    const sent = ++writeSent.current
    priorWriter.current.set(sent, new Map(written.map((name) => [name, fieldWriter.current.get(name)])))
    for (const name of written) fieldWriter.current.set(name, sent)
    return sent
  }, [])
  /**
   * A write that failed gives its fields back to the writer each had before, unless a newer
   * write was sent for it since: a late answer from that older write is still their truth.
   * A newer write still in flight inherits the same prior, should it fail too.
   */
  const failWrite = useCallback((sent: number) => {
    for (const [name, prior] of priorWriter.current.get(sent) ?? []) {
      for (const priors of priorWriter.current.values()) {
        if (priors.get(name) === sent) priors.set(name, prior)
      }
      if (fieldWriter.current.get(name) !== sent) continue
      if (prior === undefined) fieldWriter.current.delete(name)
      else fieldWriter.current.set(name, prior)
    }
    priorWriter.current.delete(sent)
  }, [])

  /**
   * Numbers a read as sent and refreshes the settings. `onAccept` runs only when no newer
   * write has landed, and takes the read by returning the seeders' edit counts to seed
   * `seed` with since, or refuses it with `undefined`. A taken read is the newest landed and
   * is cached whole; it confirms what is stored, so every seeder drops its note that it
   * could not be, and the lost writes still waiting for one (`unconfirmedWrites`) go. A read
   * that fails says those.
   *
   * `useAsync.refresh` reads calls made in the same microtask once, deciding by the last
   * caller's accept, so the other callers' `onAccept` and `seed` are dropped. The notes
   * survive a merge: they are kept in `unconfirmedWrites`, not in a caller. A caller started
   * from the same event as another must not rely on its own `seed` (or `onAccept`) being used.
   */
  const numberedRead = useCallback(
    (onAccept: () => Begun | undefined, seed: 'all' | readonly FieldName[] = 'all', onNewer?: () => void) => {
      const sent = ++writeSent.current
      refresh(
        (next) => {
          // It landed, so what is stored is known, whether or not it is taken (#4837 review).
          unconfirmedWrites.current = []
          // #4803: a save or reset sent after this read has landed already, and is newer.
          if (sent <= writeLanded.current) {
            onNewer?.()
            return false
          }
          // `onAccept` must not land a write between the check above and `landed(sent)`.
          const begun = onAccept()
          if (begun === undefined) return false
          landed(sent)
          cached.current = next
          // A section that unmounted meanwhile has nothing left to seed.
          for (const [seeder, since] of begun) if (seeders.current.has(seeder)) seeder.seed(seed, since)
          for (const seeder of seeders.current) seeder.confirm()
          return true
        },
        () => {
          // Merged reads call this once each: the first says the notes, the rest find none.
          const notes = unconfirmedWrites.current
          unconfirmedWrites.current = []
          for (const note of notes) note()
        },
      )
    },
    [landed, refresh],
  )

  /**
   * #4816 — a write that threw. Only a refusal (a 4xx) was surely not applied, and gives its
   * fields back. That includes 408 and 429 on purpose: a proxy or the server refused the
   * request before applying anything. The rule is the 4xx range, not "any status": a 5xx
   * can come after the write was applied. Any other failure (no answer, a 5xx, a timeout)
   * may have been applied with its answer lost: its fields stay its own, so no older answer
   * patches over them, and the settings are read again, seeding no field, so the form keeps
   * what was typed. Should that read fail too, nothing reads again until the next settings
   * event, so `unconfirmed` says, where the write reports its failure, that it could not
   * confirm what is stored. It waits in `unconfirmedWrites`: a newer read that replaced
   * this one says it too, should it fail (#4837).
   */
  const writeThrew = useCallback(
    (sent: number, cause: unknown, unconfirmed: () => void) => {
      if (cause instanceof ApiError && cause.status >= 400 && cause.status < 500) {
        failWrite(sent)
        return
      }
      priorWriter.current.delete(sent)
      const begun = begin()
      unconfirmedWrites.current.push(unconfirmed)
      numberedRead(() => begun, [])
    },
    [failWrite, begin, numberedRead],
  )

  const write = useCallback<SettingsCache['write']>(
    (names, request, { written, unconfirmed }) =>
      own(async () => {
        const begun = begin()
        const sent = sendWrite(written)
        let answer: Settings
        try {
          answer = await request()
        } catch (cause) {
          writeThrew(sent, cause, unconfirmed)
          throw cause
        }
        // What is cached, not this answer: a late answer is only patched in (#4803).
        const next = cacheWrite(sent, answer, written)
        setBambuddyLinks(next)
        const seeded = typeof names === 'function' ? names(next) : names
        // A section that unmounted meanwhile has nothing left to seed.
        for (const [seeder, since] of begun) if (seeders.current.has(seeder)) seeder.seed(seeded, since)
        return next
      }),
    [own, begin, sendWrite, writeThrew, cacheWrite],
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
    numberedRead(() => {
      if (!anyDirty()) return begun
      setChangedElsewhere(true)
      return undefined
    })
  })

  const loadLatest = useCallback(() => {
    setChangedElsewhere(false)
    const begun = begin()
    // A write of this tab's that landed first refuses the read, yet the drafts were asked to
    // go, and the banner has: they are seeded from that newer answer instead (#4847 review).
    // A new object, so each section's seed effect runs.
    const reseed = () => {
      const current = cached.current
      if (!current) return
      for (const [seeder, since] of begun) if (seeders.current.has(seeder)) seeder.seed('all', since)
      cached.current = { ...current }
      setData(cached.current)
    }
    numberedRead(() => begun, 'all', reseed)
  }, [begin, numberedRead, setData])
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
