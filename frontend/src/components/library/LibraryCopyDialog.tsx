import { useEffect, useRef, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api, ApiError } from '../../api/client'
import type { LibraryCopyFileResult, LibraryCopyRequest, LibraryCopyStatus, LibraryEntry, LibraryFolderView, LibraryListing, Settings } from '../../api/types'
import { folderLabel } from '../../lib/libraryTransfer'
import { Spinner } from '../ui/spinner'
import { LibraryTree } from './LibraryTree'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ToggleGroup, ToggleGroupItem } from '@/components/ui/toggle-group'

type Direction = LibraryCopyRequest['direction']

const NAMES: Record<Direction, { source: string; target: string }> = {
  bb_to_sb: { source: "Bambuddy's library", target: "ScadBuddy's library" },
  sb_to_bb: { source: "ScadBuddy's library", target: "Bambuddy's library" },
}

/** How often a running copy's status is read. */
export const POLL_MS = 1500

interface Props {
  open: boolean
  onClose: () => void
  /** ScadBuddy's own folders, the page's tree (the target of a Bambuddy-to-ScadBuddy copy, the source of the other). */
  nativeFolders: LibraryFolderView[]
  /** The page's ticked files: a ScadBuddy-to-Bambuddy copy starts with them. */
  picked: LibraryEntry[]
  /** Called once a copy ended with something copied, so the page lists it. */
  onCopied?: () => void
}

/**
 * #5355 — a copy between Bambuddy's library and ScadBuddy's own, either way: pick the
 * source files or folders, the target folder, start it, and watch its per-file results.
 * The job runs on the server; the dialog only polls it.
 */
