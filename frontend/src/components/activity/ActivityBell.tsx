import { useCallback, useEffect, useId, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api } from '../../api/client'
import type { Activity } from '../../api/types'
import { DEFAULT_ACTIVITY_QUERY } from '../../lib/activityQuery'
import { focusInto } from '../../lib/modal'
import { getRealtime, useSubscription } from '../../lib/realtime'
import { toast } from '../../lib/toast'
import { principalLabel } from './labels'

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
  const button = useRef<HTMLButtonElement>(null)
  const popover = useRef<HTMLDivElement>(null)
  const popoverId = useId()
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
  const close = useCallback((refocus: boolean) => {
    setListed(null)
    if (refocus) button.current?.focus()
  }, [])
  const isOpen = listed !== null
  useEffect(() => {
    if (!isOpen) return
    if (popover.current) focusInto(popover.current)
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !event.defaultPrevented) close(true)
    }
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node
      if (!popover.current?.contains(target) && !button.current?.contains(target)) close(false)
    }
    document.addEventListener('keydown', onKey)
    document.addEventListener('mousedown', onPointer)
    return () => {
      document.removeEventListener('keydown', onKey)
      document.removeEventListener('mousedown', onPointer)
    }
  }, [isOpen, close])
  const markSeen = () => {
    const now = Date.now()
    seen.current = now
    writeSeen(now)
    setRows([])
  }

  return (
    <div className="relative">
      <button
        ref={button}
        type="button"
        aria-controls={listed !== null ? popoverId : undefined}
        aria-label={`Activity, ${count} finished`}
        aria-expanded={listed !== null}
        onClick={() => {
          if (listed !== null) {
            setListed(null)
            return
          }
          setListed(rows)
          markSeen()
        }}
        className="relative inline-flex h-8 items-center gap-1 rounded-[6px] px-2 text-[13px] text-muted hover:bg-surface-2 hover:text-ink"
      >
        <svg aria-hidden="true" viewBox="0 0 24 24" className="size-4" fill="none" stroke="currentColor" strokeWidth="2">
          <path d="M6 9a6 6 0 1 1 12 0c0 6 2 7 2 7H4s2-1 2-7M10 20h4" />
        </svg>
        <span
          data-testid="activity-bell-count"
          hidden={count === 0}
          className="rounded-full bg-accent px-1.5 text-[11px] leading-4 text-white"
        >
          {count}
        </span>
      </button>
      {listed !== null && (
        <div ref={popover} id={popoverId} className="absolute right-0 top-9 z-40 w-72 rounded-[6px] border border-line bg-surface p-2 text-[13px] shadow-lg">
          {listed.length === 0 ? (
            <p className="px-1 py-1 text-muted">Nothing new has finished.</p>
          ) : (
            <ul className="space-y-1">
              {listed.map((row) => (
                <li key={row.id} className="flex justify-between gap-2 px-1">
                  <span className="truncate">{row.title}</span>
                  <span className="shrink-0 text-muted">
                    {row.status} · {principalLabel(row.principal)}
                  </span>
                </li>
              ))}
            </ul>
          )}
          <Link to="/activity" onClick={() => setListed(null)} className="mt-2 block px-1 text-accent hover:underline">
            All activity
          </Link>
        </div>
      )}
    </div>
  )
}
