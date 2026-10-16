import { Fragment, Suspense, lazy, useMemo, useRef, useState, type ReactNode } from 'react'
import { ChevronRightIcon, ExternalLinkIcon, PrinterIcon, SlidersHorizontalIcon, TriangleAlertIcon } from 'lucide-react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { Job, PrintDetail, PrinterMedia } from '../api/types'
import { MediaCarousel } from '../components/media/MediaCarousel'
import { MediaLightbox } from '../components/media/MediaLightbox'
import type { Slide } from '../components/media/slides'
import { PrintAgainDialog } from '../components/PrintAgainDialog'
import { PrintAttachments } from '../components/prints/PrintAttachments'
import { ParamsTable, PrintFiles } from '../components/prints/PrintFiles'
import { libraryPrintsPath, printerLabel } from '../components/prints/prints'
import { PrintStatus } from '../components/prints/PrintStatus'
import { DELETED_STATUS, isUnfinished, statusLabel } from '../components/prints/status'
import { Hint } from '../components/Hint'
import { Spinner } from '../components/ui/Spinner'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { modelPath } from '../lib/deeplink'
import { USER_ONLY } from '../agent/dom'
import { isEmbedded, openExternal } from '../lib/embed'
import { formatBytes, formatDuration } from '../lib/format'
import { useAsync } from '../lib/useAsync'
import { fineLength, useDisplayUnit } from '../lib/units'

// The 3D viewer is three.js, the biggest thing in the bundle: loaded only when shown.
const Preview = lazy(async () => ({ default: (await import('../components/Preview')).Preview }))

type Run = PrintDetail['outcome']['runs'][number]

function formatWhen(iso: string | null | undefined): string | null {
  if (!iso) return null
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString()
}

function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
}

/** A video attachment's type, from the extension its bytes gave it (#309). */
function videoType(filename: string): string {
  return filename.toLowerCase().endsWith('.webm') ? 'video/webm' : 'video/mp4'
}

/** The print's photos, timelapse, plate image and attachments, in that order. */
function gallerySlides(print: PrintDetail): Slide[] {
  const { media } = print
  const slides: Slide[] = []
  // The photo the printer took as the print finished leads; the others follow.
  if (media.finish_photo) {
    slides.push({
      key: `photo-${media.finish_photo.name}`,
      kind: 'image',
      src: media.finish_photo.url,
      alt: 'Finish photo',
      caption: 'Taken by the printer when the print finished',
    })
  }
  media.photos.forEach((photo, index) => {
    slides.push({ key: `photo-${photo.name}`, kind: 'image', src: photo.url, alt: `Photo ${index + 1}` })
  })
  if (media.timelapse) {
    slides.push({
      key: 'timelapse',
      kind: 'video',
      src: media.timelapse.url,
      poster: media.timelapse.poster_url,
      alt: 'Timelapse',
      contentType: 'video/mp4',
    })
  }
  for (const plate of media.plate_thumbnails) {
    slides.push({
      key: `plate-${plate.index}`,
      kind: 'image',
      src: plate.url,
      alt: `${plate.name ?? `Plate ${plate.index}`} as sliced`,
      // #2055 — the plate by what it holds, when that is known.
      caption: plate.name ? `${plate.name}, as sliced` : "The slicer's plate image",
    })
  }
  media.attachments.forEach((attachment, index) => {
    slides.push({
      key: `attachment-${attachment.id}`,
      kind: attachment.kind === 'video' ? 'video' : 'image',
      src: attachment.url,
      alt: attachment.caption ?? `Attachment ${index + 1}`,
      caption: attachment.caption ?? undefined,
      contentType: attachment.kind === 'video' ? videoType(attachment.filename) : undefined,
    })
  })
  return slides
}