export function LibraryCopyDialog({ open, onClose, nativeFolders, picked, onCopied }: Props) {
  const [settings, setSettings] = useState<Settings | null>(null)
  const [direction, setDirection] = useState<Direction>('bb_to_sb')
  const [bambuddyFolders, setBambuddyFolders] = useState<LibraryFolderView[] | null>(null)
  const [sourceFolder, setSourceFolder] = useState<number | null>(null)
  const [sourceFiles, setSourceFiles] = useState<LibraryEntry[] | null>(null)
  const [fileIds, setFileIds] = useState<Set<number>>(new Set())
  const [folderIds, setFolderIds] = useState<Set<number>>(new Set())
  const [target, setTarget] = useState<number | null>(null)
  const [status, setStatus] = useState<LibraryCopyStatus | null>(null)
  const [starting, setStarting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const copied = useRef(false)

  // Each opening starts over: the settings say whether the copy can run at all.
  useEffect(() => {
    if (!open) return
    setSettings(null)
    setStatus(null)
    setError(null)
    setDirection('bb_to_sb')
    setFileIds(new Set())
    setFolderIds(new Set())
    setTarget(null)
    setSourceFolder(null)
    copied.current = false
    let cancelled = false
    api
      .getSettings()
      .then((loaded) => {
        if (!cancelled) setSettings(loaded)
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.detail : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [open])

  const native = settings?.library_backend === 'native'

  // A direction change starts the selection over on the other side: the page's ticked
  // files seed a ScadBuddy-to-Bambuddy copy.
  useEffect(() => {
    if (!open) return
    setFileIds(new Set(direction === 'sb_to_bb' ? picked.map((file) => file.id) : []))
    setFolderIds(new Set())
    setTarget(null)
    setSourceFolder(direction === 'sb_to_bb' ? (picked[0]?.folder_id ?? null) : null)
    // The ids in the arrays are what matter; `picked` itself is a new array each render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, direction])

  // Bambuddy's tree, once per opening: the source of one direction, the target of the other.
  useEffect(() => {
    if (!open || !native) return
    let cancelled = false
    setBambuddyFolders(null)
    api
      .getBambuddyLibrary(null)
      .then((listing) => {
        if (!cancelled) setBambuddyFolders(listing.folders ?? [])
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.detail : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [open, native])

  // The source folder's files, from whichever library is the source.
  useEffect(() => {
    if (!open || !native) return
    let cancelled = false
    setSourceFiles(null)
    const read =
      direction === 'bb_to_sb'
        ? api.getBambuddyLibrary(sourceFolder)
        : api.listLibraryPage({ folderId: sourceFolder, all: true }, { limit: 500 })
    read
      .then((listing: LibraryListing) => {
        if (!cancelled) setSourceFiles(listing.files ?? [])
      })
      .catch((caught: unknown) => {
        if (!cancelled) setError(caught instanceof ApiError ? caught.detail : String(caught))
      })
    return () => {
      cancelled = true
    }
  }, [open, native, direction, sourceFolder])

  // A running copy is read until it ends.
  useEffect(() => {
    if (!status?.running) return
    const id = status.copy_id
    const timer = window.setTimeout(() => {
      api
        .getLibraryCopy(id)
        .then((next) => {
          setStatus(next)
          if (!next.running && next.copied > 0 && !copied.current) {
            copied.current = true
            onCopied?.()
          }
        })
        .catch((caught: unknown) => setError(caught instanceof ApiError ? caught.detail : String(caught)))
    }, POLL_MS)
    return () => window.clearTimeout(timer)
  }, [status, onCopied])

  const sourceFolders = direction === 'bb_to_sb' ? (bambuddyFolders ?? []) : nativeFolders
  const targetFolders = direction === 'bb_to_sb' ? nativeFolders : (bambuddyFolders ?? [])
  const names = NAMES[direction]
  const selected = fileIds.size + folderIds.size

  function toggleFile(id: number, on: boolean) {
    setFileIds((now) => {
      const next = new Set(now)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  }

  function toggleFolder(id: number | null, on: boolean) {
    if (id === null) return
    setFolderIds((now) => {
      const next = new Set(now)
      if (on) next.add(id)
      else next.delete(id)
      return next
    })
  }

  async function start() {
    setStarting(true)
    setError(null)
    try {
      const started = await api.startLibraryCopy({
        direction,
        file_ids: [...fileIds],
        folder_ids: [...folderIds],
        target_folder_id: target,
      })
      setStatus(await api.getLibraryCopy(started.copy_id))
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setStarting(false)
    }
  }

  function close() {
    if (starting) return
    onClose()
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-3xl">
        <DialogHeader>
          <DialogTitle>Copy between libraries</DialogTitle>
          <DialogDescription>
            Copies run on the server. A file whose bytes the target already holds is skipped; folders keep their
            path; nothing at the target is deleted or overwritten.
          </DialogDescription>
        </DialogHeader>
        {settings !== null && !native && (
          <p role="alert" className="text-sm text-destructive" data-testid="library-copy-not-native">
            Copying needs ScadBuddy&apos;s own library: set the library backend to native in Settings first.
          </p>
        )}
        {status === null && native && (
          <div className="space-y-3">
            <ToggleGroup
              type="single"
              variant="outline"
              size="sm"
              spacing={0}
              value={direction}
              onValueChange={(value) => value && setDirection(value as Direction)}
              aria-label="Direction"
            >
              <ToggleGroupItem value="bb_to_sb" data-testid="library-copy-direction-bb_to_sb">
                Bambuddy → ScadBuddy
              </ToggleGroupItem>
              <ToggleGroupItem value="sb_to_bb" data-testid="library-copy-direction-sb_to_bb">
                ScadBuddy → Bambuddy
              </ToggleGroupItem>
            </ToggleGroup>
            <div className="grid gap-3 sm:grid-cols-2">
              <section className="space-y-2">
                <h3 className="text-sm font-medium">From {names.source}</h3>
                <ScrollArea className="h-40 rounded-lg border p-1">
                  <LibraryTree
                    folders={sourceFolders}
                    selected={sourceFolder}
                    onSelect={setSourceFolder}
                    label="Source folder"
                    testIdPrefix="library-copy-source"
                  />
                </ScrollArea>
                {sourceFolder !== null && (
                  <label className="flex items-center gap-2 text-sm">
                    <Checkbox
                      checked={folderIds.has(sourceFolder)}
                      onCheckedChange={(on) => toggleFolder(sourceFolder, on === true)}
                      data-testid="library-copy-whole-folder"
                    />
                    Copy the whole folder {folderLabel(sourceFolders, sourceFolder)}, subfolders too
                  </label>
                )}
                <ScrollArea className="h-40 rounded-lg border p-1">
                  {sourceFiles === null ? (
                    <Spinner />
                  ) : sourceFiles.length === 0 ? (
                    <p className="p-2 text-sm text-muted-foreground">No files here.</p>
                  ) : (
                    <ul aria-label="Source files" className="space-y-1 p-1">
                      {sourceFiles.map((file) => (
                        <li key={file.id}>
                          <label className="flex items-center gap-2 text-sm break-all">
                            <Checkbox
                              checked={fileIds.has(file.id)}
                              onCheckedChange={(on) => toggleFile(file.id, on === true)}
                              data-testid={`library-copy-file-${file.id}`}
                            />
                            {file.filename}
                          </label>
                        </li>
                      ))}
                    </ul>
                  )}
                </ScrollArea>
              </section>
              <section className="space-y-2">
                <h3 className="text-sm font-medium">Into {names.target}</h3>
                <ScrollArea className="h-[21.5rem] rounded-lg border p-1">
                  <LibraryTree
                    folders={targetFolders}
                    selected={target}
                    onSelect={setTarget}
                    label="Target folder"
                    testIdPrefix="library-copy-target"
                  />
                </ScrollArea>
              </section>
            </div>
          </div>
        )}
        {status !== null && <CopyProgress status={status} />}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={starting}>
            {status === null ? 'Cancel' : 'Close'}
          </Button>
          {status === null && native && (
            <Button
              data-testid="library-copy-start"
              onClick={() => void start()}
              disabled={starting || selected === 0}
              {...USER_ONLY}
            >
              {starting ? <Spinner /> : `Copy ${selected === 1 ? '1 item' : `${selected} items`} to ${folderLabel(targetFolders, target)}`}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

const LABELS: Record<LibraryCopyFileResult['status'], string> = {
  pending: 'Pending',
  copied: 'Copied',
  skipped_same_hash: 'Already there',
  failed: 'Failed',
}

function CopyProgress({ status }: { status: LibraryCopyStatus }) {
  return (
    <div className="space-y-2" data-testid="library-copy-progress">
      <p className="text-sm" aria-live="polite">
        {status.running ? (
          <>
            <Spinner /> Copying… {status.copied + status.skipped_same_hash + status.failed} of {status.total} done
          </>
        ) : status.status === 'failed' ? (
          `The copy did not run: ${status.error ?? 'unknown error'}`
        ) : (
          `Done: ${status.copied} copied, ${status.skipped_same_hash} already there, ${status.failed} failed.`
        )}
      </p>
      <ScrollArea className="h-64 rounded-lg border">
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-muted-foreground">
              <th className="p-2">File</th>
              <th className="p-2">Result</th>
            </tr>
          </thead>
          <tbody>
            {(status.files ?? []).map((file) => (
              <tr key={file.source_id} data-testid={`library-copy-result-${file.source_id}`}>
                <td className="p-2 break-all">{file.path ? `${file.path}/${file.name}` : file.name}</td>
                <td className="p-2">
                  <Badge variant={file.status === 'failed' ? 'destructive' : file.status === 'copied' ? 'default' : 'secondary'}>
                    {LABELS[file.status]}
                  </Badge>
                  {file.reason && <span className="ml-2 text-muted-foreground">{file.reason}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </ScrollArea>
    </div>
  )
}
