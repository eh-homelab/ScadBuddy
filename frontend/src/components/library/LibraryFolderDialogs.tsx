import { useEffect, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError } from '../../api/client'
import type { LibraryFolderView } from '../../api/types'
import { folderAndDescendants, folderLabel } from '../../lib/libraryTransfer'
import { Spinner } from '../ui/Spinner'
import { LibraryTree } from './LibraryTree'
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { ScrollArea } from '@/components/ui/scroll-area'

/** What a folder action asks for: the action, and the folder it acts on (the parent, for `create`). */
export type FolderAction =
  | { kind: 'rename' | 'move' | 'delete'; folder: LibraryFolderView }
  | { kind: 'create'; folder: LibraryFolderView | null }

function message(caught: unknown): string {
  return caught instanceof ApiError ? caught.detail : String(caught)
}

/** Runs `action`, holding the dialog open and showing its refusal. */
function useRun() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  async function run(action: () => Promise<void>) {
    setBusy(true)
    setError(null)
    try {
      await action()
    } catch (caught) {
      setError(message(caught))
    } finally {
      setBusy(false)
    }
  }
  return { busy, error, setError, run }
}

/** #3650 — a folder's new name, or a new folder's name. */
export function FolderNameDialog({
  action,
  folders,
  onClose,
  onConfirm,
}: {
  action: FolderAction | null
  folders: LibraryFolderView[]
  onClose: () => void
  onConfirm: (name: string) => Promise<void>
}) {
  const open = action !== null && (action.kind === 'rename' || action.kind === 'create')
  const renaming = action?.kind === 'rename' ? action.folder : null
  const [value, setValue] = useState('')
  const { busy, error, setError, run } = useRun()

  const key = open ? `${action.kind}:${action.folder?.id ?? 'top'}` : null
  useEffect(() => {
    if (key === null) return
    setValue(renaming?.name ?? '')
    setError(null)
    // `renaming` is read through `key`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  const name = value.trim()
  // The backend's folder `Name`: 1-255 characters (code points, as Pydantic counts), no
  // `/` and no NUL.
  const usable = name !== '' && [...name].length <= 255 && !/[/\0]/.test(name) && name !== renaming?.name
  const where = action?.kind === 'create' ? folderLabel(folders, action.folder?.id ?? null) : null

  function close() {
    if (!busy) onClose()
  }

  return (
    <Dialog open={open} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="break-all leading-snug">
            {renaming ? `Rename the folder ${renaming.name}` : 'New folder'}
          </DialogTitle>
          <DialogDescription>
            {renaming
              ? 'Its files stay in it, with their print history.'
              : `In ${where}.`}
          </DialogDescription>
        </DialogHeader>
        <form
          id="library-folder-name-form"
          className="grid gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            if (usable) void run(() => onConfirm(name))
          }}
        >
          <Label htmlFor="library-folder-name">Folder name</Label>
          <Input
            id="library-folder-name"
            data-testid="library-folder-name"
            autoFocus
            value={value}
            maxLength={255}
            onChange={(event) => setValue(event.target.value)}
          />
        </form>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button
            type="submit"
            form="library-folder-name-form"
            data-testid="library-folder-name-confirm"
            disabled={busy || !usable}
            {...USER_ONLY}
          >
            {busy ? <Spinner /> : renaming ? 'Rename' : 'Create folder'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** #3650 — a folder moved under another, picked from the tree it is not part of. */
export function MoveFolderDialog({
  action,
  folders,
  onClose,
  onConfirm,
}: {
  action: FolderAction | null
  folders: LibraryFolderView[]
  onClose: () => void
  onConfirm: (parentId: number | null) => Promise<void>
}) {
  const folder = action?.kind === 'move' ? action.folder : null
  const [target, setTarget] = useState<number | null>(null)
  const { busy, error, setError, run } = useRun()

  useEffect(() => {
    if (!folder) return
    setTarget(folder.parent_id ?? null)
    setError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folder?.id])

  const inside = folder ? folderAndDescendants(folders, folder.id) : new Set<number>()
  // The folder cannot go into itself or a folder of its own: those are not offered.
  const offered = folders.filter((row) => !inside.has(row.id))
  const here = (folder?.parent_id ?? null) === target

  function close() {
    if (!busy) onClose()
  }

  return (
    <Dialog open={folder !== null} onOpenChange={(next) => !next && close()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="break-all leading-snug">
            {folder ? `Move the folder ${folder.name} to…` : 'Move to…'}
          </DialogTitle>
          <DialogDescription>Everything in it moves with it.</DialogDescription>
        </DialogHeader>
        <ScrollArea className="h-64 rounded-lg border p-1">
          <LibraryTree
            folders={offered}
            selected={target}
            onSelect={setTarget}
            label="Destination folder"
            testIdPrefix="library-folder-move"
          />
        </ScrollArea>
        {here && <p className="text-sm text-muted-foreground">It is in {folderLabel(folders, target)} already.</p>}
        {folder?.project_id != null && (
          <p className="text-sm text-muted-foreground">
            It stays linked to the project {folder.project_name ?? `#${folder.project_id}`}.
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button
            data-testid="library-folder-move-confirm"
            disabled={busy || here}
            onClick={() => void run(() => onConfirm(target))}
            className="max-w-full"
            {...USER_ONLY}
          >
            {busy ? <Spinner /> : <span className="truncate">{`Move to ${folderLabel(folders, target)}`}</span>}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

/** #3650 — the confirmation before a folder and everything in it goes. */
export function DeleteFolderDialog({
  action,
  folders,
  onClose,
  onConfirm,
}: {
  action: FolderAction | null
  folders: LibraryFolderView[]
  onClose: () => void
  onConfirm: () => Promise<void>
}) {
  const folder = action?.kind === 'delete' ? action.folder : null
  const { busy, error, setError, run } = useRun()
  useEffect(() => {
    if (folder) setError(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [folder?.id])

  const inside = folder ? folderAndDescendants(folders, folder.id) : new Set<number>()
  const subfolders = inside.size - 1
  // Read as each folder's own files, not its subfolders' too, as the tree's badges show
  // them; the folder's whole count is then their sum. Were Bambuddy's `file_count` to
  // include subfolders, this would count those files twice.
  const files = folders.filter((row) => inside.has(row.id)).reduce((sum, row) => sum + (row.file_count ?? 0), 0)

  function close() {
    if (!busy) onClose()
  }

  return (
    <AlertDialog open={folder !== null} onOpenChange={(next) => !next && close()}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="break-all">{folder ? `Delete the folder ${folder.name}?` : 'Delete?'}</AlertDialogTitle>
          <AlertDialogDescription>
            {[
              files > 0 ? `Its ${files === 1 ? 'file goes' : `${files} files go`} to Bambuddy's trash, which can restore them.` : 'It holds no files.',
              subfolders > 0 ? `Its ${subfolders === 1 ? 'subfolder goes' : `${subfolders} subfolders go`} too.` : null,
              'A folder itself cannot be restored.',
            ]
              .filter(Boolean)
              .join(' ')}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {folder?.project_id != null && (
          <p className="text-sm text-destructive">
            The project {folder.project_name ?? `#${folder.project_id}`} loses its library folder, and with it the files its
            page lists.
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={busy}>Cancel</AlertDialogCancel>
          <Button
            variant="destructive"
            data-testid="library-folder-delete-confirm"
            disabled={busy}
            onClick={() => void run(onConfirm)}
            {...USER_ONLY}
          >
            {busy ? <Spinner /> : 'Delete folder'}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
