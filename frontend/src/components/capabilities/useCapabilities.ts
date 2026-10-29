import { useCallback, useEffect, useState } from 'react'
import { aiCapabilities, type SourceView } from '../../api/capabilities'
import type { ApiError } from '../../api/client'

/** `next` in place of the source with its id, at any depth. */
function replaced(sources: SourceView[], next: SourceView): SourceView[] {
  return sources.map((s) => (s.id === next.id ? next : s.children.length > 0 ? { ...s, children: replaced(s.children, next) } : s))
}

/**
 * The capabilities tree (#2171), read once per mount; `filter` picks the top-level sources a
 * section shows. A write answers the source after it, which `replace` puts in place.
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

  const reload = useCallback(async () => {
    setLoading(true)
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

  const replace = useCallback((source: SourceView) => setAll((sources) => replaced(sources, source)), [])

  return { sources: all.filter(filter), loading, error, replace, reload }
}
