import { useCallback, useEffect, useRef, useState } from 'react'
import { BellIcon } from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { Activity } from '../../api/types'
import { DEFAULT_ACTIVITY_QUERY } from '../../lib/activityQuery'
import { getRealtime, useSubscription } from '../../lib/realtime'
import { toast } from '../../lib/toast'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { principalLabel } from './labels'
import { StatusBadge } from './StatusBadge'

const SEEN_KEY = 'scadbuddy.activity.seen'
const DEBOUNCE_MS = 500
/** A steady stream of signals still re-reads at least this often. */
const MAX_WAIT_MS = 2_000
/**
 * `since` filters on when a row was created, so look back this far for rows that finished
 * now. Known limits: a job created more than this before the last open (a print longer than
 * a day) is never counted, and `limit` 50 newest-created can push a late finisher out; a
 * server-side `finished_since` filter would remove both.
 */
const LOOKBACK_MS = 24 * 3600 * 1000
const FINISHED = 'succeeded,failed,cancelled'
const DEFAULT_FOLLOW = ['browser']

function readSeen(): number {
  try {
    const stored = Number(window.localStorage.getItem(SEEN_KEY))
    if (Number.isFinite(stored) && stored > 0) return stored
  } catch {
    // Storage blocked: the bell counts from this page load.
  }
  // The first marker is kept, or every load before the first open would restart the count.
  const now = Date.now()
  writeSeen(now)
  return now
}

function writeSeen(at: number): void {
  try {
    window.localStorage.setItem(SEEN_KEY, String(at))
  } catch {
    // Not remembered; the next load counts from then.
  }
}

const finishedAt = (row: Activity): number => Date.parse(row.finished_at ?? '')

/**
 * The header's bell: how many activity rows of the followed principals (the
 * `activity_follow` setting) finished since it was last opened, with a toast for each
 * as it finishes.
 */
export function ActivityBell() {
  const [follow, setFollow] = useState<string[] | null>(null)
  const [rows, setRows] = useState<Activity[]>([])
  const [listed, setListed] = useState<Activity[] | null>(null)
  const seen = useRef(0)
  // Before the effects below, which read it.
  useEffect(() => {
    seen.current = readSeen()
  }, [])
  const [mounted] = useState(() => Date.now())
  const toasted = useRef(new Set<string>())
  const generation = useRef(0)
  // A read that settles once the bell is gone sets nothing (#3706): React reads `window` on
  // every update, and a test's teardown removes it, as useAsync guards (#2908).
  const live = useRef(true)
  useEffect(() => {
    live.current = true
    return () => {
      live.current = false
    }
  }, [])

  const loadFollow = useCallback(() => {
    api
      .getSettings()
      .then((settings) => {
        if (live.current) setFollow(settings.activity_follow ?? DEFAULT_FOLLOW)
      })
      .catch(() => {
        if (live.current) setFollow((current) => current ?? DEFAULT_FOLLOW)
      })
  }, [])
  useEffect(loadFollow, [loadFollow])
  // Saved in Settings (here or elsewhere): follow the new list without a reload.
  useSubscription('settings', loadFollow)

  const followKey = follow === null ? null : JSON.stringify(follow)
  const refresh = useCallback(async () => {
    if (followKey === null) return
    const principals = JSON.parse(followKey) as string[]
    const mine = ++generation.current
    const since = new Date(seen.current - LOOKBACK_MS).toISOString()
    const pages = await Promise.all(
      principals.map((principal) =>
        api
          .listActivity({ ...DEFAULT_ACTIVITY_QUERY, principal, status: FINISHED, since }, { limit: 50 })
          .then((page) => page.items)
          .catch(() => [] as Activity[]),
      ),
    )
    // A newer read started meanwhile: its answer is the fresher one. Gone: nothing to show.
    if (mine !== generation.current || !live.current) return
    const fresh = pages
      .flat()
      .filter((row) => finishedAt(row) > seen.current)
      .sort((a, b) => finishedAt(b) - finishedAt(a))
    for (const row of fresh) {
      if (toasted.current.has(row.id)) continue
      toasted.current.add(row.id)
      // Rows that finished before this page loaded are counted, not announced.
      if (finishedAt(row) < mounted) continue
      toast({
        title: `${row.title}: ${row.status}`,
        href: `/activity?principal=${encodeURIComponent(row.principal ?? '')}&status=${row.status}`,
      })
    }
    setRows(fresh)
  }, [followKey, mounted])

  useEffect(() => {
    if (followKey === null) return
    const principals = new Set(JSON.parse(followKey) as string[])
    let timer: ReturnType<typeof setTimeout> | undefined
    let first = 0
    const schedule = () => {
      clearTimeout(timer)
      const now = Date.now()
      if (first === 0) first = now
      timer = setTimeout(
        () => {
          first = 0
          void refresh()
        },
        Math.max(0, Math.min(DEBOUNCE_MS, first + MAX_WAIT_MS - now)),
      )
    }
    const stop = getRealtime().subscribe('activity', (signal) => {
      if (signal !== 'resync') {
        const principal = signal.data.principal
        if (typeof principal === 'string' && !principals.has(principal)) return
      }
      schedule()
    })
    void refresh()
    return () => {
      stop()
      clearTimeout(timer)
    }
  }, [followKey, refresh])

  // One read per followed principal per burst (the API's `principal` takes one value), and
  // any signal from a followed principal triggers it, since the event carries no status.
  const count = rows.length
  const markSeen = () => {
    const now = Date.now()
    seen.current = now
    writeSeen(now)
    setRows([])
  }

  return (
    <Popover
      open={listed !== null}
      onOpenChange={(open) => {
        if (!open) {
          setListed(null)
          return
        }
        setListed(rows)
        markSeen()
      }}
    >
      <PopoverTrigger asChild>
        <Button variant="ghost" size="icon" aria-label={`Activity, ${count} finished`} className="relative">
          <BellIcon aria-hidden="true" />
          <Badge
            data-testid="activity-bell-count"
            hidden={count === 0}
            className="pointer-events-none absolute -top-0.5 -right-0.5 h-4 min-w-4 px-1 text-[10px] leading-none tabular-nums"
          >
            {count}
          </Badge>
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-80 max-w-[calc(100vw-2rem)] gap-1 p-1.5">
        <p className="px-2 pt-1 pb-0.5 text-xs font-medium text-muted-foreground">Finished since you last looked</p>
        {listed === null || listed.length === 0 ? (
          <p className="px-2 py-2 text-[13px] text-muted-foreground">Nothing new has finished.</p>
        ) : (
          <ul className="flex max-h-80 flex-col overflow-y-auto">
            {listed.map((row) => (
              <li key={row.id} className="flex items-center justify-between gap-2 rounded-md px-2 py-1.5 text-[13px] hover:bg-muted">
                <span className="min-w-0 truncate">{row.title}</span>
                <span className="flex shrink-0 items-center gap-1.5 text-xs text-muted-foreground">
                  <StatusBadge status={row.status} />
                  {principalLabel(row.principal)}
                </span>
              </li>
            ))}
          </ul>
        )}
        <div className="border-t pt-1">
          <Button asChild variant="ghost" size="sm" className="w-full justify-start text-primary">
            <Link to="/activity" onClick={() => setListed(null)}>
              All activity
            </Link>
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}
