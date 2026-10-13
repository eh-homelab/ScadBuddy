import { Suspense, lazy, useState, type ReactNode } from 'react'
import { api } from '../../api/client'
import type { Job, PrintDetail } from '../../api/types'
import { DownloadBlockedError, downloadBlob } from '../../lib/embed'
import { formatBytes, formatDuration, formatValue } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { Swatch } from '../print/Swatch'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

const Preview = lazy(async () => ({ default: (await import('../Preview')).Preview }))

type PrintFile = PrintDetail['files'][number]

/** What a row of the panel is: a file of the print, its parameters, or its media (#894). */
type Kind = PrintFile['kind'] | 'parameters' | 'photo' | 'timelapse'

interface Entry {
  key: string
  kind: Kind
  name: string
  size: number | null
  /** Where its bytes are; none for the parameters, which are the print's own JSON. */
  url: string | null
}

/** #894 — a friendly name for each kind of file, with its own name underneath. */
const FILE_LABELS: Record<Kind, string> = {
  output_3mf: 'Editable model (3MF)',
  preview_glb: '3D preview',
  sliced: 'Sliced file',
  source: 'Slicer project (3MF)',
  library_file: 'Library file',
  parameters: 'Parameters',
  photo: 'Photo',
  timelapse: 'Timelapse',
}

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

function entries(print: PrintDetail): Entry[] {
  const rows: Entry[] = print.files.map((file) => ({
    key: file.url,
    kind: file.kind,
    name: file.name,
    size: file.size,
    url: file.url,
  }))
  if (print.provenance) {
    rows.push({ key: 'parameters', kind: 'parameters', name: paramsFileName(print), size: null, url: null })
  }
  const { media } = print
  for (const photo of [...(media.finish_photo ? [media.finish_photo] : []), ...media.photos]) {
    rows.push({ key: photo.url, kind: 'photo', name: photo.name, size: null, url: photo.url })
  }
  if (media.timelapse) {
    rows.push({
      key: 'timelapse',
      kind: 'timelapse',
      name: `print-${print.archive_id}-timelapse.mp4`,
      size: null,
      url: media.timelapse.url,
    })
  }
  return rows
}

/** #894 — one icon per kind of file; anything else gets the plain page. */
function FileIcon({ kind }: { kind: Kind }) {
  const path: Record<Kind, ReactNode> = {
    output_3mf: <path d="M10 2 17 6v8l-7 4-7-4V6Z M3 6l7 4 7-4 M10 10v8" />,
    preview_glb: (
      <>
        <circle cx="10" cy="10" r="7" />
        <path d="M3 10c4 3 10 3 14 0 M10 3c-3 4-3 10 0 14" />
      </>
    ),
    sliced: <path d="M3 5h14 M3 9h14 M3 13h14 M3 17h14" />,
    source: <path d="M10 2 17 6v8l-7 4-7-4V6Z M7 9h6 M7 12h6" />,
    library_file: <path d="M3 4h5l2 2h7v11H3Z" />,
    parameters: <path d="M4 5h8 M15 5h1 M4 10h2 M9 10h7 M4 15h9 M16 15h0 M12 3v4 M7 8v4 M13 13v4" />,
    photo: (
      <>
        <rect x="2.5" y="4.5" width="15" height="11" rx="1.5" />
        <circle cx="7" cy="9" r="1.5" />
        <path d="m4 15 4-4 3 3 2-2 4 3" />
      </>
    ),
    timelapse: (
      <>
        <rect x="2.5" y="4.5" width="11" height="11" rx="1.5" />
        <path d="m13.5 8 4-2v8l-4-2" />
      </>
    ),
  }
  return (
    <svg
      aria-hidden
      data-testid={`file-icon-${kind}`}
      viewBox="0 0 20 20"
      className="size-5 shrink-0 text-muted"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.4"
      strokeLinejoin="round"
      strokeLinecap="round"
    >
      {path[kind]}
    </svg>
  )
}

function DownloadIcon() {
  return (
    <svg aria-hidden viewBox="0 0 16 16" className="size-4" fill="none" stroke="currentColor" strokeWidth="1.5">
      <path d="M8 2v8 M4.5 7 8 10.5 11.5 7 M3 13.5h10" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  )
}