export function PrintDetailPage() {
  const { archiveId = '' } = useParams()
  // An id that is not one would be refused as malformed; it is not a print either way (#310).
  const state = useAsync(
    () =>
      /^[1-9]\d{0,14}$/.test(archiveId)
        ? api.getPrint(Number(archiveId))
        : Promise.reject(new Error(`archive ${archiveId} is not a print of any ScadBuddy output`)),
    [archiveId],
  )
  const print = state.data

  if (state.loading) {
    return (
      <div role="status" aria-label="Loading the print" className="mx-auto max-w-6xl px-4 py-6">
        <span className="sr-only">Loading the print</span>
        <Skeleton className="h-3 w-40" />
        <Skeleton className="mt-3 mb-5 h-6 w-64" />
        <div className="grid gap-4 lg:grid-cols-2">
          <Skeleton className="h-72 rounded-xl" />
          <Skeleton className="h-72 rounded-xl" />
        </div>
      </div>
    )
  }
  if (state.error || !print) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-base font-medium">No such print</h1>
        <p className="mt-2 text-sm text-muted-foreground">{state.error?.message}</p>
        <Button asChild variant="outline" className="mt-4">
          <Link to="/prints">Back to the prints</Link>
        </Button>
      </div>
    )
  }
  return <PrintView print={print} reload={state.refresh} />
}

