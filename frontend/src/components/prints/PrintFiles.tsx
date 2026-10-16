import { Suspense, lazy, useState, type ReactNode } from 'react'
import { api } from '../../api/client'
import type { Job, ParamValue, PrintDetail } from '../../api/types'
import { DownloadBlockedError, downloadBlob } from '../../lib/embed'
import { formatBytes, formatDuration, formatValue } from '../../lib/format'
import { useAsync } from '../../lib/useAsync'
import { CheckIcon, CopyIcon, DownloadIcon } from 'lucide-react'
import { Swatch } from '../print/Swatch'
import { Hint } from '../Hint'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Spinner } from '../ui/spinner'

const Preview = lazy(async () => ({ default: (await import('../Preview')).Preview }))

type PrintFile = PrintDetail['files'][number]

/** What a row of the panel is: a file of the print, its parameters, or its media (#894). */
type Kind = PrintFile['kind'] | 'parameters' | 'photo' | 'video' | 'timelapse'

/** Every file has its bytes at a URL but the parameters, which are the print's own JSON. */
type Entry = { key: string; name: string; size: number | null } & (
  | { kind: 'parameters'; url: null }
  | { kind: Exclude<Kind, 'parameters'>; url: string }
)

/** #894 — a friendly name for each kind of file, with its own name underneath. */
const FILE_LABELS: Record<Kind, string> = {
  output_3mf: 'Editable model (3MF)',
  preview_glb: '3D preview',
  sliced: 'Sliced file',
  source: 'Slicer project (3MF)',
  library_file: 'Library file',
  parameters: 'Parameters',
  photo: 'Photo',
  video: 'Video',
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
  // #309 — the photos and videos added in ScadBuddy.
  for (const attachment of media.attachments) {
    rows.push({
      key: attachment.url,
      kind: attachment.kind,
      name: attachment.filename,
      size: attachment.size_bytes,
      url: attachment.url,
    })
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
    video: (
      <>
        <rect x="2.5" y="4.5" width="15" height="11" rx="1.5" />
        <path d="m8.5 7.5 4 2.5-4 2.5Z" />
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
      className="size-5 shrink-0 text-muted-foreground"
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
            <Button
              variant="ghost"
              aria-label={`${FILE_LABELS[entry.kind]}, ${entry.name}, open`}
              onClick={() => setOpened(entry)}
              className="h-auto min-w-0 flex-1 justify-start gap-3 px-2 py-1.5 text-left font-normal whitespace-normal"
            >
              <FileIcon kind={entry.kind} />
              <span className="min-w-0 flex-1">
                <span className="block text-[13px]">{FILE_LABELS[entry.kind]}</span>
                <Hint content={entry.name}>
                  <span className="sb-num block truncate text-xs text-muted-foreground">{entry.name}</span>
                </Hint>
              </span>
              {entry.size !== null && (
                <span className="sb-num shrink-0 text-xs text-muted-foreground">{formatBytes(entry.size)}</span>
              )}
            </Button>
            <Hint content="Download">
              <Button
                variant="outline"
                size="icon-sm"
                aria-label={entry.kind === 'parameters' ? 'Download parameters as JSON' : `Download ${entry.name}`}
                onClick={() => void save(entry)}
                disabled={busy !== null}
              >
                {busy === entry.key ? <Spinner /> : <DownloadIcon aria-hidden="true" />}
              </Button>
            </Hint>
          </li>
        ))}
      </ul>
      {error && (
        <p role="alert" className="mt-2 text-xs text-warn">
          {error}
        </p>
      )}
      <Dialog open={opened !== null} onOpenChange={(next) => !next && setOpened(null)}>
        <DialogContent className="max-h-[calc(100dvh-2rem)] overflow-y-auto sm:max-w-5xl">
          <DialogHeader>
            <DialogTitle>{opened ? FILE_LABELS[opened.kind] : ''}</DialogTitle>
            <DialogDescription className="sb-num break-all">{opened?.name}</DialogDescription>
          </DialogHeader>
          {opened && <FileView print={print} entry={opened} />}
        </DialogContent>
      </Dialog>
    </>
  )
}

