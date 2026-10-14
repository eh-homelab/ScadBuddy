import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { PrinterNow } from '../../api/types'
import { bambuddyLink } from '../../lib/bambuddyLinks'
import { formatDuration } from '../../lib/format'

/** How often the printers are read while the page is visible. */
export const NOW_POLL_MS = 15_000

const ACTIVITY: Record<PrinterNow['activity'], { label: string; tone: string }> = {
  printing: { label: 'Printing', tone: 'border-accent/50 bg-accent/10 text-ink' },
  paused: { label: 'Paused', tone: 'border-warn/40 bg-warn/10 text-warn' },
  idle: { label: 'Idle', tone: 'border-line text-muted' },
  finished: { label: 'Finished', tone: 'border-ok/40 bg-ok/10 text-ok' },
  failed: { label: 'Failed', tone: 'border-warn/40 bg-warn/10 text-warn' },
  offline: { label: 'Offline', tone: 'border-dashed border-line-strong text-faint' },
  unknown: { label: 'Unknown', tone: 'border-line text-muted' },
}

/**
 * #977 — what each printer is doing now, whoever started the job: a print ScadBuddy
 * queued, one from Bambuddy's library, or one started on the printer. Read every
 * `NOW_POLL_MS` while the page is visible, and at once when it becomes visible again.
 */
export function NowPrinting() {
  const [printers, setPrinters] = useState<PrinterNow[] | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let live = true
    // A timer read and a visibility read can overlap; only the newest one may land.
    let sent = 0
    const read = () => {
      if (document.hidden) return
      const mine = ++sent
      const current = () => live && mine === sent
      api
        .getPrintersNow()
        .then((now) => {
          if (!current()) return
          setPrinters(now.printers ?? [])
          setError(null)
        })
        .catch((cause: unknown) => {
          if (current()) setError(cause instanceof ApiError ? cause.detail : 'The printers could not be read.')
        })
    }
    read()
    const timer = setInterval(read, NOW_POLL_MS)
    document.addEventListener('visibilitychange', read)
    return () => {
      live = false
      clearInterval(timer)
      document.removeEventListener('visibilitychange', read)
    }
  }, [])

  if (printers === null && error === null) return null
  if (printers !== null && printers.length === 0) return null
  return (
    <section aria-labelledby="now-printing-heading" className="mb-6" data-testid="now-printing">
      <h2 id="now-printing-heading" className="mb-2 text-[13px] font-medium text-ink">
        Now printing
      </h2>
      {error && (
        <p className="mb-2 text-[12px] text-muted" data-testid="now-printing-error">
          {error}
        </p>
      )}
      {printers && (
        <ul className="grid gap-2 sm:grid-cols-2" aria-label="Printers">
          {printers.map((printer) => (
            <PrinterCard key={printer.printer_id} printer={printer} />
          ))}
        </ul>
      )}
    </section>
  )
}

function PrinterCard({ printer }: { printer: PrinterNow }) {
  const activity = ACTIVITY[printer.activity]
  const working = printer.activity === 'printing' || printer.activity === 'paused'
  const hms = printer.hms ?? []
  const progress = printer.progress == null ? null : Math.max(0, Math.min(100, printer.progress))
  return (
    <li
      className="min-w-0 rounded-[6px] border border-line bg-surface px-3 py-2 text-[12.5px]"
      data-testid={`printer-now-${printer.printer_id}`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="truncate font-medium text-ink">{printer.name}</span>
        <span className={`inline-flex shrink-0 items-center rounded-full border px-2 py-px text-[11px] ${activity.tone}`}>
          {activity.label}
        </span>
      </div>
      {printer.error && <p className="mt-1 text-[12px] text-muted">{printer.error}</p>}
      {working && (
        <>
          {printer.job_name && <p className="mt-1 truncate text-ink">{printer.job_name}</p>}
          {progress !== null && (
            <div
              role="progressbar"
              aria-label={`${printer.name} progress`}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(progress)}
              className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-surface-2"
            >
              <div className="h-full bg-accent" style={{ width: `${progress}%` }} />
            </div>
          )}
          <p className="sb-num mt-1 flex flex-wrap gap-x-3 text-[12px] text-muted">
            {progress !== null && <span>{Math.round(progress)}%</span>}
            {printer.layer != null && printer.total_layers ? (
              <span>
                Layer {printer.layer}/{printer.total_layers}
              </span>
            ) : null}
            {printer.remaining_minutes != null && printer.remaining_minutes > 0 && (
              <span>{formatDuration(printer.remaining_minutes * 60)} left</span>
            )}
          </p>
        </>
      )}
      {printer.awaiting_plate_clear && (
        <p className="mt-1 text-[12px] text-muted">Clear the plate before the next print.</p>
      )}
      {hms.length > 0 && (
        <ul
          className={`mt-1.5 space-y-0.5 text-[12px] ${printer.activity === 'paused' ? 'text-warn' : 'text-muted'}`}
          aria-label={`${printer.name} errors`}
          data-testid={`printer-now-hms-${printer.printer_id}`}
        >
          {hms.map((error, index) => (
            <li key={`${index}-${error.code}`}>
              {error.description ?? 'No description from Bambuddy'}{' '}
              <span className="sb-num text-faint">HMS {error.code}</span>
            </li>
          ))}
        </ul>
      )}
      {printer.archive_id != null &&
        (printer.linked ? (
          <Link to={`/prints/${printer.archive_id}`} className="mt-1.5 inline-block text-[12px] text-accent hover:underline">
            View this print
          </Link>
        ) : printer.bambuddy_url ? (
          // Bambuddy has no page per archive, so the link names the page it opens and the
          // archive to look for there sits beside it (#2930).
          <p className="mt-1.5 text-[12px] text-muted">
            <a
              href={bambuddyLink(printer.bambuddy_url)}
              target="_blank"
              rel="noreferrer"
              className="text-accent hover:underline"
            >
              Bambuddy's archives
            </a>{' '}
            · archive {printer.archive_id}
          </p>
        ) : null)}
    </li>
  )
}