function PrintView({ print, reload }: { print: PrintDetail; reload: () => void }) {
  const deleted = print.status === DELETED_STATUS
  const [reprinting, setReprinting] = useState(false)
  const title = print.output_name ?? `Print ${print.archive_id}`

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <nav aria-label="Breadcrumb" className="mb-2 flex items-baseline gap-1.5 text-xs text-muted-foreground">
          <Link to="/prints" className="hover:text-foreground">
            Prints
          </Link>
          <ChevronRightIcon aria-hidden="true" className="size-3 self-center" />
          {print.slug !== null ? (
            <Link to={modelPath(print.slug)} className="hover:text-foreground">
              {print.slug}
            </Link>
          ) : print.library_file_id !== null ? (
            // #1755 — the file by its name, linking to every print of it.
            <Link to={libraryPrintsPath(print.library_file_id)} className="hover:text-foreground">
              {print.library_file_name ?? 'Bambuddy library file'}
            </Link>
          ) : (
            <span>Bambuddy library file</span>
          )}
        </nav>
        <header className="mb-5 flex flex-wrap items-center gap-3">
          <h1 className="text-xl font-semibold tracking-tight">{title}</h1>
          <PrintStatus status={print.status} />
          <span className="sb-num text-xs text-muted-foreground">Archive #{print.archive_id}</span>
          <div className="ml-auto flex flex-wrap gap-2">
            {print.provenance && (
              <Button asChild variant="outline">
                <Link to={print.provenance.edit_url}>
                  <SlidersHorizontalIcon aria-hidden="true" />
                  Customize from this
                </Link>
              </Button>
            )}
            {/* #2736 — not while it is still going: its outcome is not known yet. */}
            {!deleted && !isUnfinished(print.status) && (
              <Button onClick={() => setReprinting(true)}>
                <PrinterIcon aria-hidden="true" />
                Print again
              </Button>
            )}
            {print.links.bambuddy_url && (
              <Button variant="outline" onClick={() => openExternal(print.links.bambuddy_url as string)}>
                <ExternalLinkIcon aria-hidden="true" />
                Open in Bambuddy
              </Button>
            )}
          </div>
        </header>

        {deleted && (
          <Alert role="status" className="mb-4 border-warn/40 bg-warn/10 text-warn">
            <TriangleAlertIcon aria-hidden="true" />
            <AlertDescription className="text-warn">
              This print was deleted in Bambuddy. Its photos, timelapse and sliced file went with
              it{print.provenance ? '; ScadBuddy still has the parameters and its own files.' : '.'}
            </AlertDescription>
          </Alert>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          {!deleted && <GallerySection print={print} />}
          <RenderSection print={print} />
          {!deleted && <TimelapseSection print={print} onPulled={reload} />}
          {!deleted && (
            <Section title="Photos and videos">
              <PrintAttachments print={print} onChanged={reload} />
            </Section>
          )}
          <OutcomeSection print={print} />
          {print.provenance ? (
            <ProvenanceSection provenance={print.provenance} />
          ) : (
            <Section title="Provenance">
              {/* #1860 — say why there is none, rather than leave the section out. */}
              <p className="text-[13px] text-muted-foreground">
                {print.library_file_id !== null
                  ? 'Printed from a Bambuddy library file, not a ScadBuddy template: there is no template, revision or parameters.'
                  : 'The ScadBuddy output this print came from is not known.'}
              </p>
            </Section>
          )}
          {print.slicing && <SlicingSection slicing={print.slicing} />}
          {print.record && <RecordSection record={print.record} />}
          <FilesSection print={print} />
        </div>
      </div>
      {!deleted && (
        <PrintAgainDialog open={reprinting} print={print} onClose={() => setReprinting(false)} />
      )}
    </div>
  )
}

/** A section of the print, as a card: a region named by its heading. */
function Section({ title, children, wide }: { title: string; children: ReactNode; wide?: boolean }) {
  const heading = `print-section-${title.toLowerCase().replace(/\W+/g, '-')}`
  return (
    <Card role="region" aria-labelledby={heading} className={cn('min-w-0 gap-3', wide && 'lg:col-span-2')}>
      <CardHeader>
        <CardTitle>
          <h2 id={heading} className="text-sm font-medium">
            {title}
          </h2>
        </CardTitle>
      </CardHeader>
      <CardContent className="min-w-0">{children}</CardContent>
    </Card>
  )
}

function GallerySection({ print }: { print: PrintDetail }) {
  const slides = useMemo(() => gallerySlides(print), [print])
  const [open, setOpen] = useState<number | null>(null)
  return (
    <Section title="Gallery">
      <MediaCarousel
        slides={slides}
        onOpen={setOpen}
        label={`Print ${print.archive_id}`}
        fallback={<p className="text-[13px] text-muted-foreground">Bambuddy has no photos of this print.</p>}
      />
      <MediaLightbox slides={slides} index={open} onClose={() => setOpen(null)} />
    </Section>
  )
}

/**
 * ScadBuddy's view of what was printed, beside the photos for comparison: the output's
 * render, or the library file's plate, read from its 3MF as a print of it reads it (#1753).
 */
function RenderSection({ print }: { print: PrintDetail }) {
  const glb = print.files.find((file) => file.kind === 'preview_glb')
  const output = useAsync(
    async () => (glb && print.output_id ? await api.getOutput(print.output_id).catch(() => null) : null),
    [print.output_id, glb?.url],
  )
  const job = useMemo<Job | undefined>(() => {
    const subject =
      print.output_id !== null && print.slug !== null
        ? { id: print.output_id, slug: print.slug }
        : print.library_file_id !== null
          ? { id: `library-${print.library_file_id}`, slug: '' }
          : null
    if (!glb || !subject) return undefined
    return {
      ...subject,
      status: 'done',
      created_at: output.data?.created_at ?? '',
      preview_url: glb.url,
      bbox_mm: output.data?.bbox_mm ?? null,
      colors: output.data?.colors ?? null,
    }
  }, [glb, print.output_id, print.slug, print.library_file_id, output.data])
  // #2731 — a library file's plate mesh is the whole file's geometry for that plate (11 MB
  // for one print): its image first, and the mesh only when asked for.
  // Keyed by the mesh's URL (#2915): another print shown in this same section starts on
  // its image again, rather than loading its mesh unasked.
  const [meshAskedFor, setMeshAskedFor] = useState<string | null>(null)
  // #2915 — the plate image that would not load: shown as a placeholder, not a broken image.
  const [brokenImage, setBrokenImage] = useState<string | null>(null)
  if (!glb || !job) return null
  const meshAsked = meshAskedFor === glb.url
  const library = print.library_file_id
  const plate = Number(new URL(glb.url, window.location.origin).searchParams.get('plate') ?? '1')
  return (
    <Section title={print.output_id !== null ? 'ScadBuddy render' : 'Library file'}>
      <div className="relative aspect-[4/3] overflow-hidden rounded-lg">
        {library !== null && !meshAsked ? (
          <>
            {brokenImage === api.libraryPlateThumbnailUrl(library, plate) ? (
              <p className="flex size-full items-center justify-center bg-ui-muted text-xs text-muted-foreground">
                No image of plate {plate}
              </p>
            ) : (
              <img
                src={api.libraryPlateThumbnailUrl(library, plate)}
                alt={`Plate ${plate} of the library file`}
                className="size-full bg-ui-muted object-contain"
                onError={() => setBrokenImage(api.libraryPlateThumbnailUrl(library, plate))}
              />
            )}
            <Button
              size="sm"
              variant="secondary"
              className="absolute right-2 bottom-2 shadow-sm"
              onClick={() => setMeshAskedFor(glb.url)}
            >
              Show in 3D
            </Button>
          </>
        ) : (
          !output.loading && (
            <Suspense fallback={null}>
              <Preview job={job} rendering={false} />
            </Suspense>
          )
        )}
      </div>
    </Section>
  )
}

function TimelapseSection({ print, onPulled }: { print: PrintDetail; onPulled: () => void }) {
  const timelapse = print.media.timelapse
  const video = useRef<HTMLVideoElement>(null)
  const [looking, setLooking] = useState(false)
  const [onPrinter, setOnPrinter] = useState<PrinterMedia | null>(null)
  const [pulling, setPulling] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  // #2360 — read after the page shows: Bambuddy can take tens of seconds over them.
  const stills = useAsync(
    async () => (timelapse ? await api.getTimelapseFrames(print.archive_id).catch(() => null) : null),
    [print.archive_id, timelapse?.frames_url],
  )

  async function look() {
    setLooking(true)
    setError(null)
    try {
      const detail = await api.getPrint(print.archive_id, { printerMedia: true })
      setOnPrinter(detail.printer_media ?? { archive_id: print.archive_id, remote_files: [], warnings: [] })
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not ask the printer.')
    } finally {
      setLooking(false)
    }
  }

  async function pull(filename: string) {
    setPulling(filename)
    setError(null)
    try {
      await api.pullTimelapse(print.archive_id, filename)
      setOnPrinter(null)
      onPulled()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not pull the timelapse.')
    } finally {
      setPulling(null)
    }
  }

  if (timelapse) {
    const info = stills.data?.info ?? null
    const frames = stills.data?.frames ?? []
    return (
      <Section title="Timelapse">
        {/* Native controls against the Range proxy: a seek fetches only those bytes.
            No fullscreen control inside Bambuddy's frame, which does not grant it. */}
        <video
          ref={video}
          src={timelapse.url}
          poster={timelapse.poster_url}
          controls
          controlsList={isEmbedded() ? 'nofullscreen' : undefined}
          playsInline
          preload="metadata"
          className="aspect-video w-full rounded-lg bg-black"
        />
        {info && (
          <p className="sb-num mt-1.5 text-xs text-muted-foreground">
            {clock(info.duration)} · {info.width}×{info.height} · {formatBytes(info.file_size)}
          </p>
        )}
        {frames.length > 1 || (frames.length === 1 && info) ? (
          <ul aria-label="Timelapse frames" className="mt-2 flex gap-1.5 overflow-x-auto">
            {frames.map((frame) => (
              <li key={frame.timestamp} className="shrink-0">
                <Button
                  variant="ghost"
                  aria-label={`Go to ${clock(frame.timestamp)}`}
                  onClick={() => {
                    if (video.current) video.current.currentTime = frame.timestamp
                  }}
                  className="h-auto overflow-hidden rounded-md p-0"
                >
                  <img src={frame.url} alt="" loading="lazy" className="h-12 w-auto" />
                </Button>
              </li>
            ))}
          </ul>
        ) : null}
      </Section>
    )
  }

  const timelapses = (onPrinter?.remote_files ?? []).filter((file) => file.kind === 'timelapse')
  const forbidden = (onPrinter?.warnings ?? []).includes('printer_files_forbidden')
  return (
    <Section title="Timelapse">
      <p className="text-[13px] text-muted-foreground">Bambuddy has no timelapse of this print.</p>
      {onPrinter === null ? (
        <Button size="sm" variant="outline" className="mt-2" onClick={() => void look()} disabled={looking}>
          {looking && <Spinner />}
          Look on the printer
        </Button>
      ) : forbidden ? (
        <p className="mt-2 text-xs text-warn">
          The Bambuddy API key cannot list the printer&apos;s files: that needs the Control
          Printer permission.
        </p>
      ) : timelapses.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">No timelapse on the printer for this print.</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {timelapses.map((file) => (
            <li key={file.path} className="flex flex-wrap items-center gap-2">
              <span className="sb-num min-w-0 truncate text-xs">{file.name}</span>
              <span className="sb-num text-xs text-muted-foreground">{formatBytes(file.size)}</span>
              <Button
                size="sm"
                variant="outline"
                className="ml-auto"
                onClick={() => void pull(file.name)}
                disabled={pulling !== null}
                {...USER_ONLY}
              >
                {pulling === file.name && <Spinner />}
                Pull timelapse from printer
              </Button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="mt-2 text-xs text-warn">
          {error}
        </p>
      )}
    </Section>
  )
}