function FileView({ print, entry }: { print: PrintDetail; entry: Entry }) {
  switch (entry.kind) {
    case 'preview_glb':
      return <MeshView print={print} url={entry.url} />
    case 'output_3mf':
      return <ModelView print={print} meshUrl={print.files.find((file) => file.kind === 'preview_glb')?.url ?? null} />
    case 'sliced':
    case 'source':
    case 'library_file':
      return <SlicedView print={print} />
    case 'parameters':
      return <ParametersView print={print} />
    case 'photo':
      return <img src={entry.url} alt={entry.name} className="max-h-[70vh] w-full object-contain" />
    case 'video':
    case 'timelapse':
      return (
        <video
          src={entry.url}
          controls
          playsInline
          preload="metadata"
          aria-label={FILE_LABELS[entry.kind]}
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
  if (output.loading) {
    return (
      <div role="status" aria-label="Loading the 3D preview">
        <Skeleton className="aspect-[4/3] w-full rounded-lg" />
      </div>
    )
  }
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
    <div data-testid="file-mesh" className="aspect-[4/3] w-full overflow-hidden rounded-lg">
      <Suspense fallback={null}>
        <Preview job={job} rendering={false} />
      </Suspense>
    </div>
  )
}

/** The editable 3MF: its mesh, the colours it uses, and each plate's image. */
/** The editable 3MF: its mesh when ScadBuddy still has the preview, colours, and plates. */
function ModelView({ print, meshUrl }: { print: PrintDetail; meshUrl: string | null }) {
  const id = print.output_id
  const plates = useAsync(async () => (id ? await api.getOutputPlates(id).catch(() => []) : []), [id])
  const output = useAsync(async () => (id ? await api.getOutput(id).catch(() => null) : null), [id])
  if (!id) return null
  const colours = output.data?.colors ?? []
  return (
    <div className="space-y-3">
      {meshUrl && <MeshView print={print} url={meshUrl} />}
      {colours.length > 0 && (
        <p className="flex items-center gap-2 text-xs text-muted-foreground">
          Colours
          {colours.map((colour) => (
            <Swatch key={colour} colour={colour} size="sm" />
          ))}
        </p>
      )}
      {(plates.data ?? []).length > 1 && (
        <ul aria-label="Plates" className="flex flex-wrap gap-2">
          {(plates.data ?? []).map((plate) => (
            <li key={plate.index} className="text-center text-xs text-muted-foreground">
              <img
                src={api.outputPlateThumbnailUrl(id, plate.index)}
                alt={`Plate ${plate.index}`}
                className="h-28 w-auto rounded-lg bg-muted"
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
            <li key={plate.index} className="text-center text-xs text-muted-foreground">
              <img
                src={plate.url}
                alt={plate.name ?? `Plate ${plate.index}`}
                className="h-48 w-auto rounded-lg bg-muted"
              />
              {plate.name ?? `Plate ${plate.index}`}
            </li>
          ))}
        </ul>
      )}
      <dl aria-label="Slicing summary" className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[13px]">
        {outcome.estimated_time_seconds !== null && (
          <>
            <dt className="text-muted-foreground">Print time</dt>
            <dd className="sb-num">{formatDuration(outcome.estimated_time_seconds)}</dd>
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
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="sb-num">{value}</dd>
    </>
  )
}

/** The parameters, those that differ from the template's defaults marked, to copy or save. */
function ParametersView({ print }: { print: PrintDetail }) {
  const [copied, setCopied] = useState<'idle' | 'copied' | 'failed'>('idle')
  if (!print.provenance) return null
  const params = print.provenance.params
  const changed = new Set(Object.keys(print.params_diff ?? {}))
  const json = JSON.stringify(params, null, 2) + '\n'
  return (
    <div className="space-y-3">
      <Card size="sm" className="gap-0 py-0">
        <ParamsTable label="Parameters" params={params} changed={changed} />
      </Card>
      <Button
        variant="outline"
        size="sm"
        onClick={() => {
          // No clipboard on an insecure origin, and some frames refuse it: say so.
          if (!navigator.clipboard) {
            setCopied('failed')
            return
          }
          void navigator.clipboard.writeText(json).then(
            () => setCopied('copied'),
            () => setCopied('failed'),
          )
        }}
      >
        {copied === 'copied' ? <CheckIcon aria-hidden="true" /> : <CopyIcon aria-hidden="true" />}
        {copied === 'copied' ? 'Copied' : 'Copy JSON'}
      </Button>
      {copied === 'failed' && (
        <p role="alert" className="text-xs text-warn">
          The parameters could not be copied here. Download them as JSON instead.
        </p>
      )}
    </div>
  )
}

/**
 * A print's parameters as a table, in a card the caller gives it: those in `changed`
 * (differing from the template's defaults) marked.
 */
export function ParamsTable({
  label,
  params,
  changed,
}: {
  label: string
  params: Record<string, ParamValue>
  changed?: ReadonlySet<string>
}) {
  return (
    <table aria-label={label} className="w-full text-xs">
      <tbody className="divide-y">
        {Object.entries(params).map(([name, value]) => {
          const differs = changed?.has(name) ?? false
          return (
            <tr key={name} data-changed={differs || undefined}>
              <th scope="row" className="py-1.5 pr-3 pl-3 text-left font-normal text-muted-foreground">
                {name}
              </th>
              <td className={`sb-num py-1.5 pr-3 ${differs ? 'font-medium text-primary' : ''}`}>
                {formatValue(value)}
                {differs && <span className="sr-only"> (changed from the default)</span>}
              </td>
            </tr>
          )
        })}
      </tbody>
    </table>
  )
}