/**
 * #894 — the print's files, each with its icon, a friendly label over its own name, its
 * size, and a download button of its own. Clicking a row opens the file in the app: a mesh
 * in the viewer, the sliced file's plates and summary, the parameters as a table, a photo
 * or the timelapse.
 */
export function PrintFiles({ print }: { print: PrintDetail }) {
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [opened, setOpened] = useState<Entry | null>(null)
  const rows = entries(print)

  async function save(entry: Entry) {
    setBusy(entry.key)
    setError(null)
    try {
      if (entry.url) {
        await download(entry.url, entry.name)
      } else if (print.provenance) {
        const json = JSON.stringify(print.provenance.params, null, 2) + '\n'
        await downloadBlob(async () => new Blob([json], { type: 'application/json' }), entry.name)
      }
    } catch (cause) {
      setError(
        cause instanceof DownloadBlockedError
          ? cause.message
          : entry.kind === 'parameters'
            ? 'Could not save the parameters.'
            : `Could not download ${entry.name}.`,
      )
    } finally {
      setBusy(null)
    }
  }

  return (
    <>
      <ul aria-label="Files" className="space-y-1">
        {rows.map((entry) => (
          <li key={entry.key} className="flex items-center gap-2">
            <button
              type="button"
              aria-label={`${FILE_LABELS[entry.kind]}, ${entry.name}, open`}
              onClick={() => setOpened(entry)}
              className="flex min-w-0 flex-1 items-center gap-3 rounded-[6px] px-2 py-1.5 text-left hover:bg-surface-2 focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-none"
            >
              <FileIcon kind={entry.kind} />
              <span className="min-w-0 flex-1">
                <span className="block text-[13px] text-ink">{FILE_LABELS[entry.kind]}</span>
                <span title={entry.name} className="sb-num block truncate text-[12px] text-faint">
                  {entry.name}
                </span>
              </span>
              {entry.size !== null && (
                <span className="sb-num shrink-0 text-[12px] text-faint">{formatBytes(entry.size)}</span>
              )}
            </button>
            <Button
              size="sm"
              aria-label={entry.kind === 'parameters' ? 'Download parameters as JSON' : `Download ${entry.name}`}
              title="Download"
              onClick={() => void save(entry)}
              disabled={busy !== null}
            >
              {busy === entry.key ? <Spinner /> : <DownloadIcon />}
            </Button>
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" className="mt-2 text-[12px] text-warn">
          {error}
        </p>
      )}
      <Dialog
        open={opened !== null}
        title={opened ? FILE_LABELS[opened.kind] : ''}
        description={opened?.name}
        onClose={() => setOpened(null)}
        size="wide"
      >
        {opened && <FileView print={print} entry={opened} />}
      </Dialog>
    </>
  )
}

function FileView({ print, entry }: { print: PrintDetail; entry: Entry }) {
  switch (entry.kind) {
    case 'preview_glb':
      return <MeshView print={print} url={entry.url as string} />
    case 'output_3mf':
      return <ModelView print={print} />
    case 'sliced':
    case 'source':
    case 'library_file':
      return <SlicedView print={print} />
    case 'parameters':
      return <ParametersView print={print} />
    case 'photo':
      return <img src={entry.url as string} alt={entry.name} className="max-h-[70vh] w-full object-contain" />
    case 'timelapse':
      return (
        <video
          src={entry.url as string}
          controls
          playsInline
          preload="metadata"
          aria-label="Timelapse"
          className="max-h-[70vh] w-full bg-black"
        />
      )
  }
}

/** A mesh in the 3D viewer, framed by the output's box when the print has an output. */
function MeshView({ print, url }: { print: PrintDetail; url: string }) {
  const output = useAsync(
    async () => (print.output_id ? await api.getOutput(print.output_id).catch(() => null) : null),
    [print.output_id],
  )
  if (output.loading) return <Spinner />
  const job: Job = {
    id: print.output_id ?? `print-${print.archive_id}`,
    slug: print.slug ?? '',
    status: 'done',
    created_at: output.data?.created_at ?? '',
    preview_url: url,
    bbox_mm: output.data?.bbox_mm ?? null,
    colors: output.data?.colors ?? null,
  }
  return (
    <div data-testid="file-mesh" className="aspect-[4/3] w-full overflow-hidden rounded-[4px]">
      <Suspense fallback={null}>
        <Preview job={job} rendering={false} />
      </Suspense>
    </div>
  )
}

/** The editable 3MF: its mesh, the colours it uses, and each plate's image. */
function ModelView({ print }: { print: PrintDetail }) {
  const id = print.output_id
  const plates = useAsync(async () => (id ? await api.getOutputPlates(id).catch(() => []) : []), [id])
  const output = useAsync(async () => (id ? await api.getOutput(id).catch(() => null) : null), [id])
  if (!id) return null
  const colours = output.data?.colors ?? []
  return (
    <div className="space-y-3">
      <MeshView print={print} url={`/api/v1/outputs/${id}/preview.glb`} />
      {colours.length > 0 && (
        <p className="flex items-center gap-2 text-[12px] text-muted">
          Colours
          {colours.map((colour) => (
            <Swatch key={colour} colour={colour} size="sm" />
          ))}
        </p>
      )}
      {(plates.data ?? []).length > 1 && (
        <ul aria-label="Plates" className="flex flex-wrap gap-2">
          {(plates.data ?? []).map((plate) => (
            <li key={plate.index} className="text-center text-[12px] text-muted">
              <img
                src={api.outputPlateThumbnailUrl(id, plate.index)}
                alt={`Plate ${plate.index}`}
                className="h-28 w-auto rounded-[4px] bg-surface-2"
              />
              Plate {plate.index}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

/** The sliced file: its plate images and what the slicer said of it. */
function SlicedView({ print }: { print: PrintDetail }) {
  const { slicing, outcome, media } = print
  return (
    <div className="space-y-3">
      {media.plate_thumbnails.length > 0 && (
        <ul aria-label="Plates as sliced" className="flex flex-wrap gap-2">
          {media.plate_thumbnails.map((plate) => (
            <li key={plate.index} className="text-center text-[12px] text-muted">
              <img
                src={plate.url}
                alt={plate.name ?? `Plate ${plate.index}`}
                className="h-48 w-auto rounded-[4px] bg-surface-2"
              />
              {plate.name ?? `Plate ${plate.index}`}
            </li>
          ))}
        </ul>
      )}
      <dl aria-label="Slicing summary" className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]">
        {outcome.estimated_time_seconds !== null && (
          <>
            <dt className="text-faint">Print time</dt>
            <dd className="sb-num text-ink">{formatDuration(outcome.estimated_time_seconds)}</dd>
          </>
        )}
        {(slicing?.filament_slots ?? []).map((slot) => (
          <FragmentRow
            key={slot.slot_id}
            label={`Slot ${slot.slot_id}`}
            value={[slot.filament_type, slot.used_grams !== null ? `${slot.used_grams} g` : null]
              .filter(Boolean)
              .join(' · ')}
          />
        ))}
      </dl>
    </div>
  )
}

function FragmentRow({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-faint">{label}</dt>
      <dd className="sb-num text-ink">{value}</dd>
    </>
  )
}

/** The parameters, those that differ from the template's defaults marked, to copy or save. */
function ParametersView({ print }: { print: PrintDetail }) {
  const [copied, setCopied] = useState(false)
  if (!print.provenance) return null
  const params = print.provenance.params
  const changed = new Set(Object.keys(print.params_diff ?? {}))
  const json = JSON.stringify(params, null, 2) + '\n'
  return (
    <div className="space-y-2">
      <table aria-label="Parameters" className="w-full text-[12px]">
        <tbody>
          {Object.entries(params).map(([name, value]) => (
            <tr key={name} className="border-t border-line" data-changed={changed.has(name) || undefined}>
              <th scope="row" className="py-1 pr-3 text-left font-normal text-muted">
                {name}
              </th>
              <td className={`sb-num py-1 ${changed.has(name) ? 'font-medium text-accent' : 'text-ink'}`}>
                {formatValue(value)}
                {changed.has(name) && <span className="sr-only"> (changed from the default)</span>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <Button
        size="sm"
        onClick={() =>
          void navigator.clipboard?.writeText(json).then(
            () => setCopied(true),
            () => setCopied(false),
          )
        }
      >
        {copied ? 'Copied' : 'Copy JSON'}
      </Button>
    </div>
  )
}
