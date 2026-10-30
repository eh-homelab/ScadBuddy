import { useCallback, useEffect, useMemo, useSyncExternalStore } from 'react'
import { aiCapabilities, type SourceView } from '../../api/capabilities'
import type { ApiError } from '../../api/client'

/**
 * While a source is `checking` (#4757), the tree is read again after each of these waits in turn:
 * 1 s doubling to 10 s, about two minutes in all. Then the page stops asking and says so
 * (`stalled`), with a Retry that starts again.
 */
export const POLL_DELAYS: readonly number[] = [1000, 2000, 4000, 8000, ...Array<number>(11).fill(10_000)]

/** `next` in place of the source with its id, at any depth. */
function replaced(sources: SourceView[], next: SourceView): SourceView[] {
  return sources.map((s) => (s.id === next.id ? next : s.children.length > 0 ? { ...s, children: replaced(s.children, next) } : s))
}

const anyChecking = (sources: readonly SourceView[]): boolean => sources.some((s) => s.checking || anyChecking(s.children))

type Tree = {
  all: SourceView[]
  loading: boolean
  error: ApiError | Error | null
  /** Waits used up while something is checking; `POLL_DELAYS.length` is given up. */
  polls: number
  /** Why the last poll read failed, while the poll goes on or has given up. */
  pollError: string | null
}

const INITIAL: Tree = { all: [], loading: true, error: null, polls: 0, pollError: null }

/**
 * The one tree a page shares: every section's hook reads it, so one checking package costs one
 * read per wait, not one per section. It is dropped once the last hook unmounts.
 */
const shared = {
  tree: INITIAL,
  listeners: new Set<() => void>(),
  mounted: 0,
  /** Bumped by each change to `all` and by a drop: a poll read started before it is not applied. */
  version: 0,
  timer: undefined as ReturnType<typeof setTimeout> | undefined,
  loading: null as Promise<void> | null,
  /** Reads started by `load`: only the latest is applied. */
  loads: 0,
  /** Bumped when the last hook unmounts: nothing started before it is applied. */
  epoch: 0,
  /** Bumped by each write's answer (`replace`): a load read before it is not applied. */
  writes: 0,
}

const asError = (caught: unknown) => (caught instanceof Error ? caught : new Error(String(caught)))

function set(next: Partial<Tree>): void {
  if (next.all !== undefined) shared.version += 1
  shared.tree = { ...shared.tree, ...next }
  for (const listener of shared.listeners) listener()
  if (next.all !== undefined) schedule()
}

/** The next poll, when something is checking and a wait is left. */
function schedule(): void {
  clearTimeout(shared.timer)
  shared.timer = undefined
  if (shared.mounted === 0) return
  if (!anyChecking(shared.tree.all)) {
    if (shared.tree.polls !== 0 || shared.tree.pollError !== null) set({ polls: 0, pollError: null })
    return
  }
  const wait = POLL_DELAYS[shared.tree.polls]
  if (wait === undefined) return
  shared.timer = setTimeout(poll, wait)
}

function poll(): void {
  shared.timer = undefined
  const at = shared.version
  const epoch = shared.epoch
  aiCapabilities.list().then(
    (all) => {
      // A write (or another answer) came first: its sources win; the wait starts again from it.
      if (at !== shared.version || epoch !== shared.epoch) return
      set({ polls: shared.tree.polls + 1, pollError: null, all })
    },
    (caught: unknown) => {
      if (at !== shared.version || epoch !== shared.epoch) return
      set({ polls: shared.tree.polls + 1, pollError: asError(caught).message })
      schedule()
    },
  )
}

/**
 * Reads the tree. A mount joins a read already under way, so sections mounted together read once;
 * a `reload` (after an install or an approval) reads afresh, and only the latest read is applied.
 * A read that a write overtook is not applied, as it may predate the write: the tree is read again.
 */
function load(join: boolean): Promise<void> {
  if (join && shared.loading) return shared.loading
  const seq = ++shared.loads
  const epoch = shared.epoch
  const writes = shared.writes
  set({ loading: true, polls: 0, pollError: null })
  const latest = () => seq === shared.loads && epoch === shared.epoch
  const done = aiCapabilities
    .list()
    .then(
      (all) => {
        if (!latest()) return
        if (writes !== shared.writes) return load(false)
        set({ all, error: null })
      },
      (caught: unknown) => {
        if (latest()) set({ error: asError(caught) })
      },
    )
    .finally(() => {
      if (!latest()) return
      shared.loading = null
      set({ loading: false })
    })
  shared.loading = done
  return done
}

function mount(): () => void {
  shared.mounted += 1
  void load(true)
  return () => {
    shared.mounted -= 1
    if (shared.mounted > 0) return
    clearTimeout(shared.timer)
    shared.timer = undefined
    shared.loading = null
    shared.epoch += 1
    shared.version += 1
    shared.tree = INITIAL
  }
}

function subscribe(listener: () => void): () => void {
  shared.listeners.add(listener)
  return () => shared.listeners.delete(listener)
}

const snapshot = () => shared.tree

/** Whether the shared poll has given up on a source still checking, and how to start it again. */
export function usePollStatus(): { stalled: boolean; pollError: string | null; retry(): void } {
  const tree = useSyncExternalStore(subscribe, snapshot)
  const stalled = anyChecking(tree.all) && tree.polls >= POLL_DELAYS.length
  return { stalled, pollError: tree.pollError, retry: () => void load(false) }
}

/**
 * The capabilities tree (#2171), read once per mount (sections mounted together share one read),
 * and again with back-off while a source says whether a turn loads it is not known yet
 * (`checking`); `filter` picks the top-level sources a section shows. A write answers the source
 * after it, which `replace` puts in place; a poll read already under way then is dropped, so it
 * cannot undo the write.
 */
export function useCapabilities(filter: (source: SourceView) => boolean): {
  sources: SourceView[]
  loading: boolean
  error: ApiError | Error | null
  stalled: boolean
  replace(source: SourceView): void
  reload(): Promise<void>
} {
  const tree = useSyncExternalStore(subscribe, snapshot)
  useEffect(mount, [])
  const replace = useCallback((source: SourceView) => {
    shared.writes += 1
    set({ all: replaced(shared.tree.all, source) })
  }, [])
  const reload = useCallback(() => load(false), [])
  const sources = useMemo(() => tree.all.filter(filter), [tree.all, filter])
  const stalled = anyChecking(tree.all) && tree.polls >= POLL_DELAYS.length
  return { sources, loading: tree.loading, error: tree.error, stalled, replace, reload }
}