/** A duration Bambuddy may not have reported, as the list's print cards write it. */
function duration(seconds: number | null | undefined): string | null {
  return seconds === null || seconds === undefined ? null : formatDuration(seconds)
}

/** A measured amount, rounded for reading: Bambuddy's floats can end `…000001`. */
function amount(value: number | null | undefined, unit: string): string | null {
  if (value === null || value === undefined) return null
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 2 }).format(value)} ${unit}`
}

/** A percentage, rounded as {@link amount} rounds. */
function percent(value: number): string {
  return `${new Intl.NumberFormat(undefined, { maximumFractionDigits: 1 }).format(value)}%`
}

/** A link Bambuddy keeps, opened outside the frame; only an http(s) one is a link. */
function LinkFact({ label, url }: { label: string; url: string | null | undefined }) {
  if (!url) return null
  const web = /^https?:\/\//i.test(url)
  return (
    <Fact
      label={label}
      value={
        web ? (
          <Button
            variant="link"
            className="h-auto p-0 text-left font-normal break-all whitespace-normal"
            onClick={() => openExternal(url)}
          >
            {url}
          </Button>
        ) : (
          url
        )
      }
    />
  )
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <>
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="sb-num text-[13px]">{value}</dd>
    </>
  )
}

function OutcomeSection({ print }: { print: PrintDetail }) {
  const { outcome } = print
  const filament =
    outcome.filament_used_grams !== null
      ? `${outcome.filament_used_grams} g${outcome.filament_type ? ` ${outcome.filament_type}` : ''}`
      : null
  const colours = outcome.filament_color?.split(',').filter(Boolean) ?? []
  return (
    <Section title="Outcome">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <Fact label="Status" value={statusLabel(outcome.status)} />
        <Fact label="Failure" value={outcome.failure_reason} />
        <Fact
          label="Printer"
          value={outcome.printer_name ?? print.printer_name ?? (outcome.printer_id !== null ? printerLabel(outcome.printer_id) : null)}
        />
        <Fact label="Started" value={formatWhen(print.started_at)} />
        <Fact label="Finished" value={formatWhen(print.completed_at)} />
        <Fact label="Took" value={duration(outcome.actual_time_seconds)} />
        <Fact label="Estimated" value={duration(outcome.estimated_time_seconds)} />
        <Fact label="Filament" value={filament} />
        {colours.length > 0 && (
          <Fact
            label="Colours"
            value={
              <span className="flex gap-1">
                {colours.map((colour) => (
                  <Swatch key={colour} colour={colour} />
                ))}
              </span>
            }
          />
        )}
        <Fact label="Cost" value={outcome.cost !== null ? outcome.cost.toFixed(2) : null} />
      </dl>
      {outcome.runs.length > 0 && (
        <>
          <h3 className="mt-3 text-xs font-medium text-muted-foreground">Runs</h3>
          <ul aria-label="Runs" className="mt-1 space-y-1">
            {outcome.runs.map((run) => (
              <RunRow key={run.id} run={run} />
            ))}
          </ul>
        </>
      )}
    </Section>
  )
}

/** A colour as Bambuddy reports one: `#RGB`, `#RRGGBB` or with alpha, nothing else. */
const HEX_COLOUR = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i

