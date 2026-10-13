import { useState, type ReactNode } from 'react'
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
import { SendDialog } from '../components/SendDialog'
import { Button } from '../components/ui/Button'
import { Dialog } from '../components/ui/Dialog'
import { Spinner } from '../components/ui/Spinner'
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
  /**
   * #2288 — each output's newest print as the Prints page has it, so a row says how its
   * print went rather than only that it was queued. Best effort: unread, rows say less.
   */
  const printsState = useAsync(
    () => api.listPrints({ slug }, { limit: 100 }).catch(() => ({ items: [], next_cursor: null })),
    [slug],
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
      outputsState.setData((outputsState.data ?? []).filter((o) => o.id !== id))
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
          <Link to="/" className="text-[12px] text-muted hover:text-ink">
            Models
          </Link>
          <span className="text-faint">/</span>
          <Link to={modelPath(slug)} className="text-[12px] text-muted hover:text-ink">
            {modelState.data?.name ?? slug}
          </Link>
          <span className="text-faint">/</span>
          <h1 className="text-[13px] font-medium">History</h1>
        </div>

        {loading && (
          <p className="flex items-center gap-2 py-16 text-[13px] text-muted">
            <Spinner /> Loading history
          </p>
        )}

        {!loading && outputsState.data?.length === 0 && (
          <div className="rounded-[6px] border border-dashed border-line-strong bg-surface p-10 text-center">
            <h2 className="text-[15px] font-medium">Nothing generated yet</h2>
            <p className="mt-2 text-[13px] text-muted">
              Every output you generate is kept here with the parameters that made it.
            </p>
            <Button variant="primary" className="mt-4" onClick={() => void navigate(modelPath(slug))}>
              Open the customizer
            </Button>
          </div>
        )}

        {!loading && schema && outputsState.data && outputsState.data.length > 0 && (
          <>
            {/* Gone from the list (deleted): nothing to offer a next step for. */}
            {arrangedOutput && arrangedRow && (
              <div className="mb-2">
                <ArrangedNext
                  output={arrangedOutput}
                  sent={wasSent(arrangedRow)}
                  onPrint={() => {
                    setQueueing(false)
                    setPrintFor(arrangedOutput)
                  }}
                />
              </div>
            )}
            <div className="mb-2 flex justify-end">
              <Button
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
              <p role="alert" className="mb-2 text-[13px] text-warn">
                {skipped}
              </p>
            )}
            {saved && (
              <p role="status" aria-label="Arranged" className="mb-2 text-[13px] text-ink">
                {savedNote(saved, slug)}
                {saved.output.slug !== slug && (
                  <>
                    {' '}
                    <Link to={modelPath(saved.output.slug, 'history')} className="text-accent underline">
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
                  onPrint={() => {
                    setQueueing(false)
                    setPrintFor(output)
                  }}
                  onQueue={() => {
                    setQueueing(true)
                    setPrintFor(output)
                  }}
                  onPreview={() => setPreviewFor(output)}
                  onRenamed={(renamed) =>
                    outputsState.setData((outputsState.data ?? []).map((o) => (o.id === renamed.id ? renamed : o)))
                  }
                  lastPrint={lastPrints.get(output.id)}
                  onDelete={() => setConfirmFor(output)}
                  bambuddyUrl={bambuddyUrl}
                  pinned={modelState.data?.libraries}
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
        onClose={() => setPrintFor(undefined)}
        onRan={() => {
          outputsState.reload()
          printsState.refresh()
        }}
      />
      <Dialog
        open={previewFor !== undefined}
        title={previewFor ? (previewFor.name ?? shortId(previewFor.id)) : ''}
        onClose={() => setPreviewFor(undefined)}
      >
        {previewFor && (
          <img
            src={api.outputThumbnailUrl(previewFor.id)}
            alt={`Preview of ${previewFor.name ?? shortId(previewFor.id)}`}
            className="mx-auto max-h-[70vh] w-auto rounded-[4px] bg-surface-2"
          />
        )}
      </Dialog>
      <SendDialog
        open={sendFor !== undefined}
        output={sendFor}
        onClose={() => setSendFor(undefined)}
        onSent={() => outputsState.reload()}
      />
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
  if (status === 'failed' || status === 'cancelled') return 'text-warn'
  return 'text-muted'
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

  return (
    <Dialog
      open={output !== undefined}
      title={`Delete ${output?.name ?? (output ? shortId(output.id) : '')}?`}
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={deleting}>
            Cancel
          </Button>
          <Button variant="danger" onClick={() => void confirm()} disabled={deleting} {...USER_ONLY}>
            {deleting ? <Spinner /> : 'Delete output'}
          </Button>
        </>
      }
    >
      <p className="text-[13px] text-ink">
        The output, its file and its parameters are deleted. This cannot be undone.
      </p>
      {copies.length > 0 && (
        <>
          <p className="mt-3 text-[13px] text-muted">This output has copies in Bambuddy&apos;s library:</p>
          <ul className="mt-1 list-disc pl-5 text-[13px] text-muted" aria-label="Library copies">
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
            <label className="mt-3 flex items-center gap-2 text-[13px] text-ink">
              <input
                type="checkbox"
                checked={alsoInbox}
                onChange={(event) => setAlsoInbox(event.target.checked)}
              />
              Also delete the inbox copies in Bambuddy
            </label>
          ) : (
            <p className="mt-3 text-[13px] text-muted">Copies in project folders stay in Bambuddy.</p>
          )}
          <p className="mt-2 text-[12px] text-faint">
            Sliced files and project copies are never deleted from here.
          </p>
        </>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}

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
      className={`${className} underline decoration-dotted underline-offset-2`}
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
    <p className="sb-num mt-1 text-[12px] text-muted" aria-label="Rendered with libraries">
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
                <span className="text-warn">({change})</span>
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
    <li className="rounded-[6px] border border-line bg-surface p-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        {/* #2288 — every output's picture; an arranged one also shows its plates below. */}
        {output.has_thumbnail ? (
          <button
            type="button"
            onClick={onPreview}
            aria-label={`Preview ${label}`}
            className="shrink-0 rounded-[4px] border border-line bg-surface-2"
          >
            <img src={api.outputThumbnailUrl(output.id)} alt="" className="h-16 w-16 rounded-[4px] object-contain" />
          </button>
        ) : (
          <div
            aria-label={`No preview of ${label}`}
            className="flex h-16 w-16 shrink-0 items-center justify-center rounded-[4px] border border-dashed border-line text-center text-[10px] text-faint"
          >
            No preview
          </div>
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2.5">
            <input
              type="checkbox"
              aria-label={`Select ${output.name ?? shortId(output.id)}`}
              checked={picked}
              onChange={onPick}
              className="accent-[var(--sb-accent)]"
            />
            <ColorStrip colors={output.colors ?? []} size="sm" />
            {naming === null ? (
              <span className="text-[13px] text-ink">{label}</span>
            ) : (
              <form
                className="flex items-center gap-1.5"
                onSubmit={(event) => {
                  event.preventDefault()
                  void rename(naming)
                }}
              >
                <input
                  aria-label={`Name for ${label}`}
                  value={naming}
                  placeholder={shortId(output.id)}
                  onChange={(event) => setNaming(event.target.value)}
                  maxLength={200}
                  autoFocus
                  className="h-7 rounded-[4px] border border-line bg-surface px-2 text-[13px]"
                />
                <Button size="sm" type="submit" variant="primary" disabled={renaming}>
                  {renaming ? <Spinner /> : 'Save'}
                </Button>
                <Button size="sm" variant="ghost" disabled={renaming} onClick={() => setNaming(null)}>
                  Cancel
                </Button>
              </form>
            )}
            <span className="text-[12px] text-faint">{timeAgo(output.created_at)}</span>
          </div>
          {renameError && (
            <p role="alert" className="mt-1 text-[12px] text-warn">
              {renameError}
            </p>
          )}
          <p className="sb-num mt-1 text-[12px] text-muted">
            {output.bbox_mm ? formatBbox(output.bbox_mm, unit) : 'No dimensions recorded'}
            {/* #2288 — how its last print went, linked to ScadBuddy's print page. Being
                queued says nothing of that, so the queue ids are neutral and show only
                until a print is on record. */}
            {lastPrint && (
              <Link
                to={`/prints/${lastPrint.archive_id}`}
                className={`ml-2 underline ${printTone(lastPrint.status)}`}
              >
                {`Last print: ${statusLabel(lastPrint.status)}`}
              </Link>
            )}
            {!lastPrint &&
              (output.plates ?? []).length > 1 &&
              (output.plates ?? []).map((plate) => (
                <BambuddyId
                  key={plate.queue_item_id}
                  className="ml-2 text-muted"
                  href={bambuddyUrl && `${bambuddyUrl}/queue/${plate.queue_item_id}`}
                >
                  {plateLabel(plateNames, plate.plate_id)} queued #{plate.queue_item_id}
                </BambuddyId>
              ))}
            {!lastPrint && output.queue_item_id && (output.plates ?? []).length <= 1 && (
              <BambuddyId
                className="ml-2 text-muted"
                href={bambuddyUrl && `${bambuddyUrl}/queue/${output.queue_item_id}`}
              >
                queued #{output.queue_item_id}
              </BambuddyId>
            )}
            {!output.queue_item_id && lastCopy(output) && (
              <BambuddyId className="ml-2 text-muted" href={bambuddyUrl && `${bambuddyUrl}/library`}>
                in library #{lastCopy(output)?.id}
              </BambuddyId>
            )}
          </p>
          <OutputLibraries libraries={output.libraries ?? []} pinned={pinned} />
          {/* #2282 — an arranged output's layout is what it is: its plates, on the row. */}
          {arrangedFrom > 0 && !platesAbove && <LazyArrangedPlates outputId={output.id} size="sm" />}
        </div>

        <div className="flex shrink-0 flex-wrap items-center gap-2">
          {arrangedFrom > 0 ? (
            <span className="text-[12px] text-muted">
              {`Arranged from ${arrangedSources(output.arranged_from ?? [])}`}
            </span>
          ) : (
            <Button size="sm" onClick={onEdit} aria-label={`Edit ${label}`}>
              Edit
            </Button>
          )}
          <Button size="sm" onClick={onPrint} aria-label={`Print ${label}`}>
            Print
          </Button>
          <Button size="sm" onClick={onQueue} aria-label={`Queue ${label}`}>
            Queue
          </Button>
          {/* #2287 — "again" only for an output that went to Bambuddy before. */}
          <Button size="sm" onClick={onSend} aria-label={`${sent ? 'Send again' : 'Send'} ${label}`}>
            {sent ? 'Send again' : 'Send'}
          </Button>
          <BlobDownload
            url={api.downloadUrl(output.id)}
            filename={`${output.slug}-${output.name ?? output.id}.3mf`}
            className="text-[13px] text-accent underline"
          >
            Download 3MF
          </BlobDownload>
          <Button
            size="sm"
            variant="ghost"
            disabled={naming !== null}
            onClick={() => {
              setRenameError(null)
              setNaming(output.name ?? '')
            }}
            aria-label={`Rename ${label}`}
          >
            Rename
          </Button>
          <Button
            size="sm"
            variant="danger"
            onClick={onDelete}
            disabled={deleting}
            aria-label={`Delete ${label}`}
            {...USER_ONLY}
          >
            {deleting ? <Spinner /> : 'Delete'}
          </Button>
        </div>
      </div>

      <div className="mt-3 border-t border-line pt-2.5">
        {diff.length === 0 ? (
          <p className="text-[12px] text-faint">Model defaults, unchanged.</p>
        ) : (
          // #1036 — the caption column is capped at half the card: as `auto`, a long caption
          // took the whole row on a phone and pushed the values off the screen.
          <dl className="grid grid-cols-[fit-content(50%)_minmax(0,1fr)] gap-x-4 gap-y-1 text-[12px]">
            {diff.map((entry) => (
              <div key={entry.name} className="contents">
                <dt className="text-muted">{entry.caption}</dt>
                <dd className="sb-num min-w-0">
                  <span className="text-ink">{formatValue(entry.value)}</span>
                  {/* #975 — read as "21, default 20", not "2120": the strike-through alone is CSS. */}
                  <span className="sr-only">, default </span>
                  <del className="ml-2 text-faint">{formatValue(entry.initial)}</del>
                </dd>
              </div>
            ))}
          </dl>
        )}
      </div>

      {/* A pipeline output's bill of materials and extra files (spec 2026-09-27 §5.2). */}
      <BomTable bom={output.bom ?? []} />
      {(output.files ?? []).length > 0 && (
        <ul className="mt-2 flex flex-wrap gap-3 text-[12px]">
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
    </li>
  )
}
