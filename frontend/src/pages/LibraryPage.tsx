import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'
import { api, ApiError } from '../api/client'
import type {
  LibraryCopyResult,
  LibraryDeleteResult,
  LibraryEntry,
  LibraryFolderView,
  LibraryListing,
  LibraryMoveResult,
  LibraryRenameResult,
} from '../api/types'
import { ArrangeDialog } from '../components/ArrangeDialog'
import { ArrangedNext } from '../components/ArrangedNext'
import { DeleteLibraryFilesDialog } from '../components/library/DeleteLibraryFilesDialog'
import { LibraryTree } from '../components/library/LibraryTree'
import { RenameLibraryFileDialog } from '../components/library/RenameLibraryFileDialog'
import { TransferLibraryFilesDialog, type TransferMode } from '../components/library/TransferLibraryFilesDialog'
import { PrintPicker } from '../components/PrintPicker'
import { libraryPrintsPath } from '../components/prints/prints'
import { Button } from '../components/ui/Button'
import { Chevron } from '../components/ui/Chevron'
import { Spinner } from '../components/ui/Spinner'
import { arrangedNote, fromFiles, savedNote, type Arranged } from '../lib/arrange'
import { traceAction } from '../lib/traceAction'
import { modelPath } from '../lib/deeplink'
import { formatBytes } from '../lib/format'
import { folderIdOf, folderSegments, libraryPath } from '../lib/libraryPath'
import { folderLabel } from '../lib/libraryTransfer'

/** #313 — remembered per viewer: whether the page lists every file type. */
const ADVANCED_KEY = 'scadbuddy.library.advanced'

function readAdvanced(): boolean {
  try {
    return window.localStorage.getItem(ADVANCED_KEY) === '1'
  } catch {
    return false
  }
}

/** A query parameter as a file id, or null. */
function idParam(value: string | null): number | null {
  const id = Number(value)
  return value !== null && Number.isInteger(id) && id > 0 ? id : null
}

/** What the page asks the listing for: a folder, or the folder a linked file is in. */
type Wanted = { folderId: number | null; fileId?: undefined } | { folderId: null; fileId: number }

/** #2167 — what the last delete did, with Undo while it can still be undone. */
interface Deleted {
  result: LibraryDeleteResult
  restoring: boolean
  restored: number | null
  error: string | null
}

/** #2670 — what the last copy, move or rename did. */
type Done =
  | { kind: 'move'; result: LibraryMoveResult; where: string }
  | { kind: 'copy'; result: LibraryCopyResult; where: string }
  | { kind: 'rename'; result: LibraryRenameResult }

function plural(count: number, word: string): string {
  return `${count} ${word}${count === 1 ? '' : 's'}`
}

/**
 * #313 — Bambuddy's library by folder. Each unsliced 3MF has Print, which opens the same
 * spool-first dialog an output uses; the file is sliced as it stands in Bambuddy.
 * Advanced also lists sliced files and STLs; a sliced file is printed from Bambuddy.
 * #1864 — each file has a tick, kept across folders; Arrange selected opens Arrange on
 * the ticked printable files.
 * #2165 — the folders are a tree, and the URL says where the page is:
 * `/library/<folder path>` the folder, `?file=<id>` a file highlighted (its folder found
 * when the path names none), `?print=<id>` its print dialog open. Back and forward move
 * between them.
 * #2167 — Delete on a card, or Delete selected, moves files to Bambuddy's trash after a
 * confirmation; Undo restores them from it.
 * #2670 — a card's actions menu (Rename, Copy to…, Move to…), or Copy and Move selected,
 * reorganise the library through Bambuddy; a move says which project loses a file.
 */
