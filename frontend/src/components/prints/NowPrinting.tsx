import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { PrinterNow } from '../../api/types'
import { bambuddyLink } from '../../lib/bambuddyLinks'
import { formatDuration } from '../../lib/format'
import { CameraIcon, RefreshCwIcon, VideoIcon } from 'lucide-react'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { mergeCards, type NowCard } from './nowCards'
import { PlateWarning } from './PlateWarning'

/** How often the printers are read while the page is visible. */
export const NOW_POLL_MS = 15_000

const ACTIVITY: Record<PrinterNow['activity'], { label: string; tone: string }> = {
  printing: { label: 'Printing', tone: 'border-primary/30 bg-muted' },
  paused: { label: 'Paused', tone: 'border-warn/40 bg-warn/10 text-warn' },
  idle: { label: 'Idle', tone: 'text-muted-foreground' },
  finished: { label: 'Finished', tone: 'border-ok/40 bg-ok/10 text-ok' },
  failed: { label: 'Failed', tone: 'border-warn/40 bg-warn/10 text-warn' },
  offline: { label: 'Offline', tone: 'border-dashed text-muted-foreground' },
  unknown: { label: 'Unknown', tone: 'text-muted-foreground' },
}

/**
 * #977 — what each printer is doing now, whoever started the job: a print ScadBuddy
 * queued, one from Bambuddy's library, or one started on the printer. Read every
 * `NOW_POLL_MS` while the page is visible, and at once when it becomes visible again.
 */
