import { Suspense, lazy, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type { Job, PrintDetail, PrinterMedia } from '../api/types'
import { MediaCarousel } from '../components/media/MediaCarousel'
import { MediaLightbox } from '../components/media/MediaLightbox'
import type { Slide } from '../components/media/slides'
import { PrintAgainDialog } from '../components/PrintAgainDialog'
import { Button } from '../components/ui/Button'
import { Spinner } from '../components/ui/Spinner'
import { modelPath } from '../lib/deeplink'
import { downloadBlob, isEmbedded, openExternal } from '../lib/embed'
import { formatValue } from '../lib/format'
import { DELETED, formatBytes, formatDuration, statusLabel } from '../lib/prints'
import { useAsync } from '../lib/useAsync'

// The 3D viewer is three.js, the biggest thing in the bundle: loaded only when shown.
const Preview = lazy(async () => ({ default: (await import('../components/Preview')).Preview }))

type PrintFile = PrintDetail['files'][number]
type Run = PrintDetail['outcome']['runs'][number]


const FILE_LABELS: Record<PrintFile['kind'], string> = {
  output_3mf: 'ScadBuddy 3MF',
  preview_glb: 'Preview mesh',
  sliced: 'Sliced file',
  source: 'Source 3MF',
}

function formatWhen(iso: string | null | undefined): string | null {
  if (!iso) return null
  const when = new Date(iso)
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString()
}

function clock(seconds: number): string {
  const whole = Math.max(0, Math.floor(seconds))
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, '0')}`
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
      poster: media.timelapse.poster_frames[0]?.data_url,
      alt: 'Timelapse',
      contentType: 'video/mp4',
    })
  }
  for (const plate of media.plate_thumbnails) {
    slides.push({
      key: `plate-${plate.index}`,
      kind: 'image',
      src: plate.url,
      alt: `Plate ${plate.index} as sliced`,
      caption: "The slicer's plate image",
    })
  }
  media.attachments.forEach((attachment, index) => {
    slides.push({
      key: `attachment-${attachment.id}`,
      kind: attachment.kind === 'video' ? 'video' : 'image',
      src: attachment.url,
      alt: attachment.caption ?? `Attachment ${index + 1}`,
      caption: attachment.caption ?? undefined,
    })
  })
  return slides
}

/** Fetched as a blob and saved through `lib/embed`, so it works inside Bambuddy's iframe. */
async function download(url: string, name: string): Promise<void> {
  await downloadBlob(async () => {
    const response = await fetch(url)
    if (!response.ok) throw new Error(`HTTP ${response.status}`)
    return await response.blob()
  }, name)
}

function paramsFileName(print: PrintDetail): string {
  const own = print.files.find((file) => file.kind === 'output_3mf')
  return own ? `${own.name.replace(/\.3mf$/i, '')}-params.json` : `print-${print.archive_id}-params.json`
}

export function PrintDetailPage() {
  const { archiveId = '' } = useParams()
  const id = Number(archiveId)
  const state = useAsync(() => api.getPrint(id), [id])
  const print = state.data

  if (state.loading) {
    return (
      <p className="flex items-center gap-2 px-4 py-16 text-[13px] text-muted">
        <Spinner /> Loading the print
      </p>
    )
  }
  if (state.error || !print) {
    return (
      <div role="alert" className="mx-auto max-w-lg px-4 py-16 text-center">
        <h1 className="text-[15px] font-medium">No such print</h1>
        <p className="mt-2 text-[13px] text-muted">{state.error?.message}</p>
        <Link to="/prints" className="mt-4 inline-block text-[13px] text-accent underline">
          Back to the prints
        </Link>
      </div>
    )
  }
  return <PrintView print={print} reload={state.refresh} />
}

function PrintView({ print, reload }: { print: PrintDetail; reload: () => void }) {
  const deleted = print.status === DELETED
  const [reprinting, setReprinting] = useState(false)
  const title = print.output_name ?? `Print ${print.archive_id}`

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-6xl px-4 py-6">
        <nav className="mb-2 flex items-baseline gap-2 text-[12px]">
          <Link to="/prints" className="text-muted hover:text-ink">
            Prints
          </Link>
          <span className="text-faint">/</span>
          <Link to={modelPath(print.slug)} className="text-muted hover:text-ink">
            {print.slug}
          </Link>
        </nav>
        <header className="mb-5 flex flex-wrap items-center gap-3">
          <h1 className="text-[17px] font-medium">{title}</h1>
          <StatusBadge status={print.status} />
          <span className="sb-num text-[12px] text-faint">Archive #{print.archive_id}</span>
          <div className="ml-auto flex flex-wrap gap-2">
            <Link
              to={print.provenance.edit_url}
              className="inline-flex items-center rounded-[6px] border border-line bg-surface-2 px-3 py-1.5 text-[13px] text-ink hover:border-line-strong"
            >
              Customize from this
            </Link>
            {!deleted && (
              <Button onClick={() => setReprinting(true)}>Print again</Button>
            )}
            {print.links.bambuddy_url && (
              <Button onClick={() => openExternal(print.links.bambuddy_url as string)}>
                Open in Bambuddy
              </Button>
            )}
          </div>
        </header>

        {deleted && (
          <p
            role="status"
            className="mb-4 rounded-[6px] border border-warn/40 bg-warn/8 px-3 py-2 text-[13px] text-warn"
          >
            This print was deleted in Bambuddy. Its photos, timelapse and sliced file went with
            it; ScadBuddy still has the parameters and its own files.
          </p>
        )}

        <div className="grid gap-4 lg:grid-cols-2">
          {!deleted && <GallerySection print={print} />}
          <RenderSection print={print} />
          {!deleted && <TimelapseSection print={print} onPulled={reload} />}
          <OutcomeSection print={print} />
          <ProvenanceSection print={print} />
          <FilesSection print={print} />
        </div>
      </div>
      {!deleted && (
        <PrintAgainDialog open={reprinting} print={print} onClose={() => setReprinting(false)} />
      )}
    </div>
  )
}

function StatusBadge({ status }: { status: string }) {
  const tone =
    status === 'completed'
      ? 'bg-ok/12 text-ok'
      : status === 'failed' || status === DELETED
        ? 'bg-warn/12 text-warn'
        : 'bg-surface-2 text-muted'
  return <span className={`rounded-[6px] px-1.5 py-0.5 text-[11px] ${tone}`}>{statusLabel(status)}</span>
}

function Section({ title, children, wide }: { title: string; children: ReactNode; wide?: boolean }) {
  const heading = `print-section-${title.toLowerCase().replace(/\W+/g, '-')}`
  return (
    <section
      aria-labelledby={heading}
      className={`min-w-0 rounded-[6px] border border-line bg-surface p-3 ${wide ? 'lg:col-span-2' : ''}`}
    >
      <h2 id={heading} className="mb-2 text-[12px] font-medium text-muted">
        {title}
      </h2>
      {children}
    </section>
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
        fallback={<p className="text-[13px] text-faint">Bambuddy has no photos of this print.</p>}
      />
      <MediaLightbox slides={slides} index={open} onClose={() => setOpen(null)} />
    </Section>
  )
}

/** ScadBuddy's own render of the output, beside the photos for comparison. */
function RenderSection({ print }: { print: PrintDetail }) {
  const glb = print.files.find((file) => file.kind === 'preview_glb')
  const output = useAsync(
    async () => (glb ? await api.getOutput(print.output_id).catch(() => null) : null),
    [print.output_id, glb?.url],
  )
  const job = useMemo<Job | undefined>(
    () =>
      glb
        ? {
            id: print.output_id,
            slug: print.slug,
            status: 'done',
            created_at: output.data?.created_at ?? '',
            preview_url: glb.url,
            bbox_mm: output.data?.bbox_mm ?? null,
            colors: output.data?.colors ?? null,
          }
        : undefined,
    [glb, print.output_id, print.slug, output.data],
  )
  if (!glb) return null
  return (
    <Section title="ScadBuddy render">
      <div className="aspect-[4/3] overflow-hidden rounded-[4px]">
        {!output.loading && (
          <Suspense fallback={null}>
            <Preview job={job} rendering={false} />
          </Suspense>
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
    const info = timelapse.info
    return (
      <Section title="Timelapse">
        {/* Native controls against the Range proxy: a seek fetches only those bytes.
            No fullscreen control inside Bambuddy's frame, which does not grant it. */}
        <video
          ref={video}
          src={timelapse.url}
          poster={timelapse.poster_frames[0]?.data_url}
          controls
          controlsList={isEmbedded() ? 'nofullscreen' : undefined}
          playsInline
          preload="metadata"
          className="aspect-video w-full rounded-[4px] bg-black"
        />
        {info && (
          <p className="sb-num mt-1.5 text-[12px] text-faint">
            {clock(info.duration)} · {info.width}×{info.height} · {formatBytes(info.file_size)}
          </p>
        )}
        {timelapse.poster_frames.length > 1 || (timelapse.poster_frames.length === 1 && info) ? (
          <ul aria-label="Timelapse frames" className="mt-2 flex gap-1.5 overflow-x-auto">
            {timelapse.poster_frames.map((frame) => (
              <li key={frame.timestamp} className="shrink-0">
                <button
                  type="button"
                  aria-label={`Go to ${clock(frame.timestamp)}`}
                  onClick={() => {
                    if (video.current) video.current.currentTime = frame.timestamp
                  }}
                  className="block overflow-hidden rounded-[4px] focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
                >
                  <img src={frame.data_url} alt="" className="h-12 w-auto" />
                </button>
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
      <p className="text-[13px] text-muted">Bambuddy has no timelapse of this print.</p>
      {onPrinter === null ? (
        <Button size="sm" className="mt-2" onClick={() => void look()} disabled={looking}>
          {looking && <Spinner />}
          Look on the printer
        </Button>
      ) : forbidden ? (
        <p className="mt-2 text-[12px] text-warn">
          The Bambuddy API key cannot list the printer&apos;s files: that needs the Control
          Printer permission.
        </p>
      ) : timelapses.length === 0 ? (
        <p className="mt-2 text-[12px] text-faint">No timelapse on the printer for this print.</p>
      ) : (
        <ul className="mt-2 space-y-2">
          {timelapses.map((file) => (
            <li key={file.path} className="flex flex-wrap items-center gap-2">
              <span className="sb-num min-w-0 truncate text-[12px] text-ink">{file.name}</span>
              <span className="sb-num text-[12px] text-faint">{formatBytes(file.size)}</span>
              <Button
                size="sm"
                className="ml-auto"
                onClick={() => void pull(file.name)}
                disabled={pulling !== null}
              >
                {pulling === file.name && <Spinner />}
                Pull timelapse from printer
              </Button>
            </li>
          ))}
        </ul>
      )}
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
    </Section>
  )
}

function Fact({ label, value }: { label: string; value: ReactNode }) {
  if (value === null || value === undefined || value === '') return null
  return (
    <>
      <dt className="text-[12px] text-faint">{label}</dt>
      <dd className="sb-num text-[13px] text-ink">{value}</dd>
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
          value={outcome.printer_name ?? print.printer_name ?? (outcome.printer_id !== null ? `#${outcome.printer_id}` : null)}
        />
        <Fact label="Started" value={formatWhen(print.started_at)} />
        <Fact label="Finished" value={formatWhen(print.completed_at)} />
        <Fact label="Took" value={formatDuration(outcome.actual_time_seconds)} />
        <Fact label="Estimated" value={formatDuration(outcome.estimated_time_seconds)} />
        <Fact label="Filament" value={filament} />
        {colours.length > 0 && (
          <Fact
            label="Colours"
            value={
              <span className="flex gap-1">
                {colours.map((colour) => (
                  <span
                    key={colour}
                    title={colour}
                    className="inline-block h-3.5 w-3.5 rounded-full border border-line"
                    style={{ background: colour }}
                  />
                ))}
              </span>
            }
          />
        )}
        <Fact label="Cost" value={outcome.cost !== null ? outcome.cost.toFixed(2) : null} />
      </dl>
      {outcome.runs.length > 0 && (
        <>
          <h3 className="mt-3 text-[12px] font-medium text-muted">Runs</h3>
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

function RunRow({ run }: { run: Run }) {
  return (
    <li className="flex flex-wrap gap-x-3 text-[12px]">
      <span className="text-ink">{statusLabel(run.status)}</span>
      <span className="sb-num text-faint">{formatWhen(run.started_at)}</span>
      {run.duration_seconds != null && (
        <span className="sb-num text-faint">{formatDuration(run.duration_seconds)}</span>
      )}
      {run.filament_used_grams != null && (
        <span className="sb-num text-faint">{run.filament_used_grams} g</span>
      )}
      {run.failure_reason && <span className="text-warn">{run.failure_reason}</span>}
    </li>
  )
}

function ProvenanceSection({ print }: { print: PrintDetail }) {
  const { provenance } = print
  const params = Object.entries(provenance.params)
  const version = provenance.model_version
  return (
    <Section title="Provenance">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        <Fact
          label="Template"
          value={
            <Link to={modelPath(provenance.slug)} className="text-accent underline">
              {provenance.slug}
            </Link>
          }
        />
        <Fact
          label="Revision"
          value={
            version ? (
              <Link to={modelPath(provenance.slug, 'versions')} className="text-accent underline">
                {version.slice(0, 7)}
              </Link>
            ) : (
              'Unknown'
            )
          }
        />
      </dl>
      <table aria-label="Parameters" className="mt-3 w-full text-[12px]">
        <tbody>
          {params.map(([name, value]) => (
            <tr key={name} className="border-t border-line">
              <th scope="row" className="py-1 pr-3 text-left font-normal text-faint">
                {name}
              </th>
              <td className="sb-num py-1 text-ink">{formatValue(value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  )
}

function FilesSection({ print }: { print: PrintDetail }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function save(file: PrintFile) {
    setBusy(file.url)
    setError(null)
    try {
      await download(file.url, file.name)
    } catch {
      setError(`Could not download ${file.name}.`)
    } finally {
      setBusy(null)
    }
  }

  function saveParams() {
    const json = JSON.stringify(print.provenance.params, null, 2) + '\n'
    void downloadBlob(async () => new Blob([json], { type: 'application/json' }), paramsFileName(print))
  }

  return (
    <Section title="Files">
      <ul className="space-y-1.5">
        {print.files.map((file) => (
          <li key={file.url} className="flex flex-wrap items-center gap-x-3 gap-y-1">
            <span className="text-[13px] text-ink">{FILE_LABELS[file.kind]}</span>
            <span className="sb-num min-w-0 truncate text-[12px] text-faint">{file.name}</span>
            {file.size !== null && (
              <span className="sb-num text-[12px] text-faint">{formatBytes(file.size)}</span>
            )}
            <Button
              size="sm"
              className="ml-auto"
              aria-label={`Download ${file.name}`}
              onClick={() => void save(file)}
              disabled={busy !== null}
            >
              {busy === file.url && <Spinner />}
              Download
            </Button>
          </li>
        ))}
        <li className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <span className="text-[13px] text-ink">Parameters</span>
          <span className="sb-num min-w-0 truncate text-[12px] text-faint">
            {paramsFileName(print)}
          </span>
          <Button
            size="sm"
            className="ml-auto"
            aria-label="Download parameters as JSON"
            onClick={saveParams}
          >
            Download
          </Button>
        </li>
      </ul>
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
    </Section>
  )
}