/**
 * One filament colour. Only a hex colour is painted: the value is Bambuddy's, and a CSS
 * `background` would otherwise load whatever `url(...)` it named.
 */
function Swatch({ colour }: { colour: string }) {
  const paint = HEX_COLOUR.test(colour.trim()) ? colour.trim() : undefined
  return (
    <Hint content={colour}>
      <span
        role="img"
        aria-label={colour}
        className="inline-block size-3.5 shrink-0 rounded-full border"
        style={paint ? { background: paint } : undefined}
      />
    </Hint>
  )
}

/** #1860 — what the sliced file says: the printer, bed, layers, nozzle and each slot. */
function SlicingSection({ slicing }: { slicing: NonNullable<PrintDetail['slicing']> }) {
  const unit = useDisplayUnit()
  const metres = slicing.filament_used_mm !== null ? `${(slicing.filament_used_mm / 1000).toFixed(2)} m` : null
  return (
    <Section title="Slicing">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <Fact label="Sliced for" value={slicing.sliced_for_model} />
        <Fact label="Bed" value={slicing.bed_type} />
        <Fact
          label="Layer height"
          value={slicing.layer_height_mm !== null ? `${fineLength(slicing.layer_height_mm, unit)} ${unit}` : null}
        />
        <Fact label="Layers" value={slicing.total_layers} />
        <Fact
          label="Nozzle"
          value={slicing.nozzle_diameter_mm !== null ? `${fineLength(slicing.nozzle_diameter_mm, unit)} ${unit}` : null}
        />
        <Fact label="Nozzle temp" value={slicing.nozzle_temperature_c !== null ? `${slicing.nozzle_temperature_c} °C` : null} />
        <Fact label="Bed temp" value={slicing.bed_temperature_c !== null ? `${slicing.bed_temperature_c} °C` : null} />
        <Fact label="Objects" value={slicing.objects.length > 0 ? slicing.objects.join(', ') : null} />
        <Fact label="Filament length" value={metres} />
      </dl>
      {slicing.filament_slots.length > 0 && (
        <>
          <h3 className="mt-3 text-xs font-medium text-muted-foreground">Filament by slot</h3>
          <ul aria-label="Filament by slot" className="mt-1 space-y-1">
            {slicing.filament_slots.map((slot) => (
              <li key={slot.slot_id} className="sb-num flex items-center gap-2 text-[13px]">
                {slot.color && <Swatch colour={slot.color} />}
                <span>Slot {slot.slot_id}</span>
                {slot.filament_type && <span className="text-muted-foreground">{slot.filament_type}</span>}
                {slot.used_grams !== null && <span>{amount(slot.used_grams, 'g')}</span>}
              </li>
            ))}
          </ul>
        </>
      )}
    </Section>
  )
}

