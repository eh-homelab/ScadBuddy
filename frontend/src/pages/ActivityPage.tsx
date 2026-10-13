import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import { ActivityDrawer } from '../components/activity/ActivityDrawer'
import { ActivityFilters } from '../components/activity/ActivityFilters'
import { ActivityTable } from '../components/activity/ActivityTable'
import { sinceTime } from '../components/activity/labels'
import { Button } from '../components/ui/Button'
import { parseActivityQuery, toActivityParams, type ActivityQuery } from '../lib/activityQuery'
import { getRealtime } from '../lib/realtime'
import { useAsync } from '../lib/useAsync'

const PAGE = 50
/** The API's largest page; a live re-read of what is loaded stays within it. */
const MAX_LOADED = 200
/** The facets are an aggregate: a burst of changes re-reads them at most this often. */
const FACETS_EVERY_MS = 3000

/** The query as the API takes it: `since` is a time, the URL keeps its token. */
function wire(query: ActivityQuery): ActivityQuery {
  return { ...query, since: sinceTime(query.since) }
}

export function ActivityPage() {
  const [params, setParams] = useSearchParams()
  const query = parseActivityQuery(params)
  const key = toActivityParams(query).toString()
  // How many rows to hold for this query. A live re-read asks for that many, so rows
  // already loaded stay current and a new row cannot open a gap before the next page.
  const want = useRef({ key, count: PAGE })
  const facets = useAsync(() => api.activityFacets(sinceTime(query.since) || undefined), [query.since])
  const list = useAsync(() => {
    if (want.current.key !== key) want.current = { key, count: PAGE }
    return api.listActivity(wire(query), { limit: want.current.count })
  }, [key])
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreError, setMoreError] = useState<{ key: string; message: string }>()
  const [open, setOpen] = useState<string | null>(null)

  const refreshList = list.refresh
  const refreshFacets = facets.refresh
  useEffect(() => {
    let last = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const stop = getRealtime().subscribe('activity', () => {
      refreshList()
      const wait = last + FACETS_EVERY_MS - Date.now()
      if (wait <= 0) {
        last = Date.now()
        refreshFacets()
      } else if (timer === undefined) {
        timer = setTimeout(() => {
          timer = undefined
          last = Date.now()
          refreshFacets()
        }, wait)
      }
    })
    return () => {
      stop()
      clearTimeout(timer)
    }
  }, [refreshList, refreshFacets])

  const onChange = useCallback(
    (next: ActivityQuery, options?: { typing?: boolean }) =>
      setParams(toActivityParams(next), { replace: options?.typing === true }),
    [setParams],
  )

  const items = list.data?.items ?? []
  const hasMore = list.data?.next_cursor != null
  const atCap = items.length >= MAX_LOADED

  useEffect(() => {
    setLoadingMore(false)
  }, [list.data])

  function loadMore() {
    const previous = want.current.count
    want.current = { key, count: Math.min(MAX_LOADED, items.length + PAGE) }
    setLoadingMore(true)
    setMoreError(undefined)
    list.refresh(undefined, () => {
      want.current = { key, count: previous }
      setLoadingMore(false)
      setMoreError({ key, message: 'the read failed' })
    })
  }

  return (
    <div className="flex flex-col gap-4">
      <h1 className="text-lg font-semibold tracking-tight">Activity</h1>
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
        <ActivityTable items={items} facets={facets.data} onOpen={setOpen} />
      )}
      {moreError?.key === key && (
        <p role="alert" className="text-[13px] text-warn">
          Could not load more: {moreError.message}
        </p>
      )}
      {hasMore && !atCap && (
        <div>
          <Button variant="ghost" onClick={loadMore} disabled={loadingMore}>
            Load more
          </Button>
        </div>
      )}
      {hasMore && atCap && (
        <p className="text-[13px] text-muted">Showing the latest {MAX_LOADED}. Narrow the filters to see older activity.</p>
      )}
      {open && <ActivityDrawer id={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
