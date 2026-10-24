import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate, useParams } from 'react-router'
import { USER_ONLY } from '../agent/dom'
import { ApiError, api } from '../api/client'
import type { CustomizerSchema, LibraryCopy, ModelLibrary, Output, PrintSummary } from '../api/types'
import { ArrangeDialog } from '../components/ArrangeDialog'
import { ArrangedNext } from '../components/ArrangedNext'
import { LazyArrangedPlates } from '../components/ArrangedPlates'
import { BlobDownload } from '../components/BlobDownload'
import { BomTable } from '../components/BomTable'
import { ColorStrip } from '../components/ColorStrip'
import { statusLabel } from '../components/prints/status'
import { PrintPicker } from '../components/PrintPicker'
import { AddToOrderDialog } from '../components/orders/AddToOrderDialog'
import { SendDialog } from '../components/SendDialog'
import { PageLoading } from '../components/PageState'
import { Spinner } from '../components/ui/spinner'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Dialog, DialogContent, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { MoreHorizontalIcon } from 'lucide-react'
import { editPath, editTargetFor, modelPath, type EditNavigationState } from '../lib/deeplink'
import { arrangedSources, fromOutputs, savedNote, type Arranged } from '../lib/arrange'
import { traceAction } from '../lib/traceAction'
import { wasSent } from '../lib/sent'
import { formatBbox, formatValue, shortId, timeAgo } from '../lib/format'
import { plateLabel } from '../lib/plate'
import { useDisplayUnit } from '../lib/units'
import { diffFromDefaults } from '../lib/params'
import { useAsync } from '../lib/useAsync'
import { bambuddyBase, webUrls } from '../lib/bambuddyLinks'
import { libraryLabel, pinChange } from '../lib/libraryPins'

