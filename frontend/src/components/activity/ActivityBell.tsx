import { useCallback, useContext, useEffect, useRef, useState } from 'react'
import { BellIcon, BellRingIcon } from 'lucide-react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { Activity } from '../../api/types'
import { DEFAULT_ACTIVITY_QUERY } from '../../lib/activityQuery'
import { NEEDS_YOU_PATH, NeedsYouContext, needsYouLabel } from '../../lib/needsYou'
import { cn } from '@/lib/utils'
import { NeedsYouList } from './NeedsYou'
import { getRealtime, readAgain, recordRead, useSubscription, type ReadRecord } from '../../lib/realtime'
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
 * as it finishes. #6399: first, every request that waits on the person (`lib/needsYou.ts`,
 * the shell's `NeedsYouContext`), each with its answer, until it is answered.
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

  /** What the last follow and activity reads saw, for a confirmation to judge (#1039). */
  const followRead = useRef<ReadRecord | undefined>(undefined)
  const activityRead = useRef<ReadRecord | undefined>(undefined)
  const loadFollow = useCallback(() => {
    const { record, result } = recordRead(() => api.getSettings())
    followRead.current = record
    result
      .then((settings) => {
        if (live.current) setFollow(settings.activity_follow ?? DEFAULT_FOLLOW)
      })
      .catch(() => {
        if (live.current) setFollow((current) => current ?? DEFAULT_FOLLOW)
      })
  }, [])
  useEffect(loadFollow, [loadFollow])
  // Saved in Settings (here or elsewhere): follow the new list without a reload.
  useSubscription('settings', (_signal, confirmation) => {
    if (!confirmation) return loadFollow()
    void readAgain(followRead.current, 'settings', confirmation).then((stale) => {
      if (stale) loadFollow()
    })
  })

  const followKey = follow === null ? null : JSON.stringify(follow)
  /** True when its answer is what the bell now shows. */
  const refresh = useCallback(async (): Promise<boolean> => {
    if (followKey === null) return false
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
    if (mine !== generation.current || !live.current) return false
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
    return true
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
          // Once shown, a later confirmation (a reconnect's) asks from what it saw.
          const { record } = recordRead(refresh, (applied) => applied)
          void record.shown.then((shown) => {
            if (shown) activityRead.current = record
          })
        },
        Math.max(0, Math.min(DEBOUNCE_MS, first + MAX_WAIT_MS - now)),
      )
    }
    const stop = getRealtime().subscribe('activity', (signal, confirmation) => {
      if (confirmation) {
        void readAgain(activityRead.current, 'activity', confirmation).then((stale) => {
          if (stale) schedule()
        })
        return
      }
      if (signal !== 'resync') {
        const principal = signal.data.principal
        if (typeof principal === 'string' && !principals.has(principal)) return
      }
      schedule()
    })
    activityRead.current = recordRead(refresh, (applied) => applied).record
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

  // #6399 — what waits on the person: listed first, counted until answered, never cleared by
  // opening; only the finished rows are "seen".
  const needsYou = useContext(NeedsYouContext)
  const waiting = needsYou?.items?.length ?? 0
  const label = ['Activity', needsYouLabel(waiting), count > 0 ? `${count} finished` : ''].filter(Boolean).join(', ')
  const close = () => setListed(null)

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
      {/* Announced as it changes: the bell's own name is read only when it is focused. */}
      <span data-testid="activity-bell-live" aria-live="polite" className="sr-only">
        {needsYouLabel(waiting)}
      </span>
      <PopoverTrigger asChild>
        {waiting > 0 ? (
          // Hard to miss while anything waits: its own colour, a ringing bell and the count, and
          // on a wider screen the words too; the name says it all for a screen reader.
          <Button
            variant="ghost"
            size="sm"
            aria-label={label}
            data-needs-you=""
            className="relative h-8 gap-1.5 rounded-full bg-needs px-2.5 font-semibold text-needs-ink shadow-sm hover:bg-needs/85 hover:text-needs-ink focus-visible:ring-needs/50 aria-expanded:bg-needs/85 aria-expanded:text-needs-ink"
          >
            <BellRingIcon aria-hidden="true" />
            <span className="hidden sm:inline">Needs you</span>
            <span data-testid="activity-bell-needs" className="tabular-nums">
              {waiting}
            </span>
            <Badge
              data-testid="activity-bell-count"
              hidden={count === 0}
              className="pointer-events-none absolute -top-1 -right-1 h-4 min-w-4 px-1 text-[10px] leading-none tabular-nums ring-2 ring-background"
            >
              {count}
            </Badge>
          </Button>
        ) : (
          <Button variant="ghost" size="icon" aria-label={label} className="relative">
            <BellIcon aria-hidden="true" />
            <Badge
              data-testid="activity-bell-count"
              hidden={count === 0}
              className="pointer-events-none absolute -top-0.5 -right-0.5 h-4 min-w-4 px-1 text-[10px] leading-none tabular-nums"
            >
              {count}
            </Badge>
          </Button>
        )}
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className={cn('max-w-[calc(100vw-2rem)] gap-1 p-1.5', waiting > 0 ? 'w-[26rem]' : 'w-80')}
      >
        {waiting > 0 && (
          <section aria-labelledby="bell-needs-you" className="flex flex-col gap-1 pb-1">
            <h2 id="bell-needs-you" className="flex items-center justify-between px-2 pt-1 text-xs font-semibold text-needs">
              Needs you
              <Link to={NEEDS_YOU_PATH} onClick={close} className="font-medium text-primary hover:underline">
                See all
              </Link>
            </h2>
            <div className="max-h-[min(24rem,60dvh)] overflow-y-auto pr-0.5">
              <NeedsYouList compact onLeave={close} />
            </div>
          </section>
        )}
        <p className={cn('px-2 pt-1 pb-0.5 text-xs font-medium text-muted-foreground', waiting > 0 && 'border-t pt-1.5')}>
          Finished since you last looked
        </p>
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