/** #1860 — Bambuddy's bookkeeping, and every field of its archive not shown elsewhere. */
function RecordSection({ record }: { record: NonNullable<PrintDetail['record']> }) {
  const other = Object.entries(record.other)
  return (
    <Section title="In Bambuddy">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <Fact label="Project" value={record.project_name} />
        <Fact label="Designer" value={record.designer} />
        <Fact label="Dispatched" value={formatWhen(record.created_at)} />
        <Fact label="Last run" value={formatWhen(record.last_run_at)} />
        <Fact
          label="Runs"
          value={`${record.successful_run_count} succeeded, ${record.failed_run_count} failed`}
        />
        <Fact label="Copies" value={record.quantity > 1 ? record.quantity : null} />
        <Fact
          label="Time accuracy"
          value={record.time_accuracy_percent !== null ? percent(record.time_accuracy_percent) : null}
        />
        <Fact label="Filament read" value={amount(record.filament_actual_grams, 'g')} />
        <Fact label="Energy" value={amount(record.energy_kwh, 'kWh')} />
        <Fact
          label="Energy cost"
          value={record.energy_cost !== null ? `${record.energy_cost.toFixed(2)} (in Bambuddy's currency)` : null}
        />
        <Fact label="Favourite" value={record.is_favorite ? 'Yes' : null} />
        <Fact label="Tags" value={record.tags} />
        <Fact label="Notes" value={record.notes} />
        <LinkFact label="MakerWorld" url={record.makerworld_url} />
        <LinkFact label="Link" url={record.external_url} />
      </dl>
      {other.length > 0 && (
        <Collapsible className="mt-3">
          <CollapsibleTrigger asChild>
            <Button variant="ghost" size="sm" className="group -ml-2 text-xs font-normal text-muted-foreground">
              <ChevronRightIcon
                aria-hidden="true"
                className="transition-transform group-data-[state=open]:rotate-90"
              />
              All other fields ({other.length})
            </Button>
          </CollapsibleTrigger>
          <CollapsibleContent>
            <dl aria-label="All other fields" className="mt-1 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
              {other.map(([name, value]) => (
                <Fragment key={name}>
                  <dt className="sb-num text-xs text-muted-foreground">{name}</dt>
                  <dd className="sb-num text-[13px] break-all">{value === null ? '—' : String(value)}</dd>
                </Fragment>
              ))}
            </dl>
          </CollapsibleContent>
        </Collapsible>
      )}
    </Section>
  )
}

