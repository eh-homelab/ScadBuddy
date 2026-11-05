import { lazy, Suspense, useState, type ReactNode } from 'react'
import { Link } from 'react-router'
import { BoxIcon, DownloadIcon, FolderInputIcon, PencilIcon, PrinterIcon, Trash2Icon } from 'lucide-react'
import { api, ApiError } from '../../api/client'
import type { LibraryEntry, LibraryFileEntry, LibraryFolderView } from '../../api/types'
import { editPath, modelPath } from '../../lib/deeplink'
import { formatBytes, formatDuration, formatValue, shortId } from '../../lib/format'
import { libraryPath, libraryPathOf } from '../../lib/libraryPath'
import { nameParts } from '../../lib/libraryNames'
import { folderLabel } from '../../lib/libraryTransfer'
import { useAsync } from '../../lib/useAsync'
import { BlobDownload } from '../BlobDownload'
import { libraryPrintsPath, printLabel, printPath, printerLabel } from '../prints/prints'
import { PrintStatus } from '../prints/PrintStatus'
import { Swatch } from '../print/Swatch'
import { RecordDrawer } from '../records/RecordDrawer'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import type { TransferMode } from './TransferLibraryFilesDialog'

const PlateScene = lazy(async () => ({ default: (await import('../print/PlateScene')).PlateScene }))

/** How many of the file's prints the drawer lists; the Prints page has the rest. */
const PRINTS_SHOWN = 5

/** The 3D view keeps the file's own colours: none swapped. One map, so it never changes. */
const NO_COLOURS = new Map<string, string>()

/** jsdom and a browser with WebGL off draw no 3D. */
function canDraw3d(): boolean {
  return typeof window !== 'undefined' && 'WebGLRenderingContext' in window
}

function background(): string {
  return getComputedStyle(document.documentElement).getPropertyValue('--sb-bg').trim() || '#0b0e13'
}

function problem(error: Error | undefined, fallback: string): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.detail : fallback
}

/** What kind of file it is, in words: "Sliced 3MF", "3MF", "STL". */
function fileKind(file: Pick<LibraryEntry, 'filename' | 'file_type' | 'sliced'>): string {
  if (file.sliced) return 'Sliced 3MF'
  const type = file.file_type || nameParts(file.filename).extension
  return type ? type.toUpperCase() : 'File'
}

interface Props {
  fileId: number
  /** The file as the page lists it; null when it is not listed (yet). */
  file: LibraryEntry | null
  /** Whether the page has read what it lists: a null `file` is then read by id. */
  listed: boolean
  folders: LibraryFolderView[]
  /** A search result's folder, by name, which the tree may not hold. */
  folderPath?: string[] | null
  actionsDisabled: boolean
  onClose: () => void
  onPrint: (file: LibraryEntry) => void
  onRename: (file: LibraryEntry) => void
  onTransfer: (file: LibraryEntry, mode: TransferMode) => void
  onDelete: (file: LibraryEntry) => void
  /**
   * The page's dialogs for the file (Print, Rename, Move, Delete), rendered in the
   * drawer's tree so a click in one is not a click outside the modal drawer.
   */
  dialogs?: ReactNode
}

/**
 * #1031 — one library file's details in a `RecordDrawer`: its pictures, facts, plates,
 * objects and filaments (the print dialog's reads), what it was sliced for, its prints,
 * and the output it was made from. Its actions are the card's own.
 */
