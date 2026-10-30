import { useCallback, useEffect, useState } from 'react'
import { aiCapabilities, type SourceView } from '../../api/capabilities'
import type { ApiError } from '../../api/client'

/** While a source is `checking` (#4757), the tree is read again after this long… */
export const POLL_MS = 1000
/** …at most this many times in a row: the agent's load check takes seconds, so then the page stops asking. */
export const POLL_MAX = 20

/** `next` in place of the source with its id, at any depth. */
function replaced(sources: SourceView[], next: SourceView): SourceView[] {
  return sources.map((s) => (s.id === next.id ? next : s.children.length > 0 ? { ...s, children: replaced(s.children, next) } : s))
}

const anyChecking = (sources: readonly SourceView[]): boolean => sources.some((s) => s.checking || anyChecking(s.children))

/**
 * The capabilities tree (#2171), read once per mount, and again shortly while a source says
 * whether a turn loads it is not known yet (`checking`, bounded by `POLL_MAX`); `filter` picks the
 * top-level sources a section shows. A write answers the source after it, which `replace` puts in
 * place; a read already under way then is dropped, so it cannot undo the write.
 */
export function useCapabilities(filter: (source: SourceView) => boolean): {
  sources: SourceView[]
  loading: boolean
  error: ApiError | Error | null
  replace(source: SourceView): void
  reload(): Promise<void>
} {
  const [all, setAll] = useState<SourceView[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<ApiError | Error | null>(null)
  /** Reads made in a row while something is checking; a failed one counts too. */
  const [polls, setPolls] = useState(0)

  const reload = useCallback(async () => {
    setLoading(true)
    setPolls(0)
    try {
      setAll(await aiCapabilities.list())
      setError(null)
    } catch (caught) {
      setError(caught instanceof Error ? caught : new Error(String(caught)))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  const checking = anyChecking(all)
  useEffect(() => {
    if (!checking || polls >= POLL_MAX) return
    let current = true
    const timer = setTimeout(() => {
      aiCapabilities.list().then(
        (sources) => {
          if (!current) return
          setPolls((n) => n + 1)
          setAll(sources)
        },
        () => current && setPolls((n) => n + 1),
      )
    }, POLL_MS)
    return () => {
      current = false
      clearTimeout(timer)
    }
    // `all` too: each answer (a write's or a read's) starts the wait again and drops a read under way.
  }, [all, checking, polls])

  useEffect(() => {
    if (!checking) setPolls(0)
  }, [checking])

  const replace = useCallback((source: SourceView) => setAll((sources) => replaced(sources, source)), [])

  return { sources: all.filter(filter), loading, error, replace, reload }
}
