import { useCallback, useEffect, useRef, useState } from 'react'
import { Link } from 'react-router'
import { api, ApiError } from '../../api/client'
import type { CameraFeed, PrinterCamera, PrinterNow } from '../../api/types'
import { bambuddyLink } from '../../lib/bambuddyLinks'
import { formatDuration, formatFullTime } from '../../lib/format'
import { useSubscription } from '../../lib/realtime'
import { NEW_TAB } from '../../lib/embed'
import { CameraIcon, ExternalLinkIcon, RefreshCwIcon, VideoIcon } from 'lucide-react'
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
import { JobPicture } from './JobPicture'
import { LivePlayer } from './LivePlayer'
import { lastSeen, mergeCards, type NowCard } from './nowCards'
import { LanRunControls } from '../print/LanRunControls'
import { PlateWarning } from './PlateWarning'
import { printPath, scadbuddyJobPath } from './prints'
import { QueuedFromScadBuddy } from './QueuedFromScadBuddy'

/** How often the printers are read while the page is visible: the fallback when no
 * `printers` signal comes (realtime down, or a printer only Bambuddy knows). */
export const NOW_POLL_MS = 15_000
/** The least time between two reads the `printers` topic starts: a LAN printer's watch
 * announces about once a second while it prints (`printers/watch.py`), and every read asks
 * Bambuddy about each of its printers. */
export const NOW_SIGNAL_MS = 2_000

const ACTIVITY: Record<PrinterNow['activity'], { label: string; tone: string }> = {
  printing: { label: 'Printing', tone: 'border-primary/30 bg-muted' },
  paused: { label: 'Paused', tone: 'border-warn/40 bg-warn/10 text-warn' },
  idle: { label: 'Idle', tone: 'text-muted-foreground' },
  finished: { label: 'Finished', tone: 'border-ok/40 bg-ok/10 text-ok' },
  failed: { label: 'Failed', tone: 'border-warn/40 bg-warn/10 text-warn' },
  offline: { label: 'Offline', tone: 'border-dashed text-muted-foreground' },
  unknown: { label: 'Unknown', tone: 'text-muted-foreground' },
}

/** What {@link usePrintersNow} gives: the cards as last read, and the last read's error. */
export interface PrintersNowState {
  /** `null` until the first read answers. */
  cards: NowCard[] | null
  error: string | null
}

/**
 * #977 — what each printer is doing now, whoever started the job: a print ScadBuddy
 * queued, one from Bambuddy's library, or one started on the printer. Read every
 * `NOW_POLL_MS` while the page is visible, and at once when it becomes visible again,
 * and (#3823) on a printer's report or change on the `printers` topic, no sooner than
 * `NOW_SIGNAL_MS` after the last read of any kind. #3745 — one read for the whole
 * Prints page: the strip shows the printers, and the list shows their jobs.
 */