export function LibraryFileDetails({
  fileId,
  file: listedFile,
  listed,
  folders,
  folderPath,
  actionsDisabled,
  onClose,
  onPrint,
  onRename,
  onTransfer,
  onDelete,
  dialogs,
}: Props) {
  // While the page reads its listing again (after a rename, a move), the file last listed
  // stays on screen.
  const [last, setLast] = useState<LibraryEntry | LibraryFileEntry | null>(listedFile)
  if (listedFile && listedFile !== last) setLast(listedFile)
  // #5321 — a file no listing on screen holds (one only Advanced lists, or one moved
  // away) is read alone, by id.
  const unlisted = listedFile === null && listed
  const read = useAsync(
    () => (unlisted ? api.getLibraryFile(fileId) : Promise.resolve(null)),
    [fileId, unlisted],
  )
  const file = listedFile ?? (unlisted ? (read.data ?? (read.loading ? last : null)) : last)
  const readError = unlisted && !read.loading ? problem(read.error, `Could not read file #${fileId}.`) : null
  // A file read by id names its folder's path itself.
  const where = folderPath ?? (file && 'folder_path' in file ? ((file as LibraryFileEntry).folder_path ?? null) : null)
  return (
    <RecordDrawer
      onClose={onClose}
      title={file ? <span className="break-all">{file.filename}</span> : `File #${fileId}`}
      actions={
        file && (
        <div className="flex flex-wrap items-center gap-2">
          {file.printable && (
            <Button size="sm" onClick={() => onPrint(file)}>
              <PrinterIcon /> Print
            </Button>
          )}
          <BlobDownload
            url={api.libraryFileUrl(file.id)}
            filename={file.filename}
            className="h-7 gap-1 rounded-md border px-2.5 text-[0.8rem] text-foreground no-underline hover:bg-muted hover:no-underline"
          >
            <DownloadIcon /> Download
          </BlobDownload>
          <Button size="sm" variant="outline" disabled={actionsDisabled} onClick={() => onRename(file)}>
            <PencilIcon /> Rename
          </Button>
          <Button size="sm" variant="outline" disabled={actionsDisabled} onClick={() => onTransfer(file, 'move')}>
            <FolderInputIcon /> Move to…
          </Button>
          <Button size="sm" variant="destructive" onClick={() => onDelete(file)}>
            <Trash2Icon /> Delete
          </Button>
        </div>
        )
      }
    >
      {file ? (
        <Details file={file} folders={folders} folderPath={where} />
      ) : readError ? (
        <p role="alert" className="text-sm text-muted-foreground">
          {readError}
        </p>
      ) : (
        <div role="status" aria-label="Reading the file" className="space-y-2">
          <Skeleton className="aspect-square w-full" />
          <Skeleton className="h-4 w-2/3" />
        </div>
      )}
      {dialogs}
    </RecordDrawer>
  )
}

function Section({ title, testId, children }: { title: string; testId?: string; children: ReactNode }) {
  return (
    <section data-testid={testId} className="space-y-2">
      <h3 className="text-sm font-medium">{title}</h3>
      {children}
    </section>
  )
}

function Reading() {
  return <Skeleton role="status" aria-label="Reading" className="h-10 w-full" />
}

function Fact({ term, children }: { term: string; children: ReactNode }) {
  return (
    <>
      <dt className="text-muted-foreground">{term}</dt>
      <dd className="min-w-0 break-words">{children}</dd>
    </>
  )
}

