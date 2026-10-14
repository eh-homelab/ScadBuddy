import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { PrinterNow } from '../../api/types'
import { bambuddyLink } from '../../lib/bambuddyLinks'
import { formatDuration } from '../../lib/format'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'

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
  /** #981 — the printer whose camera is open, by id, so its status stays current. */
  const [watching, setWatching] = useState<number | null>(null)

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

  // #2752 review: a printer that drops out closes its camera for good; it does not
  // reopen on its own when the printer comes back.
  useEffect(() => {
    if (watching !== null && printers !== null && !printers.some((printer) => printer.printer_id === watching)) {
      setWatching(null)
    }
  }, [printers, watching])

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
            <PrinterCard key={printer.printer_id} printer={printer} onCamera={() => setWatching(printer.printer_id)} />
          ))}
        </ul>
      )}
      <CameraDialog
        printer={printers?.find((printer) => printer.printer_id === watching) ?? null}
        onClose={() => setWatching(null)}
      />
    </section>
  )
}

function PrinterCard({ printer, onCamera }: { printer: PrinterNow; onCamera: () => void }) {
  const activity = ACTIVITY[printer.activity]
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
      <JobStatus printer={printer} />
      {printer.activity !== 'offline' && !printer.error && (
        <Button variant="ghost" size="sm" onClick={onCamera} className="mt-1.5" aria-label={`Camera of ${printer.name}`}>
          Camera
        </Button>
      )}
    </li>
  )
}

/** What the printer is doing: the job, its progress, HMS errors and where it is recorded. */
function JobStatus({ printer }: { printer: PrinterNow }) {
  const working = printer.activity === 'printing' || printer.activity === 'paused'
  const hms = printer.hms ?? []
  const progress = printer.progress == null ? null : Math.max(0, Math.min(100, printer.progress))
  return (
    <>
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
    </>
  )
}

/**
 * #981 — the printer's live camera, with what it is printing beside it. The stream is
 * Bambuddy's MJPEG through ScadBuddy, open only while this dialog is: closing it drops
 * the image, which ends the request and so Bambuddy's stream. A stream that is refused
 * or fails falls back to a snapshot with Refresh. One that stalls mid-stream is ended
 * cleanly by the backend, which browsers do not report as an error, so it keeps its
 * last frame; Close and reopen to try again. Read only.
 */
function CameraDialog({ printer, onClose }: { printer: PrinterNow | null; onClose: () => void }) {
  return (
    <Dialog
      open={printer !== null}
      title={printer ? `${printer.name} camera` : 'Camera'}
      onClose={onClose}
      size="wide"
      footer={
        <Button variant="ghost" onClick={onClose}>
          Close
        </Button>
      }
    >
      {/* Keyed per printer, so another printer's camera starts live, never in the last
          one's snapshot mode (which would fetch a snapshot first). */}
      {printer && <CameraView key={printer.printer_id} printer={printer} />}
    </Dialog>
  )
}

function CameraView({ printer }: { printer: PrinterNow }) {
  const [live, setLive] = useState(true)
  const [at, setAt] = useState(() => Date.now())
  const leaving = useRef(false)
  /**
   * Ends a live `<img>`'s request as it leaves (#3207): Chromium can keep a
   * `multipart/x-mixed-replace` image's request open for a while after the element is
   * removed, which would keep Bambuddy's stream subscribed after the dialog closed.
   * Emptying `src` aborts it, and can fire `error`, which is then not a fallback.
   */
  const dropStreamOnUnmount = useCallback((img: HTMLImageElement | null) => {
    leaving.current = false
    return () => {
      leaving.current = true
      if (img) img.src = ''
    }
  }, [])
  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_16rem]" data-testid="printer-camera">
      <div className="min-w-0">
        {live ? (
          <img
            ref={dropStreamOnUnmount}
            src={api.printerCameraStreamUrl(printer.printer_id)}
            alt={`Live camera of ${printer.name}`}
            className="w-full rounded-[6px] border border-line bg-surface-2"
            onError={() => {
              if (leaving.current) return
              // A fresh snapshot each fallback: an old `?at=` may be a cached old frame.
              setAt(Date.now())
              setLive(false)
            }}
          />
        ) : (
          <>
            <img
              src={api.printerCameraUrl(printer.printer_id, at)}
              alt={`Camera snapshot of ${printer.name}`}
              className="w-full rounded-[6px] border border-line bg-surface-2"
            />
            <p className="mt-2 flex flex-wrap items-center gap-2 text-[12px] text-muted" role="status">
              The live stream stopped; this is a snapshot.
              <Button variant="ghost" size="sm" onClick={() => setAt(Date.now())}>
                Refresh
              </Button>
              <Button variant="ghost" size="sm" onClick={() => setLive(true)}>
                Try live again
              </Button>
            </p>
          </>
        )}
      </div>
      <div className="min-w-0 text-[12.5px]" data-testid="printer-camera-status">
        <p className="font-medium text-ink">{ACTIVITY[printer.activity].label}</p>
        <JobStatus printer={printer} />
      </div>
    </div>
  )
}
