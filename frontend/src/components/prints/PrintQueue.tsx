import { useEffect, useRef, useState } from 'react'
import { linkedSince } from './liveJobs'
import { api, ApiError, BAMBUDDY_NOT_CONFIGURED } from '../../api/client'
import type { ArchiveStats, QueueItem } from '../../api/types'
import { bambuddyBase } from '../../lib/bambuddyLinks'
import { Card } from '@/components/ui/card'

/** A link to Bambuddy's queue or stats beside the frame (#2351). */
const LINK = 'font-medium text-primary underline-offset-4 hover:underline'

/** How often the queue is read while the page is visible. */
export const QUEUE_POLL_MS = 30_000

/** What {@link usePrintQueue} gives. */
export interface PrintQueueState {
  /** Bambuddy's pending items, as read; `null` until the first read answers. */
  items: QueueItem[] | null
  stats: ArchiveStats | null
  error: string | null
  /** Bambuddy is not configured: said once on the page, by the printer strip (#2919). */
  unconfigured: boolean
}

/**
 * #2351 — Bambuddy's print queue for `/prints`: what is waiting to print, whoever
 * queued it, read every {@link QUEUE_POLL_MS} while the page is visible, and the farm's
 * totals, once per visit. Read only: the queue is managed in Bambuddy. Nothing is read
 * until `enabled` (#4629: on the hot path's backends, once the person asks).
 *
 * #3745 — the items are rows of the one Prints list. `started` names the working jobs
 * (`liveJobs.ts` `startedOn`); when a new one appears, the queue is read again at once, so the
 * item that job came from leaves the list with the printers' read, not up to a poll
 * later. That is a read per job start, not per poll. A job ScadBuddy links meanwhile
 * (`printer:<id>` to `archive:<id>`) is not a start (#5940).
 */
export function usePrintQueue(enabled: boolean, started: readonly string[] | null): PrintQueueState {
  const [items, setItems] = useState<QueueItem[] | null>(null)
  const [stats, setStats] = useState<ArchiveStats | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [unconfigured, setUnconfigured] = useState(false)
  const readNow = useRef<() => void>(() => undefined)

  useEffect(() => {
    if (!enabled) return
    let live = true
    let sent = 0
    const read = () => {
      if (document.hidden) return
      const mine = ++sent
      api
        .getPrintQueue('pending')
        .then((pending) => {
          if (!live || mine !== sent) return
          setItems(pending)
          setError(null)
          setUnconfigured(false)
        })
        .catch((cause: unknown) => {
          if (!live || mine !== sent) return
          setUnconfigured(cause instanceof ApiError && cause.problem.type === BAMBUDDY_NOT_CONFIGURED)
          setError(cause instanceof ApiError ? cause.detail : "Bambuddy's queue could not be read.")
        })
    }
    readNow.current = read
    read()
    // The totals change once a print ends, so once per visit is enough. A failed read
    // only leaves them out: the queue still shows (#2922).
    api.getFarmStats().then(
      (totals) => live && setStats(totals),
      () => {},
    )
    const timer = setInterval(read, QUEUE_POLL_MS)
    document.addEventListener('visibilitychange', read)
    return () => {
      live = false
      readNow.current = () => undefined
      clearInterval(timer)
      document.removeEventListener('visibilitychange', read)
    }
  }, [enabled])

  const seen = useRef<Set<string> | null>(null)
  const startedKey = started === null ? null : [...started].sort().join(' ')
  useEffect(() => {
    // Before the printers' first read nothing has started: that read is no news.
    if (startedKey === null) return
    const now = new Set(startedKey ? startedKey.split(' ') : [])
    const before = seen.current
    seen.current = now
    if (before !== null && [...now].some((job) => !before.has(job) && !linkedSince(job, before))) readNow.current()
  }, [startedKey])

  return { items, stats, error, unconfigured }
}

/**
 * #3745 — what the Prints list says about Bambuddy's queue beside its rows: a link to
 * the queue, a failed read, an empty queue, how many items are left to Bambuddy's page
 * (`tail`), and the farm's totals.
 *
 * #5940 — a `filtered` list shows no queued row (`printRows`), so it says nothing of the
 * queue. The farm's totals stay: they describe the farm, not the list, and no filter of
 * the list narrows them.
 */
export function QueueNotes({ queue, tail, filtered = false }: { queue: PrintQueueState; tail: number; filtered?: boolean }) {
  const { items, stats, error, unconfigured } = queue
  if ((items === null && error === null) || unconfigured) return null
  const base = bambuddyBase()
  return (
    <div className="mb-3 flex flex-col gap-1.5 text-xs" data-testid="print-queue">
      {!filtered && (
        <p className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-muted-foreground">
          {error && <span data-testid="print-queue-error">{error}</span>}
          {items !== null && items.length === 0 && <span>Nothing is waiting to print.</span>}
          {tail > 0 && <span data-testid="print-queue-more">{tail} more waiting in Bambuddy's queue.</span>}
          {base && (
            <a href={`${base}/queue`} target="_blank" rel="noreferrer" className={`ml-auto ${LINK}`}>
              Bambuddy's queue
            </a>
          )}
        </p>
      )}
      {stats && <FarmTotals stats={stats} statsUrl={base ? `${base}/stats` : null} />}
    </div>
  )
}

function FarmTotals({ stats, statsUrl }: { stats: ArchiveStats; statsUrl: string | null }) {
  const finished = stats.successful_prints + stats.failed_prints
  const rate = finished > 0 ? Math.round((stats.successful_prints / finished) * 100) : null
  return (
    <Card
      size="sm"
      className="sb-num flex-row flex-wrap gap-x-4 gap-y-1 px-3 text-xs text-muted-foreground"
      data-testid="farm-totals"
    >
      <span>{stats.total_prints} prints</span>
      {rate !== null && <span>{rate}% succeeded</span>}
      <span>{Math.round(stats.total_print_time_hours)} h printing</span>
      <span>{(stats.total_filament_grams / 1000).toFixed(1)} kg filament</span>
      {/* Bambuddy's stats carry no currency, so the figure is named, not given one (#2919). */}
      {stats.total_cost > 0 && <span>Cost {stats.total_cost.toFixed(2)}</span>}
      {statsUrl && (
        <a href={statsUrl} target="_blank" rel="noreferrer" className={`ml-auto ${LINK}`}>
          Bambuddy's stats
        </a>
      )}
    </Card>
  )
}