function Details({
  file,
  folders,
  folderPath,
}: {
  file: LibraryEntry
  folders: LibraryFolderView[]
  folderPath?: string[] | null
}) {
  const id = file.id
  const unsliced = file.printable && !file.sliced
  const plates = useAsync(() => (file.printable ? api.getLibraryPlates(id) : Promise.resolve([])), [id, file.printable])
  const choices = useAsync(
    () => (file.printable ? api.getLibraryChoices(id) : Promise.resolve(null)),
    [id, file.printable],
  )
  const objects = useAsync(() => (unsliced ? api.getLibraryObjects(id) : Promise.resolve(null)), [id, unsliced])
  const prints = useAsync(
    () => api.listPrints({ library_file_id: id }, { limit: PRINTS_SHOWN }),
    [id],
  )
  const outputId = file.output_id ?? null
  const output = useAsync(() => (outputId ? api.getOutput(outputId) : Promise.resolve(null)), [outputId])
  const [preview, setPreview] = useState<number | null>(null)

  const added = file.created_at ? new Date(file.created_at) : null
  const where = folderPath ? libraryPathOf(folderPath) : libraryPath(folders, file.folder_id ?? null)
  const whereLabel = folderPath
    ? folderPath.length === 0
      ? 'Top level'
      : folderPath.join(' / ')
    : folderLabel(folders, file.folder_id ?? null)
  const sliced = choices.data?.sliced ?? null
  const slots = choices.data?.filaments.slots ?? []
  const draw = unsliced && canDraw3d()

  return (
    <>
      {file.has_thumbnail ? (
        <img
          src={api.libraryThumbnailUrl(id)}
          alt={`Thumbnail of ${file.filename}`}
          className="aspect-square w-full rounded-lg bg-muted/50 object-contain"
        />
      ) : (
        <div aria-hidden className="grid aspect-video w-full place-items-center rounded-lg bg-muted/50">
          <span className="text-sm font-medium tracking-wide text-muted-foreground uppercase">{fileKind(file)}</span>
        </div>
      )}

      <dl data-testid="library-details-facts" className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
        <Fact term="Type">{fileKind(file)}</Fact>
        {file.file_size != null && <Fact term="Size">{formatBytes(file.file_size)}</Fact>}
        {added && !Number.isNaN(added.getTime()) && (
          <Fact term="Added">
            <time dateTime={file.created_at ?? undefined}>
              {added.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
            </time>
          </Fact>
        )}
        <Fact term="Folder">
          <Link to={where} className="text-primary underline-offset-4 hover:underline">
            {whereLabel}
          </Link>
        </Fact>
        {file.is_external && <Fact term="Source">Linked from an external folder</Fact>}
      </dl>

      {outputId && (
        <Section title="Made by ScadBuddy" testId="library-details-origin">
          {output.loading ? (
            <Reading />
          ) : output.data ? (
            <div className="space-y-2 text-sm">
              <p>
                {output.data.name ?? shortId(output.data.id)}, from{' '}
                <Link to={modelPath(output.data.slug)} className="text-primary underline-offset-4 hover:underline">
                  {output.data.slug}
                </Link>
                {output.data.model_version && (
                  <span className="text-muted-foreground"> at revision {output.data.model_version.slice(0, 7)}</span>
                )}
              </p>
              {Object.keys(output.data.params ?? {}).length > 0 && (
                <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-xs">
                  {Object.entries(output.data.params ?? {}).map(([name, value]) => (
                    <Fact key={name} term={name}>
                      {formatValue(value)}
                    </Fact>
                  ))}
                </dl>
              )}
              <Button size="sm" variant="outline" asChild>
                <Link to={editPath(output.data.id)}>Open in the customizer</Link>
              </Button>
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              {problem(output.error, 'Could not read the output it was made from.')}
            </p>
          )}
        </Section>
      )}

      {file.printable && (
        <Section title="Plates" testId="library-details-plates">
          {plates.loading ? (
            <Reading />
          ) : plates.error ? (
            <p className="text-sm text-muted-foreground">{problem(plates.error, 'Could not read its plates.')}</p>
          ) : (plates.data ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No plates to show.</p>
          ) : (
            <>
              <ul className="grid grid-cols-3 gap-2">
                {(plates.data ?? []).map((plate) => {
                  const label = plate.name ?? `Plate ${plate.index}`
                  return (
                    <li key={plate.index} className="min-w-0 space-y-1">
                      {plate.has_thumbnail ? (
                        <img
                          src={api.libraryPlateThumbnailUrl(id, plate.index)}
                          alt={label}
                          loading="lazy"
                          className="aspect-square w-full rounded-md bg-muted/50 object-contain"
                        />
                      ) : (
                        <div className="grid aspect-square w-full place-items-center rounded-md bg-muted/50 text-xs text-muted-foreground">
                          {label}
                        </div>
                      )}
                      <p className="flex items-center justify-between gap-1 text-xs">
                        <span className="min-w-0 truncate">{label}</span>
                        {draw && (
                          <Button
                            size="xs"
                            variant={preview === plate.index ? 'default' : 'ghost'}
                            aria-label={`Show ${label} in 3D`}
                            aria-pressed={preview === plate.index}
                            onClick={() => setPreview(preview === plate.index ? null : plate.index)}
                          >
                            <BoxIcon /> 3D
                          </Button>
                        )}
                      </p>
                    </li>
                  )
                })}
              </ul>
              {draw && preview !== null && (
                <div className="h-[260px] overflow-hidden rounded-lg border" data-testid="library-details-3d">
                  <Suspense fallback={<Skeleton className="size-full" />}>
                    <PlateScene
                      key={preview}
                      url={api.libraryPreviewGlbUrl(id, preview)}
                      colors={NO_COLOURS}
                      cutAt={null}
                      onHeight={() => undefined}
                      background={background()}
                    />
                  </Suspense>
                </div>
              )}
            </>
          )}
        </Section>
      )}

      {file.printable && (
        <Section title="Filaments" testId="library-details-slots">
          {choices.loading ? (
            <Reading />
          ) : choices.error ? (
            <p className="text-sm text-muted-foreground">{problem(choices.error, 'Could not read its filaments.')}</p>
          ) : slots.length === 0 ? (
            <p className="text-sm text-muted-foreground">The file names no filaments.</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {slots.map((slot) => (
                <li key={slot.slot_id} className="flex items-center gap-2">
                  <Swatch colour={slot.colour} size="sm" />
                  <span className="text-muted-foreground">Slot {slot.slot_id}:</span>
                  <span>{slot.material ?? 'Unknown material'}</span>
                  {slot.used_grams != null && (
                    <span className="sb-num ml-auto text-muted-foreground">{slot.used_grams.toFixed(1)} g</span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {sliced && (
        <Section title="Sliced for" testId="library-details-sliced">
          <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
            {sliced.printer_model && <Fact term="Printer">{sliced.printer_model}</Fact>}
            {(sliced.nozzle_diameters ?? []).length > 0 && (
              <Fact term="Nozzle">{(sliced.nozzle_diameters ?? []).map((size) => `${size} mm`).join(', ')}</Fact>
            )}
            {sliced.process && <Fact term="Process">{sliced.process}</Fact>}
            {sliced.bed_type && <Fact term="Plate type">{sliced.bed_type}</Fact>}
          </dl>
          {(sliced.plates ?? []).length > 0 && (
            <ul className="space-y-0.5 text-sm">
              {(sliced.plates ?? []).map((plate) => (
                <li key={plate.index} className="sb-num flex gap-3">
                  <span className="text-muted-foreground">Plate {plate.index}</span>
                  {plate.print_seconds != null && <span>{formatDuration(plate.print_seconds)}</span>}
                  {plate.weight_g != null && <span>{plate.weight_g.toFixed(1)} g</span>}
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {unsliced && (
        <Section title="Objects" testId="library-details-objects">
          {objects.loading ? (
            <Reading />
          ) : objects.error ? (
            <p className="text-sm text-muted-foreground">{problem(objects.error, 'Could not read its objects.')}</p>
          ) : (
            <ul className="space-y-1 text-sm">
              {(objects.data?.objects ?? []).map((object) => (
                <li key={object.part} className="flex items-center gap-2">
                  <span className="flex gap-0.5">
                    {object.colours.map((colour) => (
                      <Swatch key={colour} colour={colour} size="sm" />
                    ))}
                  </span>
                  <span className="min-w-0 truncate">{object.name}</span>
                  {object.count > 1 && <span className="text-muted-foreground">× {object.count}</span>}
                  <span className="sb-num ml-auto text-xs text-muted-foreground">
                    {object.size.map((side) => Math.round(side)).join(' × ')} mm
                  </span>
                </li>
              ))}
            </ul>
          )}
        </Section>
      )}

      {file.printable && (
        <Section title="Prints" testId="library-details-prints">
          {prints.loading ? (
            <Reading />
          ) : prints.error ? (
            <p className="text-sm text-muted-foreground">{problem(prints.error, 'Could not read its prints.')}</p>
          ) : (prints.data?.items ?? []).length === 0 ? (
            <p className="text-sm text-muted-foreground">No prints of this file yet.</p>
          ) : (
            <ul className="space-y-1.5 text-sm">
              {(prints.data?.items ?? []).map((print) => {
                const when = print.started_at ? new Date(print.started_at) : null
                return (
                  <li key={print.archive_id} className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
                    <Link to={printPath(print.archive_id)} className="min-w-0 truncate text-primary hover:underline">
                      {printLabel(print)}
                    </Link>
                    <PrintStatus status={print.status} />
                    <span className="text-xs text-muted-foreground">
                      {[
                        print.printer_name ?? (print.printer_id != null ? printerLabel(print.printer_id) : null),
                        when && !Number.isNaN(when.getTime())
                          ? when.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
                          : null,
                      ]
                        .filter(Boolean)
                        .join(', ')}
                    </span>
                  </li>
                )
              })}
            </ul>
          )}
          <Link to={libraryPrintsPath(id)} className="text-sm text-primary underline-offset-4 hover:underline">
            All prints of this file
          </Link>
        </Section>
      )}
    </>
  )
}
