import { useEffect, useEffectEvent, useId, useMemo, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router'
import {
  ChevronDownIcon,
  CopyIcon,
  FolderIcon,
  FolderInputIcon,
  FolderOpenIcon,
  FolderPlusIcon,
  FolderTreeIcon,
  FolderUpIcon,
  KanbanSquareIcon,
  LayoutGridIcon,
  PencilIcon,
  PrinterIcon,
  SearchIcon,
  Trash2Icon,
  XIcon,
} from 'lucide-react'
import { api, ApiError } from '../api/client'
import type {
  LibraryCopyResult,
  LibraryDeleteResult,
  LibraryEntry,
  LibraryFolderView,
  LibraryListing,
  LibraryMoveResult,
  LibraryRenameResult,
  LibrarySearch,
  PrintBatch,
} from '../api/types'
import { ArrangeDialog } from '../components/ArrangeDialog'
import { ArrangedNext } from '../components/ArrangedNext'
import { DeleteLibraryFilesDialog } from '../components/library/DeleteLibraryFilesDialog'
import {
  DeleteFolderDialog,
  FolderNameDialog,
  MoveFolderDialog,
  type FolderAction,
} from '../components/library/LibraryFolderDialogs'
import { MoreMenu, RowContextMenu, type MenuEntry } from '../components/library/LibraryMenus'
import { LibraryTree } from '../components/library/LibraryTree'
import { BatchPrintDialog } from '../components/BatchPrintDialog'
import { RenameLibraryFileDialog } from '../components/library/RenameLibraryFileDialog'
import { TransferLibraryFilesDialog, type TransferMode } from '../components/library/TransferLibraryFilesDialog'
import { PrintPicker } from '../components/PrintPicker'
import { libraryPrintsPath } from '../components/prints/prints'
import { Spinner } from '../components/ui/spinner'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from '@/components/ui/collapsible'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { cn } from '@/lib/utils'
import { arrangedNote, fromFiles, savedNote, type Arranged } from '../lib/arrange'
import { traceAction } from '../lib/traceAction'
import { modelPath } from '../lib/deeplink'
import { useAdvanced } from '../lib/advanced'
import { formatBytes } from '../lib/format'
import { LIBRARY_ROOT, folderIdOf, folderSegments, libraryPath, libraryPathOf } from '../lib/libraryPath'
import { nameParts, shortName } from '../lib/libraryNames'
import { folderAndDescendants, folderLabel } from '../lib/libraryTransfer'
import { NARROW_QUERY, useMediaQuery } from '../lib/useMediaQuery'

/** #313 — remembered per viewer: whether the page lists every file type. */
const ADVANCED_KEY = 'scadbuddy.library.advanced'

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
  /** #3650 — the folder deleted with them, by name. */
  folder?: string
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
 * Advanced also lists sliced files and STLs. A sliced file's Print queues it as it is,
 * with no slice (#2671).
 * #1864 — each file has a tick, kept across folders; Arrange selected opens Arrange on
 * the ticked printable files that are not sliced already.
 * #2165 — the folders are a tree, and the URL says where the page is:
 * `/library/<folder path>` the folder, `?file=<id>` a file highlighted (its folder found
 * when the path names none), `?print=<id>` its print dialog open. Back and forward move
 * between them.
 * #2167 — Delete on a card, or Delete selected, moves files to Bambuddy's trash after a
 * confirmation; Undo restores them from it.
 * #2670 — a card's actions menu (Rename, Copy to…, Move to…), or Copy and Move selected,
 * reorganise the library through Bambuddy; a move says which project loses a file.
 * #3650 — a folder has the same kind of menu, on right-click (a long press on touch) and
 * on its ⋯ button: open, its project, arrange its files, new folder, rename, move,
 * delete. The library is browsed by folder or by project (`?by=project`,
 * `?project=<id>`), each view linking to the other.
 */
export function LibraryPage() {
  const location = useLocation()
  const navigate = useNavigate()
  const [search, setSearch] = useSearchParams()
  const segments = folderSegments(location.pathname)
  const fileParam = idParam(search.get('file'))
  const printParam = idParam(search.get('print'))
  /** #2672 — the batch print open, by id: a reload resumes it. */
  const batchParam = search.get('batch')
  // #3630 — the search, kept in the URL so Back returns to its results.
  const query = search.get('q') ?? ''
  const searching = query.trim() !== ''
  // #3650 — browsing by project: `?by=project` lists them, `?project=<id>` opens one.
  const projectParam = idParam(search.get('project'))
  const byProject = projectParam !== null || search.get('by') === 'project'

  const [advanced, toggleAdvanced] = useAdvanced(ADVANCED_KEY)
  const [tree, setTree] = useState<LibraryFolderView[] | null>(null)
  // #2368 — below md the tree is a collapsed disclosure, so the files come first.
  const narrow = useMediaQuery(NARROW_QUERY)
  const [foldersOpen, setFoldersOpen] = useState(false)
  // Named on the button folded too, which Radix leaves out: what it controls is there.
  const foldersId = useId()
  // Where focus goes back once a folder is chosen and the tree folds away (#3315).
  const foldersToggle = useRef<HTMLButtonElement>(null)
  const [listing, setListing] = useState<LibraryListing | null>(null)
  // #3633 — each folder's last listing, shown at once when it is opened again while it
  // is read afresh. Emptied by a reload, which follows a change made here.
  const listed = useRef(new Map<string, LibraryListing>())
  const listedReload = useRef(0)
  const [results, setResults] = useState<LibrarySearch | null>(null)
  const [searchError, setSearchError] = useState<string | null>(null)
  const [slow, setSlow] = useState(false)
  // A print link to a file the listing on screen already holds (Print pressed on its
  // card) finds nothing new: reading it again would empty the listing, closing and
  // reopening the dialog on a fresh copy of the file, which then reads everything
  // twice (#2361). While a listing is read again (Advanced, a reload) there is none on
  // screen, so the last one's answer holds; else the file alone is read meanwhile (#2822).
  const [listedPrint, setListedPrint] = useState<number | null>(null)
  // While searching, the files on screen are the results, which a print link may name.
  const onScreen = searching ? results : listing
  const seenPrint =
    onScreen === null
      ? listedPrint
      : printParam !== null && (onScreen.files ?? []).some((file) => file.id === printParam)
        ? printParam
        : null
  if (seenPrint !== listedPrint) setListedPrint(seenPrint)
  // `listedPrint` outlives the link: it stays set once the dialog closes, and across a
  // folder change until that listing arrives. Comparing with `printParam` here is what
  // makes the held value safe; it never stands in for a link that is not there (#3458).
  const printListed = printParam !== null && seenPrint === printParam
  const linked = fileParam ?? (printListed ? null : printParam)
  const [error, setError] = useState<string | null>(null)
  const [reload, setReload] = useState(0)
  const [picked, setPicked] = useState<LibraryEntry[]>([])
  /** What Arrange opens on: the ticked files, or a folder's (#3650). */
  const [arranging, setArranging] = useState<LibraryEntry[] | null>(null)
  const [arranged, setArranged] = useState<Arranged | null>(null)
  /** #2287 — the arranged result in the print dialog, and whether a print of it ran. */
  const [printArranged, setPrintArranged] = useState(false)
  const [arrangedSent, setArrangedSent] = useState(false)
  const [confirming, setConfirming] = useState<LibraryEntry[]>([])
  const [deleted, setDeleted] = useState<Deleted | null>(null)
  const [transfer, setTransfer] = useState<{ mode: TransferMode; files: LibraryEntry[] } | null>(null)
  const [renaming, setRenaming] = useState<LibraryEntry | null>(null)
  const [done, setDone] = useState<Done | null>(null)
  const [folderAction, setFolderAction] = useState<FolderAction | null>(null)
  const [folderError, setFolderError] = useState<string | null>(null)
  const isPicked = (file: LibraryEntry) => picked.some((p) => p.id === file.id)
  const togglePicked = (file: LibraryEntry) =>
    setPicked((now) => (now.some((p) => p.id === file.id) ? now.filter((p) => p.id !== file.id) : [...now, file]))

  // #3650 — the projects the library's folders are linked to, each with its folders.
  const projects = useMemo(() => projectsOf(tree ?? []), [tree])
  const project = projectParam === null ? undefined : projects.find((row) => row.id === projectParam)

  // The folder the URL names: by its path, a project's folder, or (a bare `?file=`) the
  // linked file's.
  let wanted: Wanted | undefined
  if (byProject) {
    // The grid shows the project's first folder; the header says so when there are more.
    const first = project?.folders[0]
    if (first) wanted = { folderId: first.id }
  } else if (segments.length === 0) wanted = linked !== null ? { folderId: null, fileId: linked } : { folderId: null }
  else if (tree !== null) {
    const id = folderIdOf(tree, segments)
    wanted = id === undefined ? undefined : { folderId: id }
  }
  const missing = !byProject && segments.length > 0 && tree !== null && wanted === undefined
  const missingProject = projectParam !== null && tree !== null && project === undefined
  const wantedKey = wanted === undefined ? null : wanted.fileId !== undefined ? `file:${wanted.fileId}` : `folder:${wanted.folderId}`
  const loaded = useRef(new Set<string>())

  useEffect(() => {
    let live = true
    // A path needs the tree to resolve; the top level's listing carries it.
    const ask: Wanted | null = wantedKey === null ? (missing ? null : { folderId: null }) : (wanted ?? null)
    if (ask === null) return
    const key = `${wantedKey ?? 'tree'}|${advanced}|${reload}`
    if (loaded.current.has(key)) {
      // Already read, but another folder's listing may be on screen: Back, then Forward
      // before Back's answer landed, comes back here with the folder left behind shown.
      const kept = wantedKey === null ? undefined : listed.current.get(`${wantedKey}|${advanced}`)
      if (kept) setListing(kept)
      return
    }
    if (listedReload.current !== reload) {
      listedReload.current = reload
      listed.current.clear()
    }
    setError(null)
    // Only folders are kept: a bare `?file=` link (`file:<id>`) never matches, on purpose.
    // Its folder is what the answer says, and the link then moves to that folder's path,
    // whose key is kept from then on.
    if (wantedKey !== null) setListing(listed.current.get(`${wantedKey}|${advanced}`) ?? null)
    setSlow(false)
    const slowTimer = window.setTimeout(() => setSlow(true), SLOW_MS)
    api
      .listLibrary({ folderId: ask.folderId, fileId: ask.fileId, all: advanced })
      .then((next) => {
        if (!live) return
        setTree(next.folders ?? [])
        if (wantedKey === null) return
        loaded.current = new Set([key, `folder:${next.folder_id ?? null}|${advanced}|${reload}`])
        listed.current.set(`folder:${next.folder_id ?? null}|${advanced}`, next)
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
        // A folder's last listing, if one is on screen, stays: what is known (#3633).
        if (wantedKey === null) setListing(null)
        setError(cause instanceof ApiError ? cause.detail : 'Could not read the Bambuddy library.')
      })
      .finally(() => window.clearTimeout(slowTimer))
    return () => {
      live = false
      window.clearTimeout(slowTimer)
    }
    // `wanted` and `location.search` are read through `wantedKey` and when the answer lands.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [wantedKey, missing, advanced, reload])

  // #3630 — the search's results; the last ones stay on screen while the next are read.
  // A search after a change made here reads the library afresh, not the copy kept: every
  // search asks for it until one such answer has arrived, so typing on does not lose it.
  const searchedReload = useRef(0)
  useEffect(() => {
    if (!searching) {
      setResults(null)
      setSearchError(null)
      return
    }
    const controller = new AbortController()
    const fresh = searchedReload.current !== reload
    setSearchError(null)
    api
      .searchLibrary({ q: query, all: advanced, fresh }, controller.signal)
      .then((found) => {
        if (controller.signal.aborted) return
        if (fresh) searchedReload.current = reload
        setResults(found)
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return
        // The last results stay under the error: what is known (#3633).
        setSearchError(cause instanceof ApiError ? cause.detail : 'Could not search the Bambuddy library.')
      })
    return () => controller.abort()
  }, [searching, query, advanced, reload])

  function selectFolder(id: number | null) {
    const path = libraryPath(tree ?? [], id)
    if (path !== location.pathname || location.search !== '') navigate(path)
  }

  /** #3650 — a project's page in the project view; `null` lists every project. */
  function selectProject(id: number | null) {
    navigate(id === null ? `${LIBRARY_ROOT}?by=project` : `${LIBRARY_ROOT}?project=${id}`)
  }

  /** #3650 — the toggle between the folder tree and the project list. */
  function browse(by: 'folder' | 'project') {
    if (by === (byProject ? 'project' : 'folder')) return
    if (by === 'folder') {
      // A project's own folder is where its files are seen by folder.
      const first = project?.folders[0]
      navigate(first ? libraryPath(tree ?? [], first.id) : LIBRARY_ROOT)
      return
    }
    const here = tree?.find((folder) => folder.id === folderId)
    selectProject(here?.project_id ?? null)
  }

  /** The search as typed, into the URL: a first search is a step Back undoes, the rest replace it. */
  function setQuery(text: string) {
    setSearch(
      (now) => {
        const next = new URLSearchParams(now)
        if (text.trim() === '') next.delete('q')
        else next.set('q', text)
        next.delete('file')
        next.delete('print')
        return next
      },
      { replace: searching },
    )
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

  /** #2672 — a batch print of the ticked printable files, in the order they were ticked. */
  const [batchError, setBatchError] = useState<string | null>(null)
  const [startingBatch, setStartingBatch] = useState(false)
  async function startBatch(files: LibraryEntry[]) {
    setStartingBatch(true)
    setBatchError(null)
    try {
      const batch = await api.createPrintBatch(
        files.map((file) => ({ library_file_id: file.id, filename: file.filename, sliced: file.sliced ?? false })),
      )
      setPicked([])
      const next = new URLSearchParams(search)
      next.set('batch', batch.id)
      setSearch(next)
    } catch (cause) {
      setBatchError(cause instanceof ApiError ? cause.detail : 'Could not start the batch print.')
    } finally {
      setStartingBatch(false)
    }
  }

  function closeBatch() {
    const next = new URLSearchParams(search)
    next.delete('batch')
    setSearch(next, { replace: true })
  }

  // #2718 review — a batch closed while a file was still being checked is kept, so its
  // Queue all follows that run rather than a new batch queueing the file twice. Read
  // whenever no batch is open, so a batch just closed shows here.
  const [kept, setKept] = useState<PrintBatch[]>([])
  useEffect(() => {
    if (batchParam !== null) return
    let live = true
    api
      .listPendingPrintBatches()
      .then((list) => live && setKept(list))
      .catch(() => undefined)
    return () => {
      live = false
    }
  }, [batchParam])
  function resumeBatch(batchId: string) {
    const next = new URLSearchParams(search)
    next.set('batch', batchId)
    setSearch(next)
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

  /**
   * #3650 — a folder renamed or moved. Its id stays, but its path changes: the page on
   * it, or on a folder inside it, follows it to the new one.
   */
  async function updateFolder(folder: LibraryFolderView, update: { name?: string; parentId?: number | null }) {
    const answer = await traceAction('library_folder_update', {}, () =>
      api.updateLibraryFolder(folder.id, {
        ...(update.name !== undefined && { name: update.name }),
        ...(update.parentId !== undefined && { parent_id: update.parentId ?? 0 }),
      }),
    )
    const next = (tree ?? []).map((row) =>
      row.id === folder.id ? { ...row, name: answer.name, parent_id: answer.parent_id ?? null } : row,
    )
    setFolderAction(null)
    setTree(next)
    if (!byProject && folderId !== null && folderAndDescendants(tree ?? [], folder.id).has(folderId)) {
      navigate({ pathname: libraryPath(next, folderId), search: location.search }, { replace: true })
    }
    setReload((n) => n + 1)
  }

  async function createFolder(parent: LibraryFolderView | null, name: string) {
    const made = await traceAction('library_folder_create', {}, () =>
      api.createLibraryFolder({ name, ...(parent && { parent_id: parent.id }) }),
    )
    setFolderAction(null)
    const depth = parent ? (parent.depth ?? 0) + 1 : 0
    const next = [...(tree ?? []), { id: made.id, name: made.name, parent_id: made.parent_id ?? null, depth, file_count: 0 }]
    setTree(next)
    navigate(libraryPath(next, made.id))
    setReload((n) => n + 1)
  }

  async function deleteFolder(folder: LibraryFolderView) {
    const inside = folderAndDescendants(tree ?? [], folder.id)
    const result = await traceAction('library_folder_delete', {}, () => api.deleteLibraryFolder(folder.id))
    const gone = new Set(result.deleted.map((file) => file.id))
    setFolderAction(null)
    setPicked((now) => now.filter((file) => !gone.has(file.id)))
    setDone(null)
    setDeleted({ result, folder: folder.name, restoring: false, restored: null, error: null })
    if (!byProject && folderId !== null && inside.has(folderId) && result.folders.includes(folderId)) {
      navigate(libraryPath(tree ?? [], folder.parent_id ?? null), { replace: true })
    }
    setReload((n) => n + 1)
  }

  /** #3650 — Arrange on a folder's printable files, read afresh. */
  async function arrangeFolder(folder: LibraryFolderView) {
    setFolderError(null)
    try {
      const read = await api.listLibrary({ folderId: folder.id, all: true })
      const files = (read.files ?? []).filter((file) => file.printable && !file.sliced)
      if (files.length === 0) {
        setFolderError(`${folder.name} has no unsliced 3MF or STL files to arrange.`)
        return
      }
      setArranged(null)
      setArranging(files)
    } catch (cause) {
      setFolderError(cause instanceof ApiError ? cause.detail : `Could not read ${folder.name}.`)
    }
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
  // #2671 — a sliced file prints, but has no objects to arrange.
  // #2286 — what Arrange takes. The note below names it ("unsliced 3MF and STL files"):
  // a change to this filter, or to what the server calls `printable`, changes that too.
  const printable = picked.filter((file) => file.printable && !file.sliced)
  // #2672 — any printable file, sliced or not, prints in a batch.
  const batchable = picked.filter((file) => file.printable)
  const printing =
    printParam === null ? null : ((onScreen?.files ?? []).find((file) => file.id === printParam) ?? null)
  const folderId = wanted?.fileId !== undefined ? (listing?.folder_id ?? null) : (wanted?.folderId ?? null)
  const folder = folderId === null ? undefined : tree?.find((row) => row.id === folderId)
  // #3631 — the folder's own subfolders, shown beside its files: a folder of folders is
  // not empty. The top level's are the tree itself, and listed here only when it holds no
  // files, so its files stay first on a phone.
  const subfolders =
    tree === null ||
    listing === null ||
    (byProject && !project) ||
    (listing.folder_id ?? null) !== folderId ||
    (folderId === null && (listing.files ?? []).length > 0)
      ? []
      : tree.filter((row) => (row.parent_id ?? null) === folderId)
  const showFiles = !byProject || project !== undefined

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

  /** #2724 — every listed printable file of this folder, ticked at once: only while that
   * folder's listing is what is on screen, never under a search's results or while
   * another folder loads (#2757 review). */
  const listingShown = !searching && showFiles && listing !== null && (listing.folder_id ?? null) === folderId
  const listedPrintable = listingShown ? files.filter((file) => file.printable) : []
  const unpicked = listedPrintable.filter((file) => !isPicked(file))
  const linkedMissing = fileParam !== null && listing !== null && !files.some((file) => file.id === fileParam)

  /** #3650 — what a folder's menus offer: on right-click, and on its ⋯ button. */
  const folderEntries = (target: LibraryFolderView, opening = true): MenuEntry[] => [
    ...(opening ? [{ label: 'Open', icon: FolderOpenIcon, onSelect: () => selectFolder(target.id) }] : []),
    ...(target.project_id != null
      ? [
          {
            label: `Open the project ${target.project_name ?? `#${target.project_id}`}`,
            icon: KanbanSquareIcon,
            onSelect: () => selectProject(target.project_id ?? null),
            testId: `library-folder-project-${target.id}`,
          },
        ]
      : []),
    {
      label: 'Arrange its files…',
      icon: LayoutGridIcon,
      onSelect: () => void arrangeFolder(target),
      separator: true,
      testId: `library-folder-arrange-${target.id}`,
    },
    {
      label: 'New folder inside…',
      icon: FolderPlusIcon,
      onSelect: () => setFolderAction({ kind: 'create', folder: target }),
      separator: true,
    },
    {
      label: 'Rename…',
      icon: PencilIcon,
      onSelect: () => setFolderAction({ kind: 'rename', folder: target }),
      testId: `library-folder-rename-${target.id}`,
    },
    {
      label: 'Move to…',
      icon: FolderInputIcon,
      onSelect: () => setFolderAction({ kind: 'move', folder: target }),
      testId: `library-folder-move-${target.id}`,
    },
    {
      label: 'Delete…',
      icon: Trash2Icon,
      destructive: true,
      separator: true,
      onSelect: () => setFolderAction({ kind: 'delete', folder: target }),
      testId: `library-folder-delete-${target.id}`,
    },
  ]
  const folderMenu = (target: LibraryFolderView, row: ReactNode) => (
    <RowContextMenu label={target.name} entries={folderEntries(target)}>
      {row}
    </RowContextMenu>
  )

  /** What every card does: the same on a folder's files and on search results. */
  const cardActions = (file: LibraryEntry) => ({
    highlighted: file.id === fileParam,
    picked: isPicked(file),
    actionsDisabled: tree === null,
    onPick: () => togglePicked(file),
    onPrint: () => openPrint(file),
    onRename: () => setRenaming(file),
    onTransfer: (mode: TransferMode) => setTransfer({ mode, files: [file] }),
    onDelete: () => setConfirming([file]),
  })

  const sidebar = byProject ? (
    <LibraryTree
      folders={projects.map((row) => ({ id: row.id, name: row.name, parent_id: null, depth: 0, file_count: row.files }))}
      selected={projectParam}
      onSelect={selectProject}
      label="Library projects"
      testIdPrefix="library-project"
      rootLabel="All projects"
    />
  ) : (
    <LibraryTree folders={tree ?? []} selected={folderId} onSelect={selectFolder} wrapRow={folderMenu} />
  )

  return (
    <div className="h-full overflow-y-auto">
      <div className={cn('mx-auto flex max-w-6xl flex-col gap-6 p-4 md:flex-row md:p-6', (deleted || done) && 'pb-44')}>
        <nav aria-label="Library" className="w-full space-y-3 md:w-60 md:shrink-0">
          <ToggleGroup
            type="single"
            variant="outline"
            size="sm"
            spacing={0}
            value={byProject ? 'project' : 'folder'}
            onValueChange={(value) => value && browse(value as 'folder' | 'project')}
            aria-label="Browse the library by"
            className="w-full"
          >
            <ToggleGroupItem value="folder" data-testid="library-by-folder" className="flex-1">
              <FolderTreeIcon /> Folders
            </ToggleGroupItem>
            <ToggleGroupItem value="project" data-testid="library-by-project" className="flex-1">
              <KanbanSquareIcon /> Projects
            </ToggleGroupItem>
          </ToggleGroup>
          {tree && narrow ? (
            <Collapsible open={foldersOpen} onOpenChange={setFoldersOpen}>
              <CollapsibleTrigger asChild>
                <Button ref={foldersToggle} variant="outline" className="w-full justify-between" aria-controls={foldersId}>
                  <span className="min-w-0 truncate">
                    {byProject
                      ? `Project: ${project?.name ?? 'All projects'}`
                      : `Folder: ${folderLabel(tree, folderId)}`}
                  </span>
                  <ChevronDownIcon className={cn('transition-transform', foldersOpen && 'rotate-180')} />
                </Button>
              </CollapsibleTrigger>
              {/* Always there, so `aria-controls` names an element when folded too. */}
              <CollapsibleContent id={foldersId} className="mt-2">
                {byProject ? (
                    <LibraryTree
                      folders={projects.map((row) => ({ id: row.id, name: row.name, parent_id: null, depth: 0, file_count: row.files }))}
                      selected={projectParam}
                      onSelect={(id) => {
                        setFoldersOpen(false)
                        selectProject(id)
                        foldersToggle.current?.focus()
                      }}
                      label="Library projects"
                      testIdPrefix="library-project"
                      rootLabel="All projects"
                    />
                  ) : (
                    <LibraryTree
                      folders={tree}
                      selected={folderId}
                      wrapRow={folderMenu}
                      onSelect={(id) => {
                        setFoldersOpen(false)
                        selectFolder(id)
                        foldersToggle.current?.focus()
                      }}
                    />
                  )}
              </CollapsibleContent>
            </Collapsible>
          ) : tree ? (
            sidebar
          ) : (
            <div role="status" aria-label="Reading folders" className="space-y-1.5">
              {Array.from({ length: 6 }, (_, n) => (
                <Skeleton key={n} className="h-7" style={{ width: `${88 - ((n * 13) % 40)}%` }} />
              ))}
            </div>
          )}
        </nav>

        <section className="min-w-0 flex-1 space-y-4">
          <SearchBox query={query} onSearch={setQuery} />
          <header className="flex flex-wrap items-center gap-x-2 gap-y-2">
            <h1 className="mr-1 text-lg font-semibold tracking-tight">Library</h1>
            <Button
              size="sm"
              variant="outline"
              disabled={printable.length === 0}
              onClick={() => {
                setArranged(null)
                setArranging(printable)
              }}
            >
              <LayoutGridIcon /> Arrange selected ({printable.length})
            </Button>
            <Button
              size="sm"
              variant="outline"
              data-testid="library-copy-selected"
              disabled={picked.length === 0 || tree === null}
              onClick={() => setTransfer({ mode: 'copy', files: picked })}
            >
              <CopyIcon /> Copy selected ({picked.length})
            </Button>
            <Button
              size="sm"
              variant="outline"
              data-testid="library-move-selected"
              disabled={picked.length === 0 || tree === null}
              onClick={() => setTransfer({ mode: 'move', files: picked })}
            >
              <FolderInputIcon /> Move selected ({picked.length})
            </Button>
            <Button
              size="sm"
              data-testid="library-print-selected"
              disabled={batchable.length < 2 || startingBatch}
              onClick={() => void startBatch(batchable)}
            >
              <PrinterIcon /> Print {batchable.length} files
            </Button>
            <Button
              size="sm"
              variant="destructive"
              data-testid="library-delete-selected"
              disabled={picked.length === 0}
              onClick={() => setConfirming(picked)}
            >
              <Trash2Icon /> Delete selected ({picked.length})
            </Button>
            <Button
              size="sm"
              variant="ghost"
              data-testid="library-select-all"
              disabled={unpicked.length === 0}
              onClick={() =>
                setPicked((now) => {
                  const ids = new Set(now.map((p) => p.id))
                  return [...now, ...unpicked.filter((file) => !ids.has(file.id))]
                })
              }
            >
              {/* What it adds: this folder's unticked files, whatever is ticked elsewhere. */}
              Select all in folder ({unpicked.length})
            </Button>
            {picked.length > 0 && (
              <Button size="sm" variant="ghost" onClick={() => setPicked([])}>
                <XIcon /> Clear selection
              </Button>
            )}
            {/* #2286 — a ticked file Arrange cannot take is said, not silently dropped. */}
            {picked.length > printable.length && (
              <span role="note" className="basis-full text-xs text-muted-foreground">
                {`${picked.length - printable.length} selected ${
                  picked.length - printable.length === 1 ? 'file' : 'files'
                } can't be arranged: only unsliced 3MF and STL files can.`}
              </span>
            )}
            <span className="ml-auto flex items-center gap-2">
              <Label id="library-advanced" htmlFor="library-advanced-switch">
                Advanced
              </Label>
              <Switch
                id="library-advanced-switch"
                checked={advanced}
                onCheckedChange={toggleAdvanced}
                aria-labelledby="library-advanced"
                aria-describedby="library-advanced-help"
                data-testid="library-advanced"
              />
            </span>
            <span id="library-advanced-help" className="basis-full text-xs text-muted-foreground">
              Advanced lists every file, sliced files and STLs too.
            </span>
          </header>
          {folder?.project_id != null && listedPrintable.length > 0 && (
            // Not in an empty folder, where Select all is disabled (#3112).
            <p className="text-[13px] text-subtle" data-testid="library-project">
              Select all in folder, then Print, to print this project's files in one batch.
            </p>
          )}
          {batchError && (
            <p role="alert" className="text-[13px] text-warn">
              {batchError}
            </p>
          )}
          {batchParam === null &&
            kept.map((batch) => (
              <p key={batch.id} className="text-[13px] text-subtle" data-testid={`library-kept-batch-${batch.id}`}>
                {`A batch print of ${batch.items.filter((item) => !item.removed).length} files has a file still being checked. `}
                <button type="button" className="text-primary underline" onClick={() => resumeBatch(batch.id)}>
                  Resume it
                </button>
                {' to follow that file, rather than starting it again.'}
              </p>
            ))}

          {!searching && (
            <WhereHeader
              byProject={byProject}
              project={project}
              projectCount={projects.length}
              folder={folder}
              tree={tree}
              entries={folder ? folderEntries(folder, false) : undefined}
              onNewFolder={() => setFolderAction({ kind: 'create', folder: folder ?? null })}
            />
          )}

          {missing && (
            <p role="alert" className="text-sm text-destructive">
              There is no folder {segments.join(' / ')} in the library.{' '}
              <Link to="/library" className="text-primary underline underline-offset-4">
                Top level
              </Link>
            </p>
          )}
          {missingProject && (
            <p role="alert" className="text-sm text-destructive">
              No library folder belongs to project #{projectParam}.{' '}
              <Link to={`${LIBRARY_ROOT}?by=project`} className="text-primary underline underline-offset-4">
                All projects
              </Link>
            </p>
          )}
          {folderError && (
            <p role="alert" className="text-sm text-destructive">
              {folderError}
            </p>
          )}
          {error && !searching && (
            <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
              {error}
              {/* #3633 — a read that timed out can be tried again in place. */}
              <Button size="sm" variant="outline" data-testid="library-retry" onClick={() => setReload((n) => n + 1)}>
                Retry
              </Button>
            </p>
          )}
          {arranged && (
            <p role="status" aria-label="Arranged" className="text-sm">
              {/* #2284 — names where it went: the Library page is not the model's History. */}
              {[arrangedNote(arranged.plates), arranged.skipped, savedNote(arranged)].filter(Boolean).join(' ')}{' '}
              <Link to={modelPath(arranged.output.slug, 'history')} className="text-primary underline underline-offset-4">
                Open in History
              </Link>
            </p>
          )}
          {arranged && (
            <ArrangedNext output={arranged.output} sent={arrangedSent} onPrint={() => setPrintArranged(true)} />
          )}

          {searching ? (
            <SearchResults
              query={query}
              results={results}
              error={searchError}
              advanced={advanced}
              onRetry={() => setReload((n) => n + 1)}
            >
              {(results?.files ?? []).map((file) => (
                <FileCard
                  key={file.id}
                  file={file}
                  where={
                    <Link
                      to={`${libraryPathOf(file.folder_path ?? [])}?file=${file.id}`}
                      data-testid={`library-result-folder-${file.id}`}
                      className="flex min-w-0 items-center gap-1 text-xs text-primary hover:underline"
                    >
                      <FolderIcon aria-hidden className="size-3 shrink-0" />
                      <span className="truncate">In {folderPathText(file.folder_path ?? [])}</span>
                    </Link>
                  }
                  {...cardActions(file)}
                />
              ))}
            </SearchResults>
          ) : byProject && !project ? (
            tree && <ProjectList projects={projects} tree={tree} onSelect={selectProject} />
          ) : (
            <>
              {!listing && !error && !missing && !missingProject && (
                <div role="status" aria-label="Reading the Bambuddy library" className="space-y-3">
                  <span className="sr-only">Reading the Bambuddy library</span>
                  {slow && (
                    <p className="text-xs text-muted-foreground">Bambuddy is slow to answer. You can search meanwhile.</p>
                  )}
                  <CardGrid>
                    {Array.from({ length: 4 }, (_, n) => (
                      <li key={n}>
                        <Card size="sm" className="gap-2 px-2 pt-0">
                          <Skeleton className="aspect-square w-full rounded-b-none" />
                          <Skeleton className="h-4 w-3/4" />
                          <Skeleton className="h-3 w-1/2" />
                          <Skeleton className="h-8 w-full" />
                        </Card>
                      </li>
                    ))}
                  </CardGrid>
                </div>
              )}
              {linkedMissing && (
                <p className="text-sm text-muted-foreground">
                  File #{fileParam} is not listed here{advanced ? '' : '. It may be listed under Advanced'}.
                </p>
              )}
              {subfolders.length > 0 && tree && (
                <Subfolders folders={subfolders} tree={tree} onSelect={selectFolder} menu={folderEntries} />
              )}
              {listing && showFiles && files.length === 0 && subfolders.length === 0 && (
                <p className="text-sm text-muted-foreground">No files here{hidden > 0 ? '' : ' yet'}.</p>
              )}
              {!advanced && hidden > 0 && (
                <p className="sb-num text-xs text-muted-foreground">{hidden} more under Advanced.</p>
              )}

              <CardGrid label="Files">
                {files.map((file) => (
                  <FileCard key={file.id} file={file} {...cardActions(file)} />
                ))}
              </CardGrid>
            </>
          )}
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
        {batchParam !== null && <BatchPrintDialog key={batchParam} batchId={batchParam} onClose={closeBatch} />}
        <ArrangeDialog
          open={arranging !== null}
          sources={fromFiles(arranging ?? [])}
          onClose={() => setArranging(null)}
          onArranged={(result) => {
            setArranging(null)
            setPicked([])
            setArranged(result)
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
        <FolderNameDialog
          action={folderAction}
          folders={tree ?? []}
          onClose={() => setFolderAction(null)}
          onConfirm={(name) =>
            folderAction?.kind === 'rename'
              ? updateFolder(folderAction.folder, { name })
              : createFolder(folderAction?.folder ?? null, name)
          }
        />
        <MoveFolderDialog
          action={folderAction}
          folders={tree ?? []}
          onClose={() => setFolderAction(null)}
          onConfirm={(parentId) =>
            folderAction?.kind === 'move' ? updateFolder(folderAction.folder, { parentId }) : Promise.resolve()
          }
        />
        <DeleteFolderDialog
          action={folderAction}
          folders={tree ?? []}
          onClose={() => setFolderAction(null)}
          onConfirm={() => (folderAction?.kind === 'delete' ? deleteFolder(folderAction.folder) : Promise.resolve())}
        />
        {done && <DoneToast done={done} onDismiss={() => setDone(null)} />}
      </div>
    </div>
  )
}

/** #3650 — a project, as the library's folders link to it. */
interface LibraryProject {
  id: number
  name: string
  folders: LibraryFolderView[]
  /** The files in its folders. */
  files: number
}

function projectsOf(tree: LibraryFolderView[]): LibraryProject[] {
  const byId = new Map<number, LibraryProject>()
  for (const folder of tree) {
    if (folder.project_id == null) continue
    const project = byId.get(folder.project_id) ?? {
      id: folder.project_id,
      name: folder.project_name ?? `Project ${folder.project_id}`,
      folders: [],
      files: 0,
    }
    project.folders.push(folder)
    project.files += folder.file_count ?? 0
    byId.set(folder.project_id, project)
  }
  return [...byId.values()].sort((a, b) => a.name.localeCompare(b.name))
}

/**
 * #3650 — where the page is, with the other way to see it: a folder names its project,
 * and a project its folders, each a link to the other view.
 */
function WhereHeader({
  byProject,
  project,
  projectCount,
  folder,
  tree,
  entries,
  onNewFolder,
}: {
  byProject: boolean
  project: LibraryProject | undefined
  projectCount: number
  folder: LibraryFolderView | undefined
  tree: LibraryFolderView[] | null
  entries: MenuEntry[] | undefined
  onNewFolder: () => void
}) {
  if (tree === null) return null
  if (byProject) {
    return (
      <div data-testid="library-where" className="flex flex-wrap items-center gap-2 border-b pb-3">
        <KanbanSquareIcon aria-hidden className="size-4 text-muted-foreground" />
        <h2 className="text-base font-medium">{project ? project.name : 'All projects'}</h2>
        {project ? (
          <span className="flex flex-wrap items-center gap-1.5 text-sm text-muted-foreground">
            {project.folders.length === 1 ? 'Its files are in the folder' : 'Its files are in the folders'}
            {project.folders.map((row) => (
              <Badge key={row.id} variant="outline" asChild>
                <Link to={libraryPath(tree, row.id)} data-testid={`library-project-folder-${row.id}`}>
                  <FolderIcon /> {folderLabel(tree, row.id)}
                </Link>
              </Badge>
            ))}
            {project.folders.length > 1 && project.folders[0] && (
              <span data-testid="library-project-shown">
                Showing the files in {folderLabel(tree, project.folders[0].id)}.
              </span>
            )}
          </span>
        ) : (
          <span className="text-sm text-muted-foreground">{plural(projectCount, 'project')} with library folders</span>
        )}
      </div>
    )
  }
  return (
    <div data-testid="library-where" className="flex flex-wrap items-center gap-2 border-b pb-3">
      {folder ? <FolderOpenIcon aria-hidden className="size-4 text-muted-foreground" /> : <FolderUpIcon aria-hidden className="size-4 text-muted-foreground" />}
      <h2 className="min-w-0 truncate text-base font-medium">{folder ? folderLabel(tree, folder.id) : 'Top level'}</h2>
      {folder?.project_id != null && (
        <span className="flex items-center gap-1.5 text-sm text-muted-foreground">
          in the project
          <Badge variant="outline" asChild>
            <Link to={`${LIBRARY_ROOT}?project=${folder.project_id}`} data-testid="library-folder-project-link">
              <KanbanSquareIcon /> {folder.project_name ?? `#${folder.project_id}`}
            </Link>
          </Badge>
        </span>
      )}
      <span className="ml-auto flex items-center gap-1">
        <Button size="sm" variant="ghost" onClick={onNewFolder} data-testid="library-new-folder">
          <FolderPlusIcon /> New folder
        </Button>
        {folder && entries && (
          <MoreMenu
            label={`Actions for the folder ${folder.name}`}
            heading={folder.name}
            entries={entries}
            testId="library-folder-actions"
          />
        )}
      </span>
    </div>
  )
}

/** #3650 — every project with a library folder, each opening its files. */
function ProjectList({
  projects,
  tree,
  onSelect,
}: {
  projects: LibraryProject[]
  tree: LibraryFolderView[]
  onSelect: (id: number) => void
}) {
  if (projects.length === 0) {
    return (
      <Card className="items-center px-6 py-10 text-center text-sm text-muted-foreground">
        No library folder is linked to a Bambuddy project yet. A project made from ScadBuddy gets one.
      </Card>
    )
  }
  return (
    <ul aria-label="Projects" className="grid grid-cols-[repeat(auto-fill,minmax(min(220px,100%),1fr))] gap-3">
      {projects.map((project) => (
        <li key={project.id}>
          <Card size="sm" className="h-full gap-2 px-3 transition-colors hover:bg-muted/40">
            <button
              type="button"
              onClick={() => onSelect(project.id)}
              data-testid={`library-project-card-${project.id}`}
              className="flex min-w-0 items-center gap-2 text-left font-medium outline-none focus-visible:underline"
            >
              <KanbanSquareIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
              <span className="truncate">{project.name}</span>
              <Badge variant="secondary" className="sb-num ml-auto">
                {plural(project.files, 'file')}
              </Badge>
            </button>
            <span className="flex flex-wrap gap-1">
              {project.folders.map((folder) => (
                <Badge key={folder.id} variant="outline" asChild>
                  <Link to={libraryPath(tree, folder.id)}>
                    <FolderIcon /> {folderLabel(tree, folder.id)}
                  </Link>
                </Badge>
              ))}
            </span>
          </Card>
        </li>
      ))}
    </ul>
  )
}

/** The cards' grid. */
function CardGrid({ label, children }: { label?: string; children: ReactNode }) {
  return (
    <ul aria-label={label} className="grid grid-cols-[repeat(auto-fill,minmax(min(170px,100%),1fr))] gap-3">
      {children}
    </ul>
  )
}

/** The bottom panel a delete, copy, move or rename reports in, with its Dismiss. */
function ResultPanel({ testId, onDismiss, children }: { testId: string; onDismiss: () => void; children: ReactNode }) {
  return (
    <Card
      role="status"
      data-testid={testId}
      size="sm"
      className="fixed inset-x-4 bottom-4 z-40 mx-auto max-w-xl gap-1.5 px-3 text-sm shadow-lg"
    >
      <Button
        variant="ghost"
        size="icon-xs"
        aria-label="Dismiss"
        onClick={onDismiss}
        className="absolute top-2 right-2 text-muted-foreground"
      >
        <XIcon />
      </Button>
      {children}
    </Card>
  )
}

/** Files Bambuddy left where they were, and why. */
function SkippedList({ heading, files, testId }: { heading: string; files: { id: number; filename?: string | null; reason: string }[]; testId: string }) {
  return (
    <div data-testid={testId}>
      <p className="text-destructive">{heading}</p>
      <ul className="list-disc pl-5 text-xs break-words text-muted-foreground">
        {files.map((file) => (
          <li key={file.id}>
            {file.filename ?? `#${file.id}`}: {file.reason}
          </li>
        ))}
      </ul>
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
  const { result, restoring, restored, error, folder } = deleted
  const undoable = result.deleted.filter((file) => file.trashed).length
  const gone = result.deleted.length - undoable
  const what =
    result.deleted.length > 0
      ? `Moved ${plural(undoable, 'file')} to Bambuddy's trash.${gone > 0 ? ` Removed ${plural(gone, 'external file')} for good.` : ''}`
      : folder === undefined
        ? 'Nothing was deleted.'
        : ''
  return (
    <ResultPanel testId="library-deleted" onDismiss={onDismiss}>
      <div className="flex items-start gap-3 pr-7">
        <p className="min-w-0 flex-1">
          {restored !== null
            ? `Restored ${plural(restored, 'file')}.`
            : folder !== undefined
              ? `Deleted the folder ${folder}. ${what}`.trim()
              : what}
        </p>
        {restored === null && undoable > 0 && (
          <Button size="sm" variant="outline" onClick={onUndo} disabled={restoring} data-testid="library-undo">
            {restoring ? <Spinner /> : 'Undo'}
          </Button>
        )}
      </div>
      {restored === null && result.skipped.length > 0 && (
        <SkippedList heading="Not deleted:" files={result.skipped} testId="library-skipped" />
      )}
      {error && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
    </ResultPanel>
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
    <ResultPanel testId="library-done" onDismiss={onDismiss}>
      <p className="min-w-0 pr-7">{summary}</p>
      {losses.map((line) => (
        <p key={line} className="text-destructive">
          {line}
        </p>
      ))}
      {skipped.length > 0 && (
        <SkippedList
          heading={done.kind === 'copy' ? 'Not copied:' : 'Not moved:'}
          files={skipped}
          testId="library-done-skipped"
        />
      )}
    </ResultPanel>
  )
}

/** How long a listing is read before the page says Bambuddy is slow (#3633). */
const SLOW_MS = 4000
/** How long typing pauses before the search is run (#3630). */
const SEARCH_DEBOUNCE_MS = 250
/** The subfolders shown before "Show all" (#3631). */
const FOLDERS_SHOWN = 12

function folderPathText(names: string[]): string {
  return names.length === 0 ? 'Top level' : names.join(' / ')
}

/** #3630 — one box that finds a file by name in every folder. */
function SearchBox({ query, onSearch }: { query: string; onSearch: (text: string) => void }) {
  const [typed, setTyped] = useState(query)
  // Back or a link changes the query under the box: the box follows it.
  const [shown, setShown] = useState(query)
  if (shown !== query) {
    setShown(query)
    setTyped(query)
  }
  const search = useEffectEvent((text: string) => onSearch(text))
  useEffect(() => {
    if (typed === shown) return
    const timer = window.setTimeout(() => search(typed), SEARCH_DEBOUNCE_MS)
    return () => window.clearTimeout(timer)
  }, [typed, shown])
  return (
    <form
      role="search"
      onSubmit={(event) => {
        event.preventDefault()
        if (typed !== shown) onSearch(typed)
      }}
      className="relative"
    >
      <SearchIcon aria-hidden className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-muted-foreground" />
      <Input
        type="search"
        aria-label="Search the library"
        data-testid="library-search"
        placeholder="Search every folder by file name"
        value={typed}
        onChange={(event) => setTyped(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === 'Escape' && typed !== '') {
            event.preventDefault()
            setTyped('')
            onSearch('')
          }
        }}
        className="h-9 pl-9"
      />
    </form>
  )
}

/** #3630 — what a search found, with the cards it is given. */
function SearchResults({
  query,
  results,
  error,
  advanced,
  onRetry,
  children,
}: {
  query: string
  results: LibrarySearch | null
  error: string | null
  advanced: boolean
  onRetry: () => void
  children: ReactNode
}) {
  const current = results !== null && results.query === query && results.all === advanced
  return (
    <div className="space-y-3" aria-busy={!current && !error}>
      {error ? (
        <p role="alert" className="flex flex-wrap items-center gap-2 text-sm text-destructive">
          {error}
          <Button size="sm" variant="outline" data-testid="library-search-retry" onClick={onRetry}>
            Retry
          </Button>
        </p>
      ) : !current ? (
        <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
          <Spinner /> Searching every folder
        </p>
      ) : (
        <p role="status" data-testid="library-search-summary" className="text-sm">
          {!results.total
            ? `No files match “${query}”.`
            : results.total > (results.files ?? []).length
              ? `The first ${(results.files ?? []).length} of ${results.total} files that match “${query}”.`
              : `${plural(results.total, 'file')} ${results.total === 1 ? 'matches' : 'match'} “${query}”.`}
          {!advanced && (results.hidden ?? 0) > 0 && (
            <span className="sb-num ml-1 text-xs text-muted-foreground">{results.hidden} more under Advanced.</span>
          )}
        </p>
      )}
      {results !== null && <CardGrid label="Search results">{children}</CardGrid>}
    </div>
  )
}

/** #3631 — a folder's subfolders in the main pane: a folder of folders is not empty. */
function Subfolders({
  folders,
  tree,
  onSelect,
  menu,
}: {
  folders: LibraryFolderView[]
  tree: LibraryFolderView[]
  onSelect: (id: number) => void
  /** #3650 — a folder's actions, on right-click and on its ⋯ button. */
  menu: (folder: LibraryFolderView) => MenuEntry[]
}) {
  const [all, setAll] = useState(false)
  const shown = all ? folders : folders.slice(0, FOLDERS_SHOWN)
  const inside = (id: number) => tree.filter((folder) => folder.parent_id === id).length
  return (
    <section aria-labelledby="library-subfolders" className="space-y-2">
      <h2 id="library-subfolders" className="text-xs font-medium text-muted-foreground">
        {plural(folders.length, 'folder')}
      </h2>
      <ul className="grid grid-cols-[repeat(auto-fill,minmax(min(200px,100%),1fr))] gap-2">
        {shown.map((folder) => {
          const subfolders = inside(folder.id)
          const facts = [
            folder.file_count ? plural(folder.file_count, 'file') : null,
            subfolders ? plural(subfolders, 'folder') : null,
          ].filter(Boolean)
          return (
            <li key={folder.id}>
              <RowContextMenu label={folder.name} entries={menu(folder)}>
                <Card size="sm" className="flex-row items-center gap-1 py-0 pr-1 pl-0 transition-colors hover:bg-muted/50">
                  <button
                    type="button"
                    data-testid={`library-subfolder-${folder.id}`}
                    onClick={() => onSelect(folder.id)}
                    className="flex min-w-0 flex-1 items-center gap-2.5 rounded-l-xl py-2.5 pl-3 text-left outline-none focus-visible:ring-3 focus-visible:ring-ring/50"
                  >
                    <FolderIcon aria-hidden className="size-4 shrink-0 text-muted-foreground" />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">{folder.name}</span>
                      <span className="sb-num block text-xs text-muted-foreground">{facts.join(', ') || 'Empty'}</span>
                    </span>
                  </button>
                  <MoreMenu label={`Actions for the folder ${folder.name}`} heading={folder.name} entries={menu(folder)} />
                </Card>
              </RowContextMenu>
            </li>
          )
        })}
      </ul>
      {folders.length > FOLDERS_SHOWN && (
        <Button size="sm" variant="ghost" onClick={() => setAll((now) => !now)}>
          {all ? 'Show fewer folders' : `Show all ${folders.length} folders`}
        </Button>
      )}
    </section>
  )
}

interface FileCardProps {
  file: LibraryEntry
  /** Where the file is, on a search result. */
  where?: ReactNode
  highlighted: boolean
  picked: boolean
  actionsDisabled: boolean
  onPick: () => void
  onPrint: () => void
  onRename: () => void
  onTransfer: (mode: TransferMode) => void
  onDelete: () => void
}

function FileCard({
  file,
  where,
  highlighted,
  picked,
  actionsDisabled,
  onPick,
  onPrint,
  onRename,
  onTransfer,
  onDelete,
}: FileCardProps) {
  // #2670, #3650 — Rename, Copy to… and Move to… on the ⋯ button and on right-click.
  const entries: MenuEntry[] = [
    ...(file.printable ? [{ label: 'Print…', icon: PrinterIcon, onSelect: onPrint }] : []),
    { label: 'Rename…', icon: PencilIcon, onSelect: onRename, disabled: actionsDisabled, separator: file.printable },
    { label: 'Copy to…', icon: CopyIcon, onSelect: () => onTransfer('copy'), disabled: actionsDisabled },
    { label: 'Move to…', icon: FolderInputIcon, onSelect: () => onTransfer('move'), disabled: actionsDisabled },
    { label: 'Delete…', icon: Trash2Icon, onSelect: onDelete, destructive: true, separator: true },
  ]
  return (
    <li data-testid={`library-file-${file.id}`} aria-current={highlighted ? 'true' : undefined} className="min-w-0">
      <RowContextMenu label={file.filename} entries={entries}>
        <Card
          size="sm"
          className={cn(
            'h-full gap-2 px-2 pt-2 transition-shadow',
            picked && 'ring-2 ring-primary/60',
            highlighted && 'ring-2 ring-primary',
          )}
        >
          <div className="relative">
            {file.has_thumbnail ? (
              <img
                src={api.libraryThumbnailUrl(file.id)}
                alt=""
                loading="lazy"
                className="aspect-square w-full rounded-lg bg-muted/50 object-contain"
              />
            ) : (
              <div aria-hidden className="grid aspect-square w-full place-items-center rounded-lg bg-muted/50">
                <span className="text-xs font-medium tracking-wide text-muted-foreground uppercase">
                  {nameParts(file.filename).extension || 'file'}
                </span>
              </div>
            )}
            <Checkbox
              aria-label={`Select ${file.filename}`}
              checked={picked}
              onCheckedChange={onPick}
              className="absolute top-2 left-2 bg-background/90 shadow-sm"
            />
          </div>
          <FileName file={file} />
          {where}
          <FileFacts file={file} />
          {file.printable ? (
            <Button data-testid={`library-print-${file.id}`} aria-label={`Print ${file.filename}`} onClick={onPrint}>
              <PrinterIcon /> Print
            </Button>
          ) : (
            <p className="text-xs text-muted-foreground">ScadBuddy cannot print this file type.</p>
          )}
          <div className="flex items-center justify-between gap-1 text-xs">
            {file.printable ? (
              // #1755 — the file's own print history, as a template has its Prints tab.
              <Button variant="link" size="xs" className="px-0 text-muted-foreground" asChild>
                <Link to={libraryPrintsPath(file.id)} aria-label={`Prints of ${file.filename}`}>
                  Prints
                </Link>
              </Button>
            ) : (
              <span />
            )}
            <span className="flex items-center gap-0.5">
              <MoreMenu
                label={`More actions for ${file.filename}`}
                heading={file.filename}
                entries={entries}
                disabled={actionsDisabled}
                testId={`library-actions-${file.id}`}
              />
              <Button
                variant="ghost"
                size="icon-sm"
                data-testid={`library-delete-${file.id}`}
                aria-label={`Delete ${file.filename}`}
                onClick={onDelete}
                className="text-muted-foreground hover:text-destructive"
              >
                <Trash2Icon />
              </Button>
            </span>
          </div>
        </Card>
      </RowContextMenu>
    </li>
  )
}

/**
 * #3634 — a card's name on up to two lines, cut in the middle only past that, at word
 * breaks (#935: generated names share a long prefix, so the end must show). What
 * ScadBuddy adds to a name, ` (ScadBuddy)` and ` (2)`, and a sliced file's
 * `.gcode.3mf` are badges, so the part that tells two cards apart is never what is cut.
 * The whole name is its tooltip.
 */
function FileName({ file }: { file: LibraryEntry }) {
  const parts = nameParts(file.filename)
  const shown = shortName(parts.stem)
  const badges = [
    file.sliced ? 'Sliced' : null,
    parts.scadbuddy ? 'ScadBuddy copy' : null,
    parts.copy !== null ? `Copy ${parts.copy}` : null,
    !file.sliced && parts.extension !== '' && parts.extension !== '3mf' ? parts.extension.toUpperCase() : null,
  ].filter((badge) => badge !== null)
  return (
    <div className="min-w-0">
      <Tooltip>
        <TooltipTrigger asChild>
          <p
            data-testid={`library-name-${file.id}`}
            data-filename={file.filename}
            className={cn(
              'line-clamp-2 text-sm leading-snug font-medium',
              // A cut name fills both lines, so its tail is never what the clamp hides.
              shown === parts.stem ? '[overflow-wrap:anywhere]' : 'break-all',
            )}
          >
            {shown}
          </p>
        </TooltipTrigger>
        <TooltipContent className="break-all">{file.filename}</TooltipContent>
      </Tooltip>
      {badges.length > 0 && (
        <p className="mt-1 flex flex-wrap gap-1">
          {badges.map((badge) => (
            <Badge key={badge} variant={badge === 'Sliced' ? 'default' : 'outline'} className="h-4.5 px-1.5 text-[10px]">
              {badge}
            </Badge>
          ))}
        </p>
      )}
    </div>
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
  return <p className="sb-num -mt-1 flex flex-wrap gap-x-2 text-[11px] text-muted-foreground">{facts}</p>
}