export function usePrintersNow(): PrintersNowState {
  const [cards, setCards] = useState<NowCard[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const signalled = useRef<() => void>(() => undefined)
  // A `resync` (the subscription confirmed, or the socket back) is no news here: the
  // poll and the visibility read cover what a dropped socket missed.
  // Only a printer's own events are news here (#4340 review M5).
  useSubscription('printers', (signal) => {
    if (signal !== 'resync' && (signal.kind === 'printer.report' || signal.kind === 'printer.changed')) {
      signalled.current()
    }
  })

  useEffect(() => {
    let live = true
    // A timer read and a visibility read can overlap; only the newest one may land.
    let sent = 0
    /** When the last read started, whatever started it: a signal waits `NOW_SIGNAL_MS` from it. */
    let lastRead = -Infinity
    const read = () => {
      if (document.hidden) return
      lastRead = Date.now()
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
    let pending: ReturnType<typeof setTimeout> | undefined
    signalled.current = () => {
      if (pending !== undefined) return
      pending = setTimeout(() => {
        pending = undefined
        read()
      }, Math.max(0, lastRead + NOW_SIGNAL_MS - Date.now()))
    }
    read()
    const timer = setInterval(read, NOW_POLL_MS)
    document.addEventListener('visibilitychange', read)
    return () => {
      live = false
      clearInterval(timer)
      clearTimeout(pending)
      signalled.current = () => undefined
      document.removeEventListener('visibilitychange', read)
    }
  }, [])

  return { cards, error }
}

/**
 * #3745 — the per-printer status strip: what each printer is, not what it prints. Each
 * card says its activity, which link answered for it (the printer's own LAN report or
 * Bambuddy), a stale read, a failed status read, HMS errors, "clear the plate", a
 * native run's controls and the camera. The jobs are rows of the Prints list
 * (`PrintHistory`); the camera dialog still shows the job beside the image.
 */
export function PrinterStrip({ now }: { now: PrintersNowState }) {
  const { cards, error } = now
  /** #981 — the printer whose camera is open, by id, so its status stays current. */
  // The registry printer whose camera is open (#3823): a Bambuddy-linked one, or one with
  // the live view (#7330).
  const [watching, setWatching] = useState<string | null>(null)

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
      aria-labelledby="printers-now-heading"
      aria-busy={loading || undefined}
      className="mb-6"
      data-testid="now-printing"
    >
      <h2 id="printers-now-heading" className="mb-2 text-sm font-medium">
        Printers
      </h2>
      {loading && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3" data-testid="now-printing-loading">
          <Skeleton className="h-[52px] rounded-xl" />
          <span className="sr-only">Reading the printers</span>
        </div>
      )}
      {error && (
        <p className="mb-2 text-xs text-muted-foreground" data-testid="now-printing-error">
          {error}
        </p>
      )}
      {cards && cards.length > 0 && (
        <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3" aria-label="Printers">
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

/** Which link answered for a card (`PrinterNow.path`); a printer never heard from has none. */
const PATH_LABEL: Record<PrinterNow['path'], string | null> = { native: 'LAN', bambuddy: 'Bambuddy', none: null }
const PATH_TITLE: Record<PrinterNow['path'], string> = {
  native: "From the printer's own report over the LAN",
  bambuddy: 'From Bambuddy',
  none: '',
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
          <span className="flex min-w-0 items-center gap-1.5">
            <span className="truncate font-medium">{printer.name}</span>
            {PATH_LABEL[printer.path] && (
              <span
                className="shrink-0 rounded border px-1 text-[10.5px] text-muted-foreground"
                title={PATH_TITLE[printer.path]}
              >
                {PATH_LABEL[printer.path]}
              </span>
            )}
          </span>
          <Badge variant="outline" className={cn('font-normal', activity.tone)}>
            {activity.label}
          </Badge>
        </div>
        {stale && (
          <p className="mt-0.5 text-[11.5px] text-muted-foreground" data-testid={`printer-now-stale-${printer.printer_id ?? printer.id}`}>
            {lastSeen(card)}
          </p>
        )}
        <PrinterState printer={printer} />
        {/* #4942, #4939 — a native run's prints still to start on this printer. */}
        {printer.lan_run && !stale && <LanRunControls control={printer.lan_run} printerName={printer.name} className="mt-2" />}
        {(printer.camera_live || printer.printer_id !== null) && printer.activity !== 'offline' && !printer.error && !stale && (
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

/**
 * #3745 — a printer's own state on its strip card: a failed status read, "clear the
 * plate" and its HMS errors. Its job is a row of the list, not repeated here.
 */
function PrinterState({ printer }: { printer: PrinterNow }) {
  return (
    <>
      {printer.error && <p className="mt-1 text-xs text-muted-foreground">{printer.error}</p>}
      <PlateWarning printer={printer} testId={`printer-now-plate-${printer.printer_id ?? printer.id}`} />
      <HmsErrors printer={printer} testId={`printer-now-hms-${printer.printer_id ?? printer.id}`} />
    </>
  )
}

/** A working job's progress bar and its figures: printer, percent, layer and time left. */
export function JobProgress({ printer }: { printer: PrinterNow }) {
  const progress = printer.progress == null ? null : Math.max(0, Math.min(100, printer.progress))
  return (
    <>
      {progress !== null && (
        <div
          role="progressbar"
          aria-label={`${printer.name} progress`}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={Math.round(progress)}
          className="relative z-10 mt-1.5 h-1.5 overflow-hidden rounded-full bg-muted"
        >
          <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: `${progress}%` }} />
        </div>
      )}
      <p className="sb-num mt-1 flex flex-wrap gap-x-3 text-xs text-muted-foreground">
        <span>{printer.name}</span>
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
  )
}

/** What the camera dialog shows beside the image: the job, its progress, and the printer's state. */
function JobStatus({ printer }: { printer: PrinterNow }) {
  const working = printer.activity === 'printing' || printer.activity === 'paused'
  return (
    <>
      {printer.error && <p className="mt-1 text-xs text-muted-foreground">{printer.error}</p>}
      {working && (
        <>
          {/* #3742 — ScadBuddy's picture of its own job, else Bambuddy's cover. */}
          <div className="mt-1 flex items-center gap-2">
            <JobPicture src={printer.picture} />
            <div className="min-w-0 flex-1 [&>p]:mt-0">
              <JobLink printer={printer} />
            </div>
          </div>
          {printer.scadbuddy && <QueuedFromScadBuddy job={printer.scadbuddy} className="mt-1 self-start" />}
          <JobProgress printer={printer} />
        </>
      )}
      <PlateWarning printer={printer} />
      <HmsErrors printer={printer} />
    </>
  )
}

function HmsErrors({ printer, testId }: { printer: PrinterNow; testId?: string }) {
  const hms = printer.hms ?? []
  if (hms.length === 0) return null
  return (
    <ul
      className={cn('mt-1.5 space-y-0.5 text-xs', printer.activity === 'paused' ? 'text-warn' : 'text-muted-foreground')}
      aria-label={`${printer.name} errors`}
      data-testid={testId}
    >
      {hms.map((error, index) => (
        <li key={`${index}-${error.code}`}>
          {error.description ?? 'No description from Bambuddy'}{' '}
          <span className="sb-num text-muted-foreground/70">HMS {error.code}</span>
        </li>
      ))}
    </ul>
  )
}

const JOB_LINK = 'font-medium text-primary underline-offset-4 hover:underline'

/**
 * #3743 — the job's name, as a link to where the job is: ScadBuddy's print of an
 * archive ScadBuddy linked; for ScadBuddy's own job (`scadbuddy`), its ScadBuddy page
 * ({@link scadbuddyJobPath}); for any other, its archive in Bambuddy (`/archives/{id}`
 * where Bambuddy has that page, else its archives page with the archive's number, on
 * the hostname framing ScadBuddy, in a new tab, which escapes the frame's sandbox).
 * A job with none of these is its name only.
 */
function JobLink({ printer }: { printer: PrinterNow }) {
  if (!jobName(printer) && !jobLinked(printer)) return null
  return (
    <p className="mt-1 truncate">
      <JobAnchor printer={printer} className={JOB_LINK} />
    </p>
  )
}

/** The job has a page to link: ScadBuddy's, or Bambuddy's. */
function jobLinked(printer: PrinterNow): boolean {
  return (printer.linked && printer.archive_id != null) || printer.scadbuddy != null || printer.bambuddy_url != null
}

/** {@link JobLink}'s link alone, for a list row's heading (#3745). */
export function JobAnchor({ printer, className }: { printer: PrinterNow; className: string }) {
  const name = jobName(printer)
  const inside =
    printer.linked && printer.archive_id != null
      ? printPath(printer.archive_id)
      : printer.scadbuddy
        ? scadbuddyJobPath(printer.scadbuddy)
        : null
  if (inside) {
    return (
      <Link to={inside} className={className}>
        {name ?? 'This print'}
      </Link>
    )
  }
  if (printer.bambuddy_url) {
    // A Bambuddy before 1.3.0 has no page per archive: the server then links its
    // archives page, says so (`bambuddy_archive_page`, #5840), and the archive to look
    // for there is named in the link (#2930).
    const archivePage = printer.bambuddy_archive_page
    const label = name ?? 'This print'
    return (
      <a
        href={bambuddyLink(printer.bambuddy_url)}
        {...NEW_TAB}
        className={className}
        aria-label={
          archivePage ? `${label}, in Bambuddy` : `${label}, archive ${printer.archive_id} in Bambuddy's archives`
        }
      >
        {archivePage ? label : `${label} · archive ${printer.archive_id}`}
        <ExternalLinkIcon aria-hidden="true" className="ml-1 inline size-3 align-baseline" />
      </a>
    )
  }
  return <>{name ?? 'A print'}</>
}

/** What a working job is called: its name, else its archive; `null` with neither. */
export function jobName(printer: PrinterNow): string | null {
  return printer.job_name ?? (printer.archive_id != null ? `Archive ${printer.archive_id}` : null)
}

/**
 * #981 — the printer's live camera, with what it is printing beside it. #7330 — a printer
 * with `camera_live` plays the live view ({@link LivePlayer}), with its feed states and
 * Reconnect beside it. Any other's stream is Bambuddy's MJPEG through ScadBuddy, open only while this dialog is: closing it drops
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
        {printer &&
          (printer.camera_live ? (
            <LiveCameraView key={printer.id} printer={printer} />
          ) : (
            printer.printer_id !== null && (
              <CameraView key={printer.id} printer={printer} bambuddyId={printer.printer_id} />
            )
          ))}
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

/** #7330 — the live view, with the job and the camera's feed states beside it. */
function LiveCameraView({ printer }: { printer: PrinterNow }) {
  // A reconnect restarts the relay's pull, so the player starts over too, once the pull is back.
  const [attempt, setAttempt] = useState(0)
  const restart = useCallback(() => setAttempt((count) => count + 1), [])
  return (
    <div className="grid gap-4 md:grid-cols-[minmax(0,1fr)_16rem]" data-testid="printer-camera">
      <div className="min-w-0">
        <LivePlayer key={attempt} printerId={printer.id} />
      </div>
      <div className="flex min-w-0 flex-col text-[12.5px]" data-testid="printer-camera-status">
        <Badge variant="outline" className={cn('font-normal', ACTIVITY[printer.activity].tone)}>
          {ACTIVITY[printer.activity].label}
        </Badge>
        <JobStatus printer={printer} />
        <CameraFeeds printerId={printer.id} onReconnect={restart} />
      </div>
    </div>
  )
}

function relayFeed(camera: PrinterCamera): CameraFeed | undefined {
  return camera.feeds.find((feed) => feed.part === 'relay')
}

const FEED_PART: Record<CameraFeed['part'], string> = { relay: 'Live feed', recorder: 'Recording' }
const FEED_STATE: Record<CameraFeed['state'], { label: string; dot: string }> = {
  pulling: { label: 'Receiving', dot: 'bg-ok' },
  retrying: { label: 'Retrying', dot: 'bg-warn' },
  stalled: { label: 'Stalled', dot: 'bg-warn' },
  stopped: { label: 'Stopped', dot: 'bg-muted-foreground' },
}

/**
 * How long the player waits for the relay to report `pulling` again after a reconnect
 * before it starts over anyway.
 */
const RECONNECT_WAIT_MS = 20_000

/**
 * #7330 — each part's feed state (`GET /printers/{id}/camera`), read again on the
 * printer's camera events, and Reconnect, which asks the relay to restart its pull.
 * `onReconnect` runs when the relay next reports `pulling` after the reconnect (a report
 * newer than the one read when it was sent), or after `RECONNECT_WAIT_MS`. A player that
 * started at once would attach to the old pull while the relay tears it down.
 */
function CameraFeeds({ printerId, onReconnect }: { printerId: string; onReconnect: () => void }) {
  const [camera, setCamera] = useState<PrinterCamera | null>(null)
  const [readError, setReadError] = useState<string | null>(null)
  const [sending, setSending] = useState(false)
  const [reconnect, setReconnect] = useState<{ sent: true } | { refused: string } | null>(null)
  const read = useRef<() => void>(() => undefined)
  const last = useRef<PrinterCamera | null>(null)
  const waiting = useRef<{ since: string | null; timer: ReturnType<typeof setTimeout> } | null>(null)
  const restart = useRef(onReconnect)
  useEffect(() => {
    restart.current = onReconnect
  }, [onReconnect])
  // `camera.feed` and `camera.reconnect` publish on `printer:<id>`; its reports do too,
  // about once a second while it prints, and are no news here.
  useSubscription(`printer:${printerId}`, (signal) => {
    if (signal === 'resync' || signal.kind.startsWith('camera.')) read.current()
  })
  useEffect(() => {
    let live = true
    let sent = 0
    const done = () => {
      if (waiting.current) clearTimeout(waiting.current.timer)
      waiting.current = null
    }
    read.current = () => {
      const mine = ++sent
      const current = () => live && mine === sent
      api
        .printerCamera(printerId)
        .then((answer) => {
          if (!current()) return
          last.current = answer
          setCamera(answer)
          setReadError(null)
          const relay = relayFeed(answer)
          if (waiting.current && relay?.state === 'pulling' && relay.updated_at !== waiting.current.since) {
            done()
            restart.current()
          }
        })
        .catch((cause: unknown) => {
          if (current()) setReadError(cause instanceof ApiError ? cause.detail : 'The camera state could not be read.')
        })
    }
    read.current()
    return () => {
      live = false
      read.current = () => undefined
      last.current = null
      done()
    }
  }, [printerId])

  const send = () => {
    setSending(true)
    setReconnect(null)
    api
      .reconnectPrinterCamera(printerId)
      .then(() => {
        setReconnect({ sent: true })
        if (waiting.current) clearTimeout(waiting.current.timer)
        const since = last.current ? (relayFeed(last.current)?.updated_at ?? null) : null
        const timer = setTimeout(() => {
          waiting.current = null
          restart.current()
        }, RECONNECT_WAIT_MS)
        waiting.current = { since, timer }
      })
      .catch((cause: unknown) =>
        setReconnect({
          // A problem with no detail still says what it is: its title.
          refused:
            (cause instanceof ApiError && (cause.detail || cause.problem.title)) || 'The reconnect could not be sent.',
        }),
      )
      .finally(() => setSending(false))
  }

  return (
    <section aria-labelledby={`camera-feeds-${printerId}`} className="mt-3 flex flex-col border-t pt-3">
      <h3 id={`camera-feeds-${printerId}`} className="mb-1.5 text-xs font-medium">
        Camera
      </h3>
      {readError ? (
        <p className="text-xs text-muted-foreground">{readError}</p>
      ) : camera === null ? (
        <p className="text-xs text-muted-foreground">Reading the camera</p>
      ) : camera.feeds.length === 0 ? (
        <p className="text-xs text-muted-foreground">No feed has reported yet.</p>
      ) : (
        <ul aria-label="Camera feeds" className="space-y-1.5">
          {camera.feeds.map((feed) => (
            <li
              key={feed.part}
              className="flex items-start gap-2"
              data-testid={`camera-feed-${feed.part}`}
              title={`Since ${formatFullTime(Date.parse(feed.updated_at))}`}
            >
              <span aria-hidden="true" className={cn('mt-[5px] size-2 shrink-0 rounded-full', FEED_STATE[feed.state].dot)} />
              <span className="min-w-0">
                {FEED_PART[feed.part]}{' '}
                <span className="text-muted-foreground">{FEED_STATE[feed.state].label}</span>
                {feed.detail && (
                  <span className="block text-[11.5px] break-words text-muted-foreground">{feed.detail}</span>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
      <Button variant="outline" size="sm" className="mt-2 self-start" disabled={sending} onClick={send}>
        <RefreshCwIcon aria-hidden="true" />
        Reconnect
      </Button>
      {reconnect && 'sent' in reconnect && (
        <p className="mt-1 text-xs text-muted-foreground" role="status">
          Reconnecting the camera.
        </p>
      )}
      {reconnect && 'refused' in reconnect && (
        <p className="mt-1 text-xs text-warn" role="alert">
          {reconnect.refused}
        </p>
      )}
    </section>
  )
}