export function NowPrinting() {
  const [cards, setCards] = useState<NowCard[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  /** #981 — the printer whose camera is open, by id, so its status stays current. */
  // The registry printer whose camera is open (#3823): only a Bambuddy-linked one has one.
  const [watching, setWatching] = useState<string | null>(null)

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
          setCards((previous) => mergeCards(previous, now.printers ?? [], Date.now()))
          setError(null)
        })
        .catch((cause: unknown) => {
          if (!current()) return
          const why = cause instanceof ApiError ? cause.detail : 'The printers could not be read.'
          setError(why)
          setCards((previous) => previous && previous.map((card) => ({ ...card, stale: card.stale ?? why })))
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
  // reopen on its own when the printer comes back. One whose card went stale has
  // dropped out as far as the camera is concerned.
  useEffect(() => {
    if (
      watching !== null &&
      cards !== null &&
      !cards.some((card) => card.printer.id === watching && card.stale === null)
    ) {
      setWatching(null)
    }
  }, [cards, watching])

  if (cards !== null && cards.length === 0 && error === null) return null
  // #3632 — a placeholder while the first read is out, so the strip is visibly coming
  // rather than absent: that read waits on Bambuddy for every printer.
  const loading = cards === null && error === null
  return (
    <section
      aria-labelledby="now-printing-heading"
      aria-busy={loading || undefined}
      className="mb-6"
      data-testid="now-printing"
    >
      <h2 id="now-printing-heading" className="mb-2 text-sm font-medium">
        Now printing
      </h2>
      {loading && (
        <div className="grid gap-3 sm:grid-cols-2" data-testid="now-printing-loading">
          <Skeleton className="h-[88px] rounded-xl" />
          <span className="sr-only">Reading the printers</span>
        </div>
      )}
      {error && (
        <p className="mb-2 text-xs text-muted-foreground" data-testid="now-printing-error">
          {error}
        </p>
      )}
      {cards && cards.length > 0 && (
        <ul className="grid gap-3 sm:grid-cols-2" aria-label="Printers">
          {cards.map((card) => (
            <PrinterCard key={card.printer.id} card={card} onCamera={() => setWatching(card.printer.id)} />
          ))}
        </ul>
      )}
      <CameraDialog
        printer={cards?.find((card) => card.printer.id === watching)?.printer ?? null}
        onClose={() => setWatching(null)}
      />
    </section>
  )
}

function PrinterCard({ card, onCamera }: { card: NowCard; onCamera: () => void }) {
  const { printer, stale } = card
  const activity = ACTIVITY[printer.activity]
  return (
    <li className="min-w-0" data-testid={`printer-now-${printer.printer_id ?? printer.id}`}>
      <Card
        size="sm"
        className={cn('h-full gap-0 px-3 text-[12.5px]', stale && 'border border-dashed ring-0')}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="truncate font-medium">{printer.name}</span>
          <Badge variant="outline" className={cn('font-normal', activity.tone)}>
            {activity.label}
          </Badge>
        </div>
        {stale && (
          <p className="mt-0.5 text-[11.5px] text-muted-foreground" data-testid={`printer-now-stale-${printer.printer_id ?? printer.id}`}>
            Last seen at {new Date(card.seenAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })} · {stale}
          </p>
        )}
        <JobStatus printer={printer} />
        {printer.printer_id !== null && printer.activity !== 'offline' && !printer.error && !stale && (
          <Button
            variant="outline"
            size="sm"
            onClick={onCamera}
            className="mt-2 self-start"
            aria-label={`Camera of ${printer.name}`}
          >
            <CameraIcon aria-hidden="true" />
            Camera
          </Button>
        )}
      </Card>
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
      {printer.error && <p className="mt-1 text-xs text-muted-foreground">{printer.error}</p>}
      {working && (
        <>
          {printer.job_name && <p className="mt-1 truncate">{printer.job_name}</p>}
          {progress !== null && (
            <div
              role="progressbar"
              aria-label={`${printer.name} progress`}
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={Math.round(progress)}
              className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted"
            >
              <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${progress}%` }} />
            </div>
          )}
          <p className="sb-num mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
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
      <PlateWarning printer={printer} testId={`printer-now-plate-${printer.printer_id ?? printer.id}`} />
      {hms.length > 0 && (
        <ul
          className={cn('mt-1.5 space-y-0.5 text-xs', printer.activity === 'paused' ? 'text-warn' : 'text-muted-foreground')}
          aria-label={`${printer.name} errors`}
          data-testid={`printer-now-hms-${printer.printer_id ?? printer.id}`}
        >
          {hms.map((error, index) => (
            <li key={`${index}-${error.code}`}>
              {error.description ?? 'No description from Bambuddy'}{' '}
              <span className="sb-num text-muted-foreground/70">HMS {error.code}</span>
            </li>
          ))}
        </ul>
      )}
      {printer.archive_id != null &&
        (printer.linked ? (
          <Button asChild variant="link" size="xs" className="mt-1 h-auto self-start px-0">
            <Link to={`/prints/${printer.archive_id}`}>View this print</Link>
          </Button>
        ) : printer.bambuddy_url ? (
          // Bambuddy has no page per archive, so the link names the page it opens and the
          // archive to look for there sits beside it (#2930).
          <p className="mt-1.5 text-xs text-muted-foreground">
            <a
              href={bambuddyLink(printer.bambuddy_url)}
              target="_blank"
              rel="noreferrer"
              className="font-medium text-primary underline-offset-4 hover:underline"
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
    <Dialog open={printer !== null} onOpenChange={(next) => !next && onClose()}>
      <DialogContent
        showCloseButton={false}
        className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-5xl"
      >
        <DialogHeader>
          <DialogTitle>{printer ? `${printer.name} camera` : 'Camera'}</DialogTitle>
          <DialogDescription className="sr-only">The printer's live camera and what it is printing.</DialogDescription>
        </DialogHeader>
        {/* Keyed per printer, so another printer's camera starts live, never in the last
            one's snapshot mode (which would fetch a snapshot first). */}
        {printer && printer.printer_id !== null && (
          <CameraView key={printer.id} printer={printer} bambuddyId={printer.printer_id} />
        )}
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CameraView({ printer, bambuddyId }: { printer: PrinterNow; bambuddyId: number }) {
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
            src={api.printerCameraStreamUrl(bambuddyId)}
            alt={`Live camera of ${printer.name}`}
            className="w-full rounded-lg border bg-muted"
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
              src={api.printerCameraUrl(bambuddyId, at)}
              alt={`Camera snapshot of ${printer.name}`}
              className="w-full rounded-lg border bg-muted"
            />
            <p className="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted-foreground" role="status">
              The live stream stopped; this is a snapshot.
              <Button variant="outline" size="sm" onClick={() => setAt(Date.now())}>
                <RefreshCwIcon aria-hidden="true" />
                Refresh
              </Button>
              <Button variant="outline" size="sm" onClick={() => setLive(true)}>
                <VideoIcon aria-hidden="true" />
                Try live again
              </Button>
            </p>
          </>
        )}
      </div>
      <div className="flex min-w-0 flex-col text-[12.5px]" data-testid="printer-camera-status">
        <Badge variant="outline" className={cn('font-normal', ACTIVITY[printer.activity].tone)}>
          {ACTIVITY[printer.activity].label}
        </Badge>
        <JobStatus printer={printer} />
      </div>
    </div>
  )
}