export function LibraryPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const segments = folderSegments(location.pathname)
  const fileParam = idParam(search.get('file'))
  const printParam = idParam(search.get('print'))

  const [advanced, setAdvanced] = useState(readAdvanced)
  const [tree, setTree] = useState<LibraryFolderView[] | null>(null)
  const [listing, setListing] = useState<LibraryListing | null>(null)
  // A print link to a file the listing on screen already holds (Print pressed on its
  // card) finds nothing new: reading it again would empty the listing, closing and
  // reopening the dialog on a fresh copy of the file, which then reads everything
  // twice (#2361).
  const printListed = printParam !== null && (listing?.files ?? []).some((file) => file.id === printParam)
  const linked = fileParam ?? (printListed ? null : printParam)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const [picked, setPicked] = useState<LibraryEntry[]>([])
  const [arranging, setArranging] = useState(false)
  const [arranged, setArranged] = useState<Arranged | null>(null)
  /** #2287 — the arranged result in the print dialog, and whether a print of it ran. */
  const [printArranged, setPrintArranged] = useState(false)
  const [arrangedSent, setArrangedSent] = useState(false)
  const [confirming, setConfirming] = useState<LibraryEntry[]>([])
  const [deleted, setDeleted] = useState<Deleted | null>(null)
  const [transfer, setTransfer] = useState<{ mode: TransferMode; files: LibraryEntry[] } | null>(null)
  const [renaming, setRenaming] = useState<LibraryEntry | null>(null)
  const [done, setDone] = useState<Done | null>(null)
  const isPicked = (file: LibraryEntry) => picked.some((p) => p.id === file.id)
  const togglePicked = (file: LibraryEntry) =>
    setPicked((now) => (now.some((p) => p.id === file.id) ? now.filter((p) => p.id !== file.id) : [...now, file]))

  // The folder the URL names: by its path, or (a bare `?file=`) the linked file's.
  let wanted: Wanted | undefined
  if (segments.length === 0) wanted = linked !== null ? { folderId: null, fileId: linked } : { folderId: null }
  else if (tree !== null) {
    const id = folderIdOf(tree, segments)
    wanted = id === undefined ? undefined : { folderId: id }
  }
  const missing = segments.length > 0 && tree !== null && wanted === undefined
  const wantedKey = wanted === undefined ? null : wanted.fileId !== undefined ? `file:${wanted.fileId}` : `folder:${wanted.folderId}`
  const loaded = useRef(new Set<string>())

  useEffect(() => {
    let live = true
    // A path needs the tree to resolve; the top level's listing carries it.
    const ask: Wanted | null = wantedKey === null ? (missing ? null : { folderId: null }) : (wanted ?? null)
    if (ask === null) return
    const key = `${wantedKey ?? 'tree'}|${advanced}|${reload}`
    if (loaded.current.has(key)) return
    setError(null)
    if (wantedKey !== null) setListing(null)
    api
      .listLibrary({ folderId: ask.folderId, fileId: ask.fileId, all: advanced })
      .then((next) => {
        if (!live) return
        setTree(next.folders ?? [])
        if (wantedKey === null) return
        loaded.current = new Set([key, `folder:${next.folder_id ?? null}|${advanced}|${reload}`])
        setListing(next)
        if (ask.fileId !== undefined && (next.folder_id ?? null) !== null) {
          // The linked file's folder becomes the path, in place of the bare link.
          navigate(
            { pathname: libraryPath(next.folders ?? [], next.folder_id ?? null), search: location.search },
            { replace: true },
          )
        }
      })
      .catch((cause: unknown) => {
        if (!live) return
        setListing(null)
        setError(cause instanceof ApiError ? cause.detail : 'Could not read the Bambuddy library.')
      })
    return () => {
      live = false
    }
    // `wanted` and `location.search` are read through `wantedKey` and when the answer lands.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantedKey, missing, advanced, reload])

  function toggleAdvanced() {
    const next = !advanced
    setAdvanced(next)
    try {
      window.localStorage.setItem(ADVANCED_KEY, next ? '1' : '0')
    } catch {
      // A browser that refuses storage still toggles for this visit.
    }
  }

  // #2368 — below md the tree folds behind a picker, so the files come first on a phone.
  const [foldersOpen, setFoldersOpen] = useState(false)
  function selectFolder(id: number | null) {
    setFoldersOpen(false)
    const path = libraryPath(tree ?? [], id)
    if (path !== location.pathname || location.search !== '') navigate(path)
  }

  /** Opens or closes a file's print dialog, as a URL Back returns from. */
  function openPrint(file: LibraryEntry | null) {
    // The arranged result's print is not the one opening or closing here.
    setPrintArranged(false)
    const next = new URLSearchParams(search)
    if (file) {
      next.set('print', String(file.id))
      setSearch(next, { state: { printOpened: true } })
    } else if ((location.state as { printOpened?: boolean } | null)?.printOpened) {
      // Opened here: closing is the Back it pushed, so Forward reopens it.
      navigate(-1)
    } else {
      next.delete('print')
      setSearch(next, { replace: true })
    }
  }

  async function deleteFiles(files: LibraryEntry[]) {
    const ids = files.map((file) => file.id)
    // Library actions are user actions in Tempo (#2292), each request its child.
    const result = await traceAction('library_delete', { 'scadbuddy.library_files': ids.length }, () =>
      api.deleteLibraryFiles(ids),
    )
    const gone = new Set(result.deleted.map((file) => file.id))
    setConfirming([])
    setPicked((now) => now.filter((file) => !gone.has(file.id)))
    setDone(null)
    setDeleted({ result, restoring: false, restored: null, error: null })
    if (fileParam !== null && gone.has(fileParam)) {
      const next = new URLSearchParams(search)
      next.delete('file')
      setSearch(next, { replace: true })
    }
    setReload((n) => n + 1)
  }

  async function transferFiles(files: LibraryEntry[], folderId: number | null) {
    if (!transfer) return
    const where = folderLabel(tree ?? [], folderId)
    if (transfer.mode === 'move') {
      const result = await api.moveLibraryFiles(files.map((file) => file.id), folderId)
      const moved = new Map(result.moved.map((file) => [file.id, file]))
      // A ticked file keeps its tick where it went: its folder is what the next move reads.
      setPicked((now) => now.map((file) => (moved.has(file.id) ? { ...file, folder_id: folderId } : file)))
      setDone({ kind: 'move', result, where })
    } else {
      const result = await api.copyLibraryFiles(files.map((file) => file.id), folderId)
      setDone({ kind: 'copy', result, where })
    }
    setTransfer(null)
    setDeleted(null)
    setReload((n) => n + 1)
  }

  async function renameFile(file: LibraryEntry, filename: string) {
    const result = await api.renameLibraryFile(file.id, filename)
    setPicked((now) => now.map((row) => (row.id === file.id ? { ...row, filename: result.filename } : row)))
    setRenaming(null)
    setDeleted(null)
    setDone({ kind: 'rename', result })
    setReload((n) => n + 1)
  }

  async function undo() {
    if (!deleted) return
    const ids = deleted.result.deleted.filter((file) => file.trashed).map((file) => file.id)
    setDeleted({ ...deleted, restoring: true, error: null })
    try {
      const answer = await traceAction('library_restore', { 'scadbuddy.library_files': ids.length }, () =>
        api.restoreLibraryFiles(ids),
      )
      setDeleted({ ...deleted, restoring: false, restored: answer.restored.length })
    } catch (cause) {
      setDeleted({
        ...deleted,
        restoring: false,
        error: cause instanceof ApiError ? cause.detail : 'Could not restore the files.',
      })
    }
    setReload((n) => n + 1)
  }

  const files = listing?.files ?? []
  const hidden = listing?.hidden ?? 0
  const printable = picked.filter((file) => file.printable)
  const printing = printParam === null ? null : (files.find((file) => file.id === printParam) ?? null)
  const folderId = wanted?.fileId !== undefined ? (listing?.folder_id ?? null) : (wanted?.folderId ?? null)

  // A linked file is scrolled to once it is listed.
  const scrolled = useRef<string | null>(null)
  useEffect(() => {
    if (fileParam === null || !listing) return
    const key = `${fileParam}|${listing.folder_id ?? null}`
    if (scrolled.current === key) return
    const card = document.querySelector(`[data-testid="library-file-${fileParam}"]`)
    if (!card) return
    scrolled.current = key
    card.scrollIntoView?.({ block: 'center' })
  }, [fileParam, listing])
  const linkedMissing = fileParam !== null && listing !== null && !files.some((file) => file.id === fileParam)

  return (
    <div className="h-full overflow-y-auto">
      <div className={`mx-auto flex max-w-6xl flex-col gap-6 p-4 md:flex-row md:p-6 ${deleted || done ? 'pb-44' : ''}`}>
        <nav aria-label="Library" className="w-full md:w-60 md:shrink-0">
          {tree ? (
            <>
              <button
                type="button"
                aria-expanded={foldersOpen}
                aria-controls="library-folders"
                onClick={() => setFoldersOpen(!foldersOpen)}
                className="flex min-h-9 w-full items-center gap-2 rounded-[6px] border border-line bg-surface px-3 text-left text-[13px] md:hidden"
              >
                <span className="text-muted">Folder:</span>
                <span className="min-w-0 flex-1 truncate">{folderLabel(tree, folderId)}</span>
                <Chevron open={foldersOpen} />
              </button>
              <div id="library-folders" className={`${foldersOpen ? 'mt-2' : 'hidden'} md:mt-0 md:block`}>
                <LibraryTree folders={tree} selected={folderId} onSelect={selectFolder} />
              </div>
            </>
          ) : (
            <p className="flex items-center gap-2 text-[13px] text-muted">
              <Spinner /> Reading folders
            </p>
          )}
        </nav>

        <section className="min-w-0 flex-1 space-y-4">
          <header className="flex flex-wrap items-center gap-x-3 gap-y-2">
            <h1 className="text-[15px] text-ink">Library</h1>
            <Button
              size="sm"
              disabled={printable.length === 0}
              onClick={() => {
                setArranged(null)
                setArranging(true)
              }}
            >
              Arrange selected ({printable.length})
            </Button>
            <Button
              size="sm"
              data-testid="library-copy-selected"
              disabled={picked.length === 0 || tree === null}
              onClick={() => setTransfer({ mode: 'copy', files: picked })}
            >
              Copy selected ({picked.length})
            </Button>
            <Button
              size="sm"
              data-testid="library-move-selected"
              disabled={picked.length === 0 || tree === null}
              onClick={() => setTransfer({ mode: 'move', files: picked })}
            >
              Move selected ({picked.length})
            </Button>
            <Button
              size="sm"
              variant="danger"
              data-testid="library-delete-selected"
              disabled={picked.length === 0}
              onClick={() => setConfirming(picked)}
            >
              Delete selected ({picked.length})
            </Button>
            {picked.length > 0 && (
              <Button size="sm" variant="ghost" onClick={() => setPicked([])}>
                Clear selection
              </Button>
            )}
            <span className="ml-auto flex items-center gap-2">
              <span id="library-advanced" className="text-[13px] text-ink">
                Advanced
              </span>
              <button
                type="button"
                role="switch"
                aria-checked={advanced}
                aria-labelledby="library-advanced"
                aria-describedby="library-advanced-help"
                data-testid="library-advanced"
                onClick={toggleAdvanced}
                className={`relative h-5 w-9 shrink-0 rounded-full border transition-colors ${
                  advanced ? 'border-accent bg-accent' : 'border-line-strong bg-surface-3'
                }`}
              >
                <span
                  className={`absolute top-[2px] size-3.5 rounded-full transition-[left] ${
                    advanced ? 'left-[18px] bg-accent-ink' : 'left-[2px] bg-muted'
                  }`}
                />
              </button>
            </span>
            <span id="library-advanced-help" className="basis-full text-[12px] text-faint">
              Advanced lists every file, sliced files and STLs too.
            </span>
          </header>

          {missing && (
            <p role="alert" className="text-[13px] text-warn">
              There is no folder {segments.join(' / ')} in the library.{' '}
              <Link to="/library" className="text-accent underline">
                Top level
              </Link>
            </p>
          )}
          {error && (
            <p role="alert" className="text-[13px] text-warn">
              {error}
            </p>
          )}
          {arranged && (
            <p role="status" aria-label="Arranged" className="text-[13px] text-ink">
              {/* #2284 — names where it went: the Library page is not the model's History. */}
              {[arrangedNote(arranged.plates), arranged.skipped, savedNote(arranged)].filter(Boolean).join(' ')}{' '}
              <Link to={modelPath(arranged.output.slug, 'history')} className="text-accent underline">
                Open in History
              </Link>
            </p>
          )}
          {arranged && (
            <ArrangedNext output={arranged.output} sent={arrangedSent} onPrint={() => setPrintArranged(true)} />
          )}
          {!listing && !error && !missing && (
            <p className="flex items-center gap-2 text-[13px] text-muted">
              <Spinner /> Reading the Bambuddy library
            </p>
          )}
          {linkedMissing && (
            <p className="text-[13px] text-muted">
              File #{fileParam} is not listed here{advanced ? '' : '. It may be listed under Advanced'}.
            </p>
          )}
          {listing && files.length === 0 && (
            <p className="text-[13px] text-muted">No files here{hidden > 0 ? '' : ' yet'}.</p>
          )}
          {!advanced && hidden > 0 && (
            <p className="sb-num text-[12px] text-faint">{hidden} more under Advanced.</p>
          )}

          <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(160px,100%),1fr))] gap-3">
            {files.map((file) => {
              const highlighted = file.id === fileParam
              return (
                <li
                  key={file.id}
                  data-testid={`library-file-${file.id}`}
                  aria-current={highlighted ? 'true' : undefined}
                  className={`flex min-w-0 flex-col gap-2 rounded-[8px] border bg-surface-2 p-2 ${
                    highlighted ? 'border-accent ring-2 ring-accent/40' : 'border-line'
                  }`}
                >
                  {file.has_thumbnail ? (
                    <img
                      src={api.libraryThumbnailUrl(file.id)}
                      alt=""
                      loading="lazy"
                      className="aspect-square w-full rounded-[4px] object-contain"
                    />
                  ) : (
                    <div className="aspect-square w-full rounded-[4px] bg-surface-3" aria-hidden />
                  )}
                  <div className="flex min-w-0 items-center gap-1.5">
                    <input
                      type="checkbox"
                      aria-label={`Select ${file.filename}`}
                      checked={isPicked(file)}
                      onChange={() => togglePicked(file)}
                    />
                    <MiddleTruncated name={file.filename} />
                  </div>
                  <FileFacts file={file} />
                  {file.printable ? (
                    <Button
                      variant="primary"
                      data-testid={`library-print-${file.id}`}
                      aria-label={`Print ${file.filename}`}
                      onClick={() => openPrint(file)}
                    >
                      Print
                    </Button>
                  ) : (
                    <p className="text-[12px] text-faint">
                      {file.file_type?.toLowerCase() === 'gcode.3mf'
                        ? 'Sliced already. Print it from Bambuddy.'
                        : 'ScadBuddy cannot print this file type.'}
                    </p>
                  )}
                  <div className="flex items-center justify-between gap-2 text-[12px]">
                    {file.printable ? (
                      // #1755 — the file's own print history, as a template has its Prints tab.
                      <Link
                        to={libraryPrintsPath(file.id)}
                        aria-label={`Prints of ${file.filename}`}
                        className="text-muted hover:text-ink"
                      >
                        Prints
                      </Link>
                    ) : (
                      <span />
                    )}
                    <span className="flex items-center gap-1">
                      <FileActions
                        file={file}
                        disabled={tree === null}
                        onRename={() => setRenaming(file)}
                        onTransfer={(mode) => setTransfer({ mode, files: [file] })}
                      />
                      <button
                        type="button"
                        data-testid={`library-delete-${file.id}`}
                        aria-label={`Delete ${file.filename}`}
                        onClick={() => setConfirming([file])}
                        className="rounded px-1 text-muted hover:text-warn"
                      >
                        Delete
                      </button>
                    </span>
                  </div>
                </li>
              )
            })}
          </ul>
        </section>

        <PrintPicker
          open={printing !== null || (printArranged && arranged !== null)}
          source={
            printing
              ? { kind: 'library', file: printing }
              : printArranged && arranged
                ? { kind: 'output', output: arranged.output }
                : undefined
          }
          onClose={() => (printing ? openPrint(null) : setPrintArranged(false))}
          onRan={() => (printing ? undefined : setArrangedSent(true))}
        />
        <ArrangeDialog
          open={arranging}
          sources={fromFiles(printable)}
          onClose={() => setArranging(false)}
          onArranged={(done) => {
            setArranging(false)
            setPicked([])
            setArranged(done)
            setArrangedSent(false)
          }}
        />
        <DeleteLibraryFilesDialog files={confirming} onClose={() => setConfirming([])} onConfirm={deleteFiles} />
        {deleted && <DeletedToast deleted={deleted} onUndo={() => void undo()} onDismiss={() => setDeleted(null)} />}
        <TransferLibraryFilesDialog
          mode={transfer?.mode ?? 'move'}
          files={transfer?.files ?? []}
          folders={tree ?? []}
          onClose={() => setTransfer(null)}
          onConfirm={transferFiles}
        />
        <RenameLibraryFileDialog file={renaming} onClose={() => setRenaming(null)} onConfirm={renameFile} />
        {done && <DoneToast done={done} onDismiss={() => setDone(null)} />}
      </div>
    </div>
  )
}