/** Why a run's filament reading is not taken as this print's. */
const SUSPECT_READING = "Far more than this print used, most likely a spool's weight; not used for the cost"

function RunRow({ run }: { run: Run }) {
  return (
    <li className="flex flex-wrap gap-x-3 text-xs">
      <span>{statusLabel(run.status)}</span>
      <span className="sb-num text-muted-foreground">{formatWhen(run.started_at)}</span>
      {run.duration_seconds != null && (
        <span className="sb-num text-muted-foreground">{duration(run.duration_seconds)}</span>
      )}
      {run.filament_used_grams != null &&
        (run.filament_reading_suspect ? (
          <>
            {/* Focusable, so the tooltip opens from the keyboard too. */}
            <Hint content={SUSPECT_READING}>
              <span
                tabIndex={0}
                className="sb-num rounded-sm text-warn outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                aria-describedby={`run-${run.id}-suspect`}
              >
                {`${run.filament_used_grams} g, not this print's`}
              </span>
            </Hint>
            <span id={`run-${run.id}-suspect`} className="sr-only">
              {SUSPECT_READING}
            </span>
          </>
        ) : (
          <span className="sb-num text-muted-foreground">{run.filament_used_grams} g</span>
        ))}
      {run.failure_reason && <span className="text-warn">{run.failure_reason}</span>}
    </li>
  )
}

function ProvenanceSection({ provenance }: { provenance: NonNullable<PrintDetail['provenance']> }) {
  const version = provenance.model_version
  return (
    <Section title="Provenance">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <Fact
          label="Template"
          value={
            <Link to={modelPath(provenance.slug)} className="font-medium text-primary underline underline-offset-4">
              {provenance.slug}
            </Link>
          }
        />
        <Fact
          label="Revision"
          value={
            version ? (
              <Link
                to={modelPath(provenance.slug, 'versions')}
                className="font-medium text-primary underline underline-offset-4"
              >
                {version.slice(0, 7)}
              </Link>
            ) : (
              'Unknown'
            )
          }
        />
      </dl>
      <Card size="sm" className="mt-3 gap-0 py-0">
        <ParamsTable label="Parameters" params={provenance.params} />
      </Card>
    </Section>
  )
}

function FilesSection({ print }: { print: PrintDetail }) {
  return (
    <Section title="Files">
      <PrintFiles print={print} />
    </Section>
  )
}
