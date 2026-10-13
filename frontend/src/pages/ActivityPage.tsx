import { useCallback, useState } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { Activity } from '../api/types'
import { ActivityFilters } from '../components/activity/ActivityFilters'
import { ActivityTable } from '../components/activity/ActivityTable'
import { sinceTime } from '../components/activity/labels'
import { Button } from '../components/ui/Button'
import { parseActivityQuery, toActivityParams, type ActivityQuery } from '../lib/activityQuery'
import { useAsync } from '../lib/useAsync'

/** The query as the API takes it: `since` is a time, the URL keeps its token. */
function wire(query: ActivityQuery): ActivityQuery {
  return { ...query, since: sinceTime(query.since) }
}

export function ActivityPage() {
  const [params, setParams] = useSearchParams()
  const query = parseActivityQuery(params)
  const key = toActivityParams(query).toString()
  const facets = useAsync(() => api.activityFacets(sinceTime(query.since) || undefined), [query.since], ['activity'])
  const list = useAsync(() => api.listActivity(wire(query)), [key], ['activity'])
  const [more, setMore] = useState<{ key: string; items: Activity[]; cursor: string | null } | null>(null)
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState<string>()

  const onChange = useCallback(
    (next: ActivityQuery) => setParams(toActivityParams(next), { replace: true }),
    [setParams],
  )

  const extra = more?.key === key ? more : null
  const first = list.data
  const items = first ? [...first.items, ...(extra?.items ?? []).filter((i) => !first.items.some((f) => f.id === i.id))] : []
  const cursor = extra ? extra.cursor : (first?.next_cursor ?? null)

  async function loadMore() {
    if (!cursor) return
    setLoadingMore(true)
    setMoreError(undefined)
    try {
      const page = await api.listActivity(wire(query), { cursor })
      setMore({ key, items: [...(extra?.items ?? []), ...page.items], cursor: page.next_cursor })
    } catch (e) {
      setMoreError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-[18px] font-medium">Activity</h1>
      <ActivityFilters query={query} facets={facets.data} onChange={onChange} />
      {list.error ? (
        <p role="alert" className="text-[13px] text-warn">
          Could not load activity: {list.error.message}
        </p>
      ) : list.loading ? (
        <p className="text-[13px] text-muted">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-[13px] text-muted">No activity matches.</p>
      ) : (
        <ActivityTable items={items} facets={facets.data} />
      )}
      {moreError && (
        <p role="alert" className="text-[13px] text-warn">
          Could not load more: {moreError}
        </p>
      )}
      {cursor && (
        <div>
          <Button variant="ghost" onClick={() => void loadMore()} disabled={loadingMore}>
            Load more
          </Button>
        </div>
      )}
    </div>
  )
}