/** #2167 — what a delete did, with Undo for the files that went to the trash. */
function DeletedToast({
  deleted,
  onUndo,
  onDismiss,
}: {
  deleted: Deleted
  onUndo: () => void
  onDismiss: () => void
}) {
  const { result, restoring, restored, error } = deleted
  const undoable = result.deleted.filter((file) => file.trashed).length
  const gone = result.deleted.length - undoable
  return (
    <div
      role="status"
      data-testid="library-deleted"
      className="fixed inset-x-4 bottom-4 z-40 mx-auto flex max-w-xl flex-col gap-1.5 rounded-[8px] border border-line-strong bg-surface-2 p-3 text-[13px] text-ink shadow-lg"
    >
      <div className="flex items-start gap-3">
        <p className="min-w-0 flex-1">
          {restored !== null
            ? `Restored ${plural(restored, 'file')}.`
            : result.deleted.length > 0
              ? `Moved ${plural(undoable, 'file')} to Bambuddy's trash.${gone > 0 ? ` Removed ${plural(gone, 'external file')} for good.` : ''}`
              : 'Nothing was deleted.'}
        </p>
        {restored === null && undoable > 0 && (
          <Button size="sm" onClick={onUndo} disabled={restoring} data-testid="library-undo">
            {restoring ? <Spinner /> : 'Undo'}
          </Button>
        )}
        <button type="button" aria-label="Dismiss" onClick={onDismiss} className="px-1 text-muted hover:text-ink">
          ×
        </button>
      </div>
      {restored === null && result.skipped.length > 0 && (
        <div data-testid="library-skipped">
          <p className="text-warn">Not deleted:</p>
          <ul className="list-disc pl-5 text-[12px] break-words text-muted">
            {result.skipped.map((file) => (
              <li key={file.id}>
                {file.filename ?? `#${file.id}`}: {file.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
      {error && (
        <p role="alert" className="text-warn">
          {error}
        </p>
      )}
    </div>
  )
}

/** #2670 — a card's Rename, Copy to… and Move to…, behind one small menu button. */
function FileActions({
  file,
  disabled,
  onRename,
  onTransfer,
}: {
  file: LibraryEntry
  disabled: boolean
  onRename: () => void
  onTransfer: (mode: TransferMode) => void
}) {
  const [open, setOpen] = useState(false)
  const menu = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const away = (event: MouseEvent) => {
      if (!menu.current?.contains(event.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', away)
    return () => document.removeEventListener('mousedown', away)
  }, [open])

  function pick(action: () => void) {
    setOpen(false)
    action()
  }

  const item = 'block w-full rounded px-2 py-1 text-left text-ink hover:bg-surface-3 focus-visible:bg-surface-3 outline-none'
  return (
    <div ref={menu} className="relative">
      <button
        type="button"
        data-testid={`library-actions-${file.id}`}
        aria-label={`More actions for ${file.filename}`}
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={disabled}
        onClick={() => setOpen((now) => !now)}
        className="rounded px-1 text-muted hover:text-ink disabled:opacity-50"
      >
        ⋯
      </button>
      {open && (
        <div
          role="menu"
          aria-label={`Actions for ${file.filename}`}
          onKeyDown={(event) => {
            if (event.key === 'Escape') setOpen(false)
          }}
          className="absolute right-0 bottom-full z-30 mb-1 w-36 rounded-[6px] border border-line-strong bg-surface-2 p-1 text-[13px] shadow-lg"
        >
          <button type="button" role="menuitem" className={item} onClick={() => pick(onRename)}>
            Rename…
          </button>
          <button type="button" role="menuitem" className={item} onClick={() => pick(() => onTransfer('copy'))}>
            Copy to…
          </button>
          <button type="button" role="menuitem" className={item} onClick={() => pick(() => onTransfer('move'))}>
            Move to…
          </button>
        </div>
      )}
    </div>
  )
}

/** #2670 — what a copy, move or rename did, and what Bambuddy skipped. */
function DoneToast({ done, onDismiss }: { done: Done; onDismiss: () => void }) {
  let summary: string
  let skipped: { id: number; filename?: string | null; reason: string }[] = []
  const losses: string[] = []
  if (done.kind === 'rename') {
    summary = `Renamed ${done.result.previous} to ${done.result.filename}.`
  } else if (done.kind === 'move') {
    const { moved } = done.result
    skipped = done.result.skipped
    summary = moved.length > 0 ? `Moved ${plural(moved.length, 'file')} to ${done.where}.` : 'Nothing was moved.'
    const byProject = new Map<number, { name: string; files: string[] }>()
    for (const file of moved) {
      if (!file.left_project) continue
      const entry = byProject.get(file.left_project.id) ?? {
        name: file.left_project.name ?? `project ${file.left_project.id}`,
        files: [],
      }
      entry.files.push(file.filename)
      byProject.set(file.left_project.id, entry)
    }
    for (const { name, files } of byProject.values()) losses.push(`The project ${name} no longer lists ${files.join(', ')}.`)
  } else {
    const { copied } = done.result
    skipped = done.result.skipped
    summary = copied.length > 0 ? `Copied ${plural(copied.length, 'file')} to ${done.where}.` : 'Nothing was copied.'
  }
  return (
    <div
      role="status"
      data-testid="library-done"
      className="fixed inset-x-4 bottom-4 z-40 mx-auto flex max-w-xl flex-col gap-1.5 rounded-[8px] border border-line-strong bg-surface-2 p-3 text-[13px] text-ink shadow-lg"
    >
      <div className="flex items-start gap-3">
        <p className="min-w-0 flex-1">{summary}</p>
        <button type="button" aria-label="Dismiss" onClick={onDismiss} className="px-1 text-muted hover:text-ink">
          ×
        </button>
      </div>
      {losses.map((line) => (
        <p key={line} className="text-warn">
          {line}
        </p>
      ))}
      {skipped.length > 0 && (
        <div data-testid="library-done-skipped">
          <p className="text-warn">{done.kind === 'copy' ? 'Not copied:' : 'Not moved:'}</p>
          <ul className="list-disc pl-5 text-[12px] break-words text-muted">
            {skipped.map((file) => (
              <li key={file.id}>
                {file.filename ?? `#${file.id}`}: {file.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  )
}

/** The last characters of a name's stem that stay when it is cut, with its extension. */
const KEPT_TAIL = 8

/**
 * #935 — a long name cut in the middle, not at the end. Generated names share a long
 * prefix (`bag-clip-3155628dc2bb…`), so cutting the end hides the only part that differs.
 * CSS has no middle ellipsis: the head truncates and the tail never shrinks.
 */
function MiddleTruncated({ name }: { name: string }) {
  // The extension is found from the end: a dot early in a name (`v1.2_shelf….3mf`) is
  // part of the stem, and must not pull the whole name into the tail that never shrinks.
  const sliced = name.toLowerCase().endsWith('.gcode.3mf') ? name.length - '.gcode.3mf'.length : -1
  const last = name.lastIndexOf('.')
  const dot = sliced > 0 ? sliced : last > 0 ? last : name.length
  const split = Math.max(0, dot - KEPT_TAIL)
  return (
    <p className="flex min-w-0 text-[13px] text-ink" title={name}>
      <span className="truncate">{name.slice(0, split)}</span>
      <span className="shrink-0 whitespace-pre">{name.slice(split)}</span>
    </p>
  )
}

/** When it was added and how big it is: what tells two files of one name apart (#935). */
function FileFacts({ file }: { file: LibraryEntry }) {
  const added = file.created_at ? new Date(file.created_at) : null
  const facts = [
    added && !Number.isNaN(added.getTime()) ? (
      <time key="added" dateTime={file.created_at ?? undefined}>
        {added.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })}
      </time>
    ) : null,
    file.file_size != null ? <span key="size">{formatBytes(file.file_size)}</span> : null,
  ].filter((fact) => fact !== null)
  if (facts.length === 0) return null
  return (
    <p className="sb-num -mt-1 flex flex-wrap gap-x-2 text-[11px] text-faint">{facts}</p>
  )
}