export function HistoryPage() {
  const { slug = '' } = useParams()
  const navigate = useNavigate()
  const schemaState = useAsync(() => api.getSchema(slug), [slug])
  // #939 — the breadcrumb names the model by its record. The schema's `title` is the
  // .scad file OpenSCAD exported, "model" for every model.
  const modelState = useAsync(() => api.getModel(slug), [slug], [`model:${slug}`])
  // #269 — live: outputs saved or deleted elsewhere show up here. Print progress is
  // on `print:<output id>`, which this list does not follow.
  const outputsState = useAsync(() => api.listOutputs(slug), [slug], [`model:${slug}`])
  // The rows as they are now, for an edit that lands after an await (#2786 review): a
  // rename or a delete answered late must not bring back what changed meanwhile.
  const rows = useRef(outputsState.data)
  useEffect(() => {
    rows.current = outputsState.data
  })
  // #89 — an output records Bambuddy's ids, never a URL, so the base to deep-link them
  // against comes from Settings. Until it answers, the ids still read as plain text.
  const settings = useAsync(() => api.getSettings(), []).data
  const bambuddyUrl = bambuddyBase(webUrls(settings)) ?? undefined
  const [sendFor, setSendFor] = useState<Output | undefined>(undefined)
  const [deleting, setDeleting] = useState<string | null>(null)
  // #316, #2369 — every delete asks first; one with copies in Bambuddy offers the inbox ones.
  const [confirmFor, setConfirmFor] = useState<Output | undefined>(undefined)
  /** §7 — outputs ticked for the next Arrange (#314). */
  const [picked, setPicked] = useState<string[]>([])
  const [arranging, setArranging] = useState(false)
  /** #2287 — the last Arrange's result, with its next step, until the page is left. */
  const [arrangedOutput, setArrangedOutput] = useState<Output | null>(null)
  /** #2287 — the output in the print dialog (History's Print, or the arranged result's). */
  const [printFor, setPrintFor] = useState<Output | undefined>(undefined)
  /** #2288 — the dialog was opened by Queue: the print waits for a manual start. */
  const [queueing, setQueueing] = useState(false)
  /** #2288 — the output whose preview is open large. */
  const [previewFor, setPreviewFor] = useState<Output | undefined>(undefined)
  /** #4249 — "Order this": the output becomes a line item of an order. */
  const [orderFor, setOrderFor] = useState<Output | undefined>(undefined)
  /**
   * #2288 — each output's newest print as the Prints page has it, so a row says how its
   * print went rather than only that it was queued. Best effort: unread, rows say less.
   * Only the slug's newest 100 prints are read (`/prints` has no filter by output), so an
   * output whose last print is older than those falls back to its queue id, as if it
   * had not printed yet.
   */
  // Read again when any listed output's print starts or settles (#2786 review): a print
  // that finishes while the page is open says so without a reload. Not on its progress
  // ticks: each would be a read of Bambuddy's prints for an outcome that has not changed.
  const printsState = useAsync(
    () => api.listPrints({ slug }, { limit: 100 }).catch(() => ({ items: [], next_cursor: null })),
    [slug],
    (outputsState.data ?? []).map((output) => `print:${output.id}`),
    (signal) => signal === 'resync' || signal.kind !== 'print.progress',
  )
  const lastPrints = new Map<string, PrintSummary>()
  // Newest first: the first one seen for an output is its last print.
  for (const print of printsState.data?.items ?? []) {
    if (print.output_id && !lastPrints.has(print.output_id)) lastPrints.set(print.output_id, print)
  }
  /** #902 — the outputs the last Arrange could not re-render, said once it has closed. */
  const [skipped, setSkipped] = useState<string | null>(null)
  /** #2284 — the last Arrange's result, said by where it was filed: maybe another model. */
  const [saved, setSaved] = useState<Arranged | null>(null)
  const togglePicked = (id: string) =>
    setPicked((current) => (current.includes(id) ? current.filter((p) => p !== id) : [...current, id]))

  async function remove(id: string, deleteInboxCopies = false) {
    setDeleting(id)
    try {
      // A user action of its own in Tempo (#2292), the delete its child.
      await traceAction('delete', { 'scadbuddy.output_id': id }, () => api.deleteOutput(id, deleteInboxCopies))
      outputsState.setData((rows.current ?? []).filter((o) => o.id !== id))
      setPicked((current) => current.filter((p) => p !== id))
      setConfirmFor(undefined)
    } finally {
      setDeleting(null)
    }
  }

  const loading = schemaState.loading || outputsState.loading
  const schema = schemaState.data
  /** #2287 — the arranged result's row as the list has it now: gone once deleted. */
  const arrangedRow = arrangedOutput ? outputsState.data?.find((o) => o.id === arrangedOutput.id) : undefined

  return (
    <div className="h-full overflow-y-auto">
      <div className="mx-auto max-w-4xl px-4 py-6">
        <div className="mb-5 flex items-baseline gap-2">
          <Link to="/" className="text-sm text-muted-foreground hover:text-foreground">
            Models
          </Link>
          <span className="text-muted-foreground">/</span>
          <Link to={modelPath(slug)} className="text-sm text-muted-foreground hover:text-foreground">
            {modelState.data?.name ?? slug}
          </Link>
          <span className="text-muted-foreground">/</span>
          <h1 className="text-sm font-medium">History</h1>
        </div>

        {loading && <PageLoading label="Loading history" />}

        {!loading && outputsState.data?.length === 0 && (
          <Card className="border border-dashed text-center ring-0">
            <CardHeader>
              <CardTitle>
                <h2>Nothing generated yet</h2>
              </CardTitle>
              <CardDescription>Every output you generate is kept here with the parameters that made it.</CardDescription>
            </CardHeader>
            <CardContent>
              <Button type="button" onClick={() => void navigate(modelPath(slug))}>
                Open the customizer
              </Button>
            </CardContent>
          </Card>
        )}

        {!loading && schema && outputsState.data && outputsState.data.length > 0 && (
          <>
            {/* Gone from the list (deleted): nothing to offer a next step for. */}
            {arrangedOutput && arrangedRow && (
              <div className="mb-2">
                <ArrangedNext
                  output={arrangedOutput}
                  sent={wasSent(arrangedRow)}
                  onPrint={() => setPrintFor(arrangedOutput)}
                />
              </div>
            )}
            <div className="mb-2 flex justify-end">
              <Button
                type="button"
                variant="outline"
                size="sm"
                disabled={picked.length === 0}
                onClick={() => {
                  setSkipped(null)
                  setSaved(null)
                  setArranging(true)
                }}
              >
                Arrange selected ({picked.length})
              </Button>
            </div>
            {skipped && (
              <p role="alert" className="mb-2 text-sm text-destructive">
                {skipped}
              </p>
            )}
            {saved && (
              <p role="status" aria-label="Arranged" className="mb-2 text-sm">
                {savedNote(saved, slug)}
                {saved.output.slug !== slug && (
                  <>
                    {' '}
                    <Link to={modelPath(saved.output.slug, 'history')} className="text-primary underline-offset-4 hover:underline">
                      Open in History
                    </Link>
                  </>
                )}
              </p>
            )}
            <ul data-testid="outputs" aria-label="Generated outputs" className="space-y-2">
              {outputsState.data.map((output) => (
                <OutputRow
                  key={output.id}
                  output={output}
                  schema={schema}
                  deleting={deleting === output.id}
                  picked={picked.includes(output.id)}
                  onPick={() => togglePicked(output.id)}
                  onEdit={() =>
                    void navigate(editPath(output.id), {
                      state: { editTarget: editTargetFor(output) } satisfies EditNavigationState,
                    })
                  }
                  onSend={() => setSendFor(output)}
                  onPrint={() => setPrintFor(output)}
                  onOrder={() => setOrderFor(output)}
                  onDelete={() => setConfirmFor(output)}
                  bambuddyUrl={bambuddyUrl}
                  pinned={modelState.data?.libraries}
                  onQueue={() => {
                    setQueueing(true)
                    setPrintFor(output)
                  }}
                  onPreview={() => setPreviewFor(output)}
                  onRenamed={(renamed) =>
                    outputsState.setData((rows.current ?? []).map((o) => (o.id === renamed.id ? renamed : o)))
                  }
                  lastPrint={lastPrints.get(output.id)}
                  // Its plates show once, in the next-step card above (#2754 review).
                  platesAbove={arrangedRow?.id === output.id}
                />
              ))}
            </ul>
          </>
        )}
      </div>

      <DeleteOutputDialog
        output={confirmFor}
        // Undefined until the settings load: every copy then reads as not yet placed,
        // rather than being labelled against a guessed inbox.
        inboxFolderId={settings === undefined ? undefined : (settings.library_folder_id ?? null)}
        deleting={confirmFor !== undefined && deleting === confirmFor.id}
        onClose={() => setConfirmFor(undefined)}
        onConfirm={(output, deleteInboxCopies) => remove(output.id, deleteInboxCopies)}
      />

      <PrintPicker
        open={printFor !== undefined}
        source={printFor ? { kind: 'output', output: printFor } : undefined}
        queue={queueing}
        // #3651 — an arranged output opens on the project its sources are all in.
        content={printFor && saved?.output.id === printFor.id ? saved.from : undefined}
        onClose={() => {
          setPrintFor(undefined)
          // A Queue lasts as long as its dialog: the next one opened is a Print unless asked.
          setQueueing(false)
        }}
        onRan={() => {
          outputsState.reload()
          printsState.refresh()
        }}
      />
      <Dialog open={previewFor !== undefined} onOpenChange={(open) => !open && setPreviewFor(undefined)}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>{previewFor ? (previewFor.name ?? shortId(previewFor.id)) : ''}</DialogTitle>
          </DialogHeader>
          {previewFor && (
            <img
              src={api.outputThumbnailUrl(previewFor.id)}
              alt={`Preview of ${previewFor.name ?? shortId(previewFor.id)}`}
              className="mx-auto max-h-[70vh] w-auto rounded-lg bg-muted"
            />
          )}
        </DialogContent>
      </Dialog>
      <SendDialog
        open={sendFor !== undefined}
        output={sendFor}
        onClose={() => setSendFor(undefined)}
        onSent={() => outputsState.reload()}
      />
      {orderFor && <AddToOrderDialog open outputId={orderFor.id} outputName={orderFor.name} onClose={() => setOrderFor(undefined)} />}
      <ArrangeDialog
        open={arranging}
        sources={fromOutputs((outputsState.data ?? []).filter((o) => picked.includes(o.id)))}
        onClose={() => setArranging(false)}
        onArranged={(arranged) => {
          setSkipped(arranged.skipped ?? null)
          setSaved(arranged)
          setArranging(false)
          setPicked([])
          setArrangedOutput(arranged.output)
          // #2283 — in place: the list stays up and gains the new row, rather than
          // dropping to "Loading history" until the read answers.
          outputsState.refresh()
        }}
      />
    </div>
  )
}

