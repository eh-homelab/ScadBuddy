import { useEffect, useState } from 'react'
import { api, ApiError } from '../../api/client'
import type { ArchiveStats, QueueItem } from '../../api/types'
import { bambuddyBase } from '../../lib/bambuddyLinks'
import { formatDuration } from '../../lib/format'

/** How often the queue is read while the page is visible. */
export const QUEUE_POLL_MS = 30_000
/** Items listed before the rest are left to Bambuddy's queue page. */
export const QUEUE_SHOWN = 8

/**
 * #2351 — Bambuddy's print queue on `/prints`: what is waiting to print, whoever
 * queued it, and why it is waiting; with the farm's totals under it. Read only: the
 * queue is managed in Bambuddy, which the links open.
 */
export function PrintQueue() {
  const [items, setItems] = useState<QueueItem[] | null>(null)
  const [stats, setStats] = useState<ArchiveStats | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    const read = () => {
      if (document.hidden) return
      api
        .getPrintQueue('pending')
        .then((pending) => {
          if (!live) return
          setItems([...pending].sort((a, b) => (a.position ?? Infinity) - (b.position ?? Infinity)))
          setError(null)
        })
        .catch((cause: unknown) => {
          if (live) setError(cause instanceof ApiError ? cause.detail : "Bambuddy's queue could not be read.")
        })
    }
    read()
    // The totals change once a print ends, so once per visit is enough.
    api.getFarmStats().then(
      (totals) => live && setStats(totals),
      () => {},
    )
    const timer = setInterval(read, QUEUE_POLL_MS)
    document.addEventListener('visibilitychange', read)
    return () => {
      live = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', read)
    }
  }, [])

  if (items === null && error === null) return null
  const base = bambuddyBase()
  const shown = items?.slice(0, QUEUE_SHOWN) ?? []
  const more = (items?.length ?? 0) - shown.length
  return (
    <section aria-labelledby="print-queue-heading" className="mb-6" data-testid="print-queue">
      <div className="mb-2 flex items-baseline justify-between gap-2">
        <h2 id="print-queue-heading" className="text-[13px] font-medium text-ink">
          Up next
        </h2>
        {base && (
          <a href={`${base}/queue`} target="_blank" rel="noreferrer" className="text-[12px] text-accent hover:underline">
            Bambuddy's queue
          </a>
        )}
      </div>
      {error && (
        <p className="text-[12px] text-muted" data-testid="print-queue-error">
          {error}
        </p>
      )}
      {items !== null && items.length === 0 && <p className="text-[12px] text-muted">Nothing is waiting to print.</p>}
      {shown.length > 0 && (
        <ol className="divide-y divide-line rounded-[6px] border border-line bg-surface" aria-label="Waiting to print">
          {shown.map((item) => (
            <QueueRow key={item.id} item={item} />
          ))}
        </ol>
      )}
      {more > 0 && (
        <p className="mt-1 text-[12px] text-muted" data-testid="print-queue-more">
          {more} more waiting in Bambuddy's queue.
        </p>
      )}
      {stats && <FarmTotals stats={stats} statsUrl={base ? `${base}/stats` : null} />}
    </section>
  )
}

function QueueRow({ item }: { item: QueueItem }) {
  const name = item.archive_name ?? item.library_file_name ?? `Queue item ${item.id}`
  const where = item.printer_name ?? (item.target_model ? `Any ${item.target_model}` : 'No printer yet')
  const notes: string[] = []
  if (item.scheduled_time) notes.push(`Scheduled ${new Date(item.scheduled_time).toLocaleString()}`)
  if (item.manual_start) notes.push('Waits for a manual start')
  if (item.waiting_reason) notes.push(item.waiting_reason)
  return (
    <li className="px-3 py-2 text-[12.5px]" data-testid={`queue-item-${item.id}`}>
      <div className="flex flex-wrap items-baseline justify-between gap-x-3">
        <span className="min-w-0 truncate text-ink">{name}</span>
        <span className="sb-num shrink-0 text-[12px] text-muted">
          {where}
          {item.print_time_seconds ? ` · ${formatDuration(item.print_time_seconds)}` : ''}
          {item.filament_used_grams ? ` · ${Math.round(item.filament_used_grams)} g` : ''}
        </span>
      </div>
      {item.filament_short && <p className="mt-0.5 text-[12px] text-warn">Not enough filament loaded for this print.</p>}
      {notes.length > 0 && <p className="mt-0.5 text-[12px] text-muted">{notes.join(' · ')}</p>}
    </li>
  )
}

function FarmTotals({ stats, statsUrl }: { stats: ArchiveStats; statsUrl: string | null }) {
  const finished = stats.successful_prints + stats.failed_prints
  const rate = finished > 0 ? Math.round((stats.successful_prints / finished) * 100) : null
  return (
    <p className="sb-num mt-2 flex flex-wrap gap-x-3 text-[12px] text-muted" data-testid="farm-totals">
      <span>{stats.total_prints} prints</span>
      {rate !== null && <span>{rate}% succeeded</span>}
      <span>{Math.round(stats.total_print_time_hours)} h printing</span>
      <span>{(stats.total_filament_grams / 1000).toFixed(1)} kg filament</span>
      {stats.total_cost > 0 && <span>{stats.total_cost.toFixed(2)} spent</span>}
      {statsUrl && (
        <a href={statsUrl} target="_blank" rel="noreferrer" className="text-accent hover:underline">
          Bambuddy's stats
        </a>
      )}
    </p>
  )
}
