import { useCallback, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router'
import { api } from '../api/client'
import type { Activity, ActivityFacets, ActivityPage as ActivityPageData } from '../api/types'
import { ActivityDrawer } from '../components/activity/ActivityDrawer'
import { ActivityFilters } from '../components/activity/ActivityFilters'
import { ActivityTable } from '../components/activity/ActivityTable'
import { sinceTime } from '../components/activity/labels'
import { Button } from '../components/ui/Button'
import { parseActivityQuery, toActivityParams, type ActivityQuery } from '../lib/activityQuery'
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
  // Pages appended past the cap, by cursor. While any are held the list is not re-read
  // (a new row would open a gap before them); the facets stay live.
  const [older, setOlder] = useState<{ key: string; items: Activity[]; cursor: string | null } | null>(null)
  const olderRef = useRef(older)
  const dataRef = useRef<ActivityPageData | undefined>(undefined)
  const gate = useRef<{ last: number; pending: Promise<ActivityFacets> | null }>({ last: 0, pending: null })

  const facets = useAsync(
    () => {
      const read = () => api.activityFacets(sinceTime(query.since) || undefined)
      const g = gate.current
      const wait = g.last + FACETS_EVERY_MS - Date.now()
      if (wait <= 0) {
        g.last = Date.now()
        return read()
      }
      g.pending ??= new Promise<ActivityFacets>((resolve, reject) => {
        setTimeout(() => {
          g.pending = null
          g.last = Date.now()
          read().then(resolve, reject)
        }, wait)
      })
      return g.pending
    },
    [query.since],
    ['activity'],
  )
  const list = useAsync(
    () => {
      if (olderRef.current?.key === key && dataRef.current) return Promise.resolve(dataRef.current)
      if (want.current.key !== key) want.current = { key, count: PAGE }
      return api.listActivity(wire(query), { limit: want.current.count })
    },
    [key],
    ['activity'],
  )
  const [loadingMore, setLoadingMore] = useState(false)
  const [moreFailed, setMoreFailed] = useState<string>()
  const [open, setOpen] = useState<string | null>(null)

  useEffect(() => {
    dataRef.current = list.data
  }, [list.data])

  const onChange = useCallback(
    (next: ActivityQuery, options?: { typing?: boolean }) =>
      setParams(toActivityParams(next), { replace: options?.typing === true }),
    [setParams],
  )

  const held = older?.key === key ? older : null
  const base = list.data?.items ?? []
  const items = [...base, ...(held?.items ?? []).filter((i) => !base.some((b) => b.id === i.id))]
  const hasMore = held ? held.cursor !== null : list.data?.next_cursor != null
  const capped = base.length >= MAX_LOADED

  useEffect(() => {
    setLoadingMore(false)
  }, [list.data])

  function keepOlder(next: typeof older) {
    olderRef.current = next
    setOlder(next)
  }

  function loadMore() {
    setLoadingMore(true)
    setMoreFailed(undefined)
    if (!capped) {
      const previous = want.current.count
      want.current = { key, count: Math.min(MAX_LOADED, base.length + PAGE) }
      list.refresh(undefined, () => {
        want.current = { key, count: previous }
        setLoadingMore(false)
        setMoreFailed(key)
      })
      return
    }
    const cursor = held ? held.cursor : (list.data?.next_cursor ?? null)
    if (!cursor) return
    api.listActivity(wire(query), { cursor, limit: PAGE }).then(
      (page) => {
        keepOlder({ key, items: [...(held?.items ?? []), ...page.items], cursor: page.next_cursor })
        setLoadingMore(false)
      },
      () => {
        setLoadingMore(false)
        setMoreFailed(key)
      },
    )
  }

  function showLatest() {
    want.current = { key, count: PAGE }
    keepOlder(null)
    list.refresh()
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
      {moreFailed === key && (
        <p role="alert" className="text-[13px] text-warn">
          Could not load more. Try again.
        </p>
      )}
      {held && (
        <p className="flex items-center gap-2 text-[13px] text-muted">
          Live updates paused, showing older activity.
          <Button size="sm" variant="ghost" onClick={showLatest}>
            Show latest
          </Button>
        </p>
      )}
      {hasMore && (
        <div>
          <Button variant="ghost" onClick={loadMore} disabled={loadingMore}>
            Load more
          </Button>
        </div>
      )}
      {open && <ActivityDrawer id={open} onClose={() => setOpen(null)} />}
    </div>
  )
}