/** #2288 — a print's outcome in its colour: green only for one that succeeded. */
function printTone(status: string): string {
  if (status === 'completed') return 'text-ok'
  if (status === 'failed' || status === 'cancelled') return 'text-destructive'
  return 'text-muted-foreground'
}

/** The copy the output was last uploaded as; copies are recorded in upload order. */
function lastCopy(output: Output): LibraryCopy | undefined {
  const copies = output.library_files ?? []
  return copies[copies.length - 1]
}

type CopyPlace = 'inbox' | 'project' | 'unknown'

function placeOf(copy: LibraryCopy, inboxFolderId: number | null | undefined): CopyPlace {
  if (inboxFolderId === undefined) return 'unknown'
  return (copy.folder_id ?? null) === inboxFolderId ? 'inbox' : 'project'
}

/**
 * #2369 — every output delete asks first: it cannot be undone.
 * #316 — for an output that has copies in Bambuddy's library, the copies in the
 * inbox folder can go with it; a copy in a project's folder is that project's record
 * of what it printed and always stays. While the inbox folder is not known yet, the
 * server decides, and deletes a copy only if it is in the inbox.
 */
function DeleteOutputDialog({
  output,
  inboxFolderId,
  deleting,
  onClose,
  onConfirm,
}: {
  output: Output | undefined
  inboxFolderId: number | null | undefined
  deleting: boolean
  onClose: () => void
  onConfirm: (output: Output, deleteInboxCopies: boolean) => Promise<void>
}) {
  const [alsoInbox, setAlsoInbox] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const copies = output?.library_files ?? []
  const count = (place: CopyPlace) =>
    copies.filter((copy) => placeOf(copy, inboxFolderId) === place).length
  const removable = count('inbox') + count('unknown')

  function close() {
    if (deleting) return
    setAlsoInbox(false)
    setError(null)
    onClose()
  }

  async function confirm() {
    if (!output) return
    setError(null)
    try {
      await onConfirm(output, alsoInbox && removable > 0)
      setAlsoInbox(false)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    }
  }

  const alsoId = `also-inbox-${output?.id ?? ''}`
  return (
    <AlertDialog open={output !== undefined} onOpenChange={(open) => !open && close()}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{`Delete ${output?.name ?? (output ? shortId(output.id) : '')}?`}</AlertDialogTitle>
          <AlertDialogDescription>
            The output, its file and its parameters are deleted. This cannot be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {copies.length > 0 && (
          <div className="flex flex-col gap-2 text-sm">
            <p className="text-muted-foreground">This output has copies in Bambuddy&apos;s library:</p>
            <ul className="list-disc pl-5 text-muted-foreground" aria-label="Library copies">
              {copies.map((copy) => {
                const place = placeOf(copy, inboxFolderId)
                return (
                  <li key={copy.id}>
                    <span className="sb-num">#{copy.id}</span>{' '}
                    {place === 'inbox'
                      ? 'in the inbox folder'
                      : place === 'project'
                        ? 'in a project folder, kept'
                        : 'folder not recorded; checked before deleting'}
                  </li>
                )
              })}
            </ul>
            {removable > 0 ? (
              <div className="flex items-center gap-2">
                <Checkbox id={alsoId} checked={alsoInbox} onCheckedChange={(next) => setAlsoInbox(next === true)} />
                <Label htmlFor={alsoId} className="font-normal">
                  Also delete the inbox copies in Bambuddy
                </Label>
              </div>
            ) : (
              <p className="text-muted-foreground">Copies in project folders stay in Bambuddy.</p>
            )}
            <p className="text-xs text-muted-foreground">Sliced files and project copies are never deleted from here.</p>
          </div>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
          <Button type="button" variant="destructive" onClick={() => void confirm()} disabled={deleting} {...USER_ONLY}>
            {deleting ? <Spinner /> : 'Delete output'}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

/** #4491 — a row's actions are a finger's target on a phone: 40 px tall, below `sm`. */
const PHONE_TAP = 'max-sm:h-10'
/** #4524 — a link on a row (Download 3MF, the size line's) the same, its text centred. */
const PHONE_TAP_LINK = `max-sm:inline-flex max-sm:items-center ${PHONE_TAP}`

/**
 * A recorded Bambuddy id, linked to the page it means something on (#89). A queue item
 * links to its own page, and a library file to the library. `target=_blank` because
 * ScadBuddy renders inside Bambuddy's sandboxed iframe (spec §1).
 */
function BambuddyId({
  href,
  className,
  children,
}: {
  href: string | undefined
  className: string
  children: ReactNode
}) {
  if (!href) return <span className={className}>{children}</span>
  return (
    <a
      className={`${className} underline decoration-dotted underline-offset-2 ${PHONE_TAP_LINK}`}
      href={href}
      target="_blank"
      rel="noopener noreferrer"
    >
      {children}
    </a>
  )
}

/** #1296 — the libraries and commits an output was rendered with. */
function OutputLibraries({
  libraries,
  pinned,
}: {
  libraries: ModelLibrary[]
  pinned: ModelLibrary[] | undefined
}) {
  if (libraries.length === 0) return null
  return (
    <p className="sb-num mt-1 text-xs text-muted-foreground" aria-label="Rendered with libraries">
      {libraries.map((library, index) => {
        const change = pinChange(library, pinned)
        return (
          // #2109: real text separators, so the line does not read (or copy) as one run-together
          // token; the comma is for the text only, the margin spaces the line on screen.
          <span key={library.name} className={index > 0 ? 'ml-3' : undefined}>
            {index > 0 && <span className="sr-only">, </span>}
            {libraryLabel(library)}
            {change && (
              <>
                {' '}
                <span className="text-destructive">({change})</span>
              </>
            )}
          </span>
        )
      })}
    </p>
  )
}

function OutputRow({
  output,
  schema,
  deleting,
  picked,
  onPick,
  onEdit,
  onSend,
  onPrint,
  onOrder,
  onQueue,
  onPreview,
  onRenamed,
  lastPrint,
  onDelete,
  bambuddyUrl,
  pinned,
  platesAbove = false,
}: {
  output: Output
  schema: CustomizerSchema
  deleting: boolean
  /** §7 — ticked for the next Arrange. */
  picked: boolean
  onPick: () => void
  onEdit: () => void
  onSend: () => void
  onPrint: () => void
  /** #4249 — the output as a line item of an order. */
  onOrder: () => void
  /** #2288 — the print dialog, set to wait in Bambuddy's queue for a manual start. */
  onQueue: () => void
  /** #2288 — the row's preview, large. */
  onPreview: () => void
  onRenamed: (output: Output) => void
  /** #2288 — the output's newest print, from the Prints page's own read. */
  lastPrint: PrintSummary | undefined
  onDelete: () => void
  bambuddyUrl: string | undefined
  /** The model's current pins; undefined until the model has loaded, so nothing is flagged. */
  pinned: ModelLibrary[] | undefined
  /** The just-arranged output, whose plates `ArrangedNext` shows above the list already. */
  platesAbove?: boolean
}) {
  const diff = diffFromDefaults(schema, output.params ?? {})
  const unit = useDisplayUnit()
  const queuedPlates = output.plates ?? []
  // #986 — a printed plate by what it holds. Read only for an output printed plate by
  // plate; an unreadable list leaves the numbers.
  const plateNames = useAsync(
    () => (queuedPlates.length > 1 ? api.getOutputPlates(output.id).catch(() => []) : Promise.resolve([])),
    [output.id, queuedPlates.length > 1],
  ).data ?? []
  const sent = wasSent(output)
  /** An arranged output has no template inputs to reopen in the customizer. */
  const arrangedFrom = (output.arranged_from ?? []).length
  // #975 — what each row's buttons are named after, so a list of them can tell the rows apart.
  const label = output.name ?? shortId(output.id)
  /** #2288 — the name being typed, while Rename is open. */
  const [naming, setNaming] = useState<string | null>(null)
  const [renaming, setRenaming] = useState(false)
  const [renameError, setRenameError] = useState<string | null>(null)
  /** #4520 — Rename was chosen: the menu's close hands the focus to the name field. */
  const toNameField = useRef(false)
  const nameField = useRef<HTMLInputElement>(null)

  async function rename(name: string) {
    setRenaming(true)
    setRenameError(null)
    try {
      onRenamed(await api.renameOutput(output.id, name.trim() || null))
      setNaming(null)
    } catch (caught) {
      setRenameError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setRenaming(false)
    }
  }

  return (
    <li>
      <Card size="sm" className="px-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        {/* #2288 — every output's picture; an arranged one also shows its plates below. */}
        {output.has_thumbnail ? (
          <button
            type="button"
            onClick={onPreview}
            aria-label={`Preview ${label}`}
            className="shrink-0 rounded-lg border bg-muted outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
          >
            <img src={api.outputThumbnailUrl(output.id)} alt="" className="h-16 w-16 rounded-lg object-contain" />
          </button>
        ) : (
          <div
            aria-label={`No preview of ${label}`}
            className="flex h-16 w-16 shrink-0 items-center justify-center rounded-lg border border-dashed text-center text-[10px] text-muted-foreground"
          >
            No preview
          </div>
        )}
        {/* #4491 — at least 10rem beside the picture, so on a phone the actions wrap below
            instead of squeezing this column to one word wide. Rename's form takes its own
            line and wraps Save and Cancel below the field when 10rem is all there is
            (320 px); e2e/phone-width.spec.ts opens it at each width. */}
        <div className="min-w-40 flex-1" data-testid="output-summary">
          {/* #4491 — wraps: the chips keep a row, the name truncates, the age stays one line. */}
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1">
            <Checkbox aria-label={`Select ${output.name ?? shortId(output.id)}`} checked={picked} onCheckedChange={onPick} />
            <ColorStrip colors={output.colors ?? []} size="sm" />
            {naming === null ? (
              <span
                className="max-w-full min-w-0 truncate text-sm font-medium"
                title={label}
                data-testid="output-name"
              >
                {label}
              </span>
            ) : (
              <form
                className="flex w-full flex-wrap items-center gap-1.5"
                onSubmit={(event) => {
                  event.preventDefault()
                  void rename(naming)
                }}
              >
                <Input
                  ref={nameField}
                  aria-label={`Name for ${label}`}
                  value={naming}
                  placeholder={shortId(output.id)}
                  onChange={(event) => setNaming(event.target.value)}
                  maxLength={200}
                  autoFocus
                  className="h-7 min-w-32 flex-1"
                />
                <Button size="sm" type="submit" disabled={renaming}>
                  {renaming ? <Spinner /> : 'Save'}
                </Button>
                <Button type="button" size="sm" variant="ghost" disabled={renaming} onClick={() => setNaming(null)}>
                  Cancel
                </Button>
              </form>
            )}
            <span className="text-xs whitespace-nowrap text-muted-foreground" data-testid="output-age">
              {timeAgo(output.created_at)}
            </span>
          </div>
          {renameError && (
            <p role="alert" className="mt-1 text-xs text-destructive" data-testid="output-rename-error">
              {renameError}
            </p>
          )}
          {/* #4491 — each fact on one line, the line wrapping between them. */}
          <p className="sb-num mt-1 flex flex-wrap gap-x-2 text-xs text-muted-foreground">
            <span className="whitespace-nowrap">
              {output.bbox_mm ? formatBbox(output.bbox_mm, unit) : 'No dimensions recorded'}
            </span>
            {/* #2288 — how its last print went, linked to ScadBuddy's print page. Being
                queued says nothing of that, so the queue ids are neutral and show only
                until a print is on record. */}
            {lastPrint && (
              <Link
                to={`/prints/${lastPrint.archive_id}`}
                className={`underline underline-offset-2 ${PHONE_TAP_LINK} ${printTone(lastPrint.status)}`}
              >
                {`Last print: ${statusLabel(lastPrint.status)}`}
              </Link>
            )}
            {!lastPrint &&
              (output.plates ?? []).length > 1 &&
              (output.plates ?? []).map((plate) => (
                <BambuddyId
                  key={plate.queue_item_id}
                  className="whitespace-nowrap text-muted-foreground"
                  href={bambuddyUrl && `${bambuddyUrl}/queue/${plate.queue_item_id}`}
                >
                  {plateLabel(plateNames, plate.plate_id)} queued #{plate.queue_item_id}
                </BambuddyId>
              ))}
            {!lastPrint && output.queue_item_id && (output.plates ?? []).length <= 1 && (
              <BambuddyId
                className="whitespace-nowrap text-muted-foreground"
                href={bambuddyUrl && `${bambuddyUrl}/queue/${output.queue_item_id}`}
              >
                queued #{output.queue_item_id}
              </BambuddyId>
            )}
            {!output.queue_item_id && lastCopy(output) && (
              <BambuddyId
                className="whitespace-nowrap text-muted-foreground"
                href={bambuddyUrl && `${bambuddyUrl}/library`}
              >
                in library #{lastCopy(output)?.id}
              </BambuddyId>
            )}
          </p>
          <OutputLibraries libraries={output.libraries ?? []} pinned={pinned} />
          {/* #2282 — an arranged output's layout is what it is: its plates, on the row. */}
          {arrangedFrom > 0 && !platesAbove && (
            <div data-testid="output-plates">
              <LazyArrangedPlates outputId={output.id} size="sm" />
            </div>
          )}
        </div>

        <div className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid="output-actions">
          {arrangedFrom > 0 ? (
            <span className="text-xs text-muted-foreground">
              {`Arranged from ${arrangedSources(output.arranged_from ?? [])}`}
            </span>
          ) : (
            <Button type="button" variant="outline" size="sm" className={PHONE_TAP} onClick={onEdit} aria-label={`Edit ${label}`}>
              Edit
            </Button>
          )}
          <Button type="button" size="sm" className={PHONE_TAP} onClick={onPrint} aria-label={`Print ${label}`}>
            Print
          </Button>
          {/* #2287 — "again" only for an output that went to Bambuddy before. */}
          <Button
            type="button"
            variant="outline"
            size="sm"
            className={PHONE_TAP}
            onClick={onSend}
            aria-label={`${sent ? 'Send again' : 'Send'} ${label}`}
          >
            {sent ? 'Send again' : 'Send'}
          </Button>
          <BlobDownload
            url={api.downloadUrl(output.id)}
            // The server names it (`download_filename`), as every other download of it.
            filename={`${output.slug}-${output.id}.3mf`}
            className={`px-1 text-sm text-primary underline-offset-4 hover:underline ${PHONE_TAP_LINK}`}
          >
            Download 3MF
          </BlobDownload>
          {/* #3672 — the row's other actions, in one menu. */}
          <DropdownMenu modal={false}>
            <DropdownMenuTrigger asChild>
              <Button
                type="button"
                variant="ghost"
                size="icon-sm"
                className={`max-sm:w-10 ${PHONE_TAP}`}
                aria-label={`More for ${label}`}
                disabled={deleting}
              >
                {deleting ? <Spinner /> : <MoreHorizontalIcon />}
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent
              align="end"
              className="w-auto min-w-40"
              onCloseAutoFocus={(event) => {
                // #4520 — not back to the ⋯ button: the closing menu took the focus from the
                // field's autoFocus, so give it back here, once the menu has gone.
                if (!toNameField.current) return
                toNameField.current = false
                event.preventDefault()
                nameField.current?.focus()
              }}
            >
              <DropdownMenuItem onSelect={() => setTimeout(onQueue, 0)} aria-label={`Queue ${label}`}>
                Queue
              </DropdownMenuItem>
              <DropdownMenuItem onSelect={() => setTimeout(onOrder, 0)} aria-label={`Order ${label}`}>
                Order this
              </DropdownMenuItem>
              <DropdownMenuItem
                disabled={naming !== null}
                onSelect={() => {
                  toNameField.current = true
                  setRenameError(null)
                  setNaming(output.name ?? '')
                }}
                aria-label={`Rename ${label}`}
              >
                Rename
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                variant="destructive"
                onSelect={() => setTimeout(onDelete, 0)}
                aria-label={`Delete ${label}`}
                {...USER_ONLY}
              >
                Delete
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </div>

      <div className="border-t pt-2.5">
        {diff.length === 0 ? (
          <p className="text-xs text-muted-foreground">Model defaults, unchanged.</p>
        ) : (
          // #1036 — the caption column is capped at half the card: as `auto`, a long caption
          // took the whole row on a phone and pushed the values off the screen.
          <dl className="grid grid-cols-[fit-content(50%)_minmax(0,1fr)] gap-x-4 gap-y-1 text-xs">
            {diff.map((entry) => (
              <div key={entry.name} className="contents">
                <dt className="text-muted-foreground">{entry.caption}</dt>
                <dd className="sb-num min-w-0">
                  <span className="text-foreground">{formatValue(entry.value)}</span>
                  {/* #975 — read as "21, default 20", not "2120": the strike-through alone is CSS. */}
                  <span className="sr-only">, default </span>
                  <del className="ml-2 text-muted-foreground">{formatValue(entry.initial)}</del>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {/* A pipeline output's bill of materials and extra files (spec 2026-09-27 §5.2). */}
      <BomTable bom={output.bom ?? []} />
      {(output.files ?? []).length > 0 && (
        <ul className="flex flex-wrap gap-3 text-xs">
          {(output.files ?? []).map((name) => (
            <li key={name}>
              {/* #2287 — through downloadBlob: a plain link is dropped in Bambuddy's frame (#612). */}
              <BlobDownload url={api.outputFileUrl(output.id, name)} filename={name}>
                {name}
              </BlobDownload>
            </li>
          ))}
        </ul>
      )}
      </Card>
    </li>
  )
}
