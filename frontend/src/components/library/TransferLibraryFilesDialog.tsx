import { useEffect, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError } from '../../api/client'
import type { LibraryEntry, LibraryFolderView } from '../../api/types'
import { folderLabel, projectLosses } from '../../lib/libraryTransfer'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'
import { LibraryTree } from './LibraryTree'

export type TransferMode = 'copy' | 'move'

/** How many names a list in the dialog shows before "and n more". */
const LISTED = 8

interface Props {
  mode: TransferMode
  files: LibraryEntry[]
  folders: LibraryFolderView[]
  onClose: () => void
  /** Copies or moves them into `folderId` (`null`, the top level); a refusal is thrown and shown. */
  onConfirm: (files: LibraryEntry[], folderId: number | null) => Promise<void>
}

function names(files: LibraryEntry[]): string {
  const shown = files.slice(0, LISTED).map((file) => file.filename)
  return files.length > LISTED ? `${shown.join(', ')} and ${files.length - LISTED} more` : shown.join(', ')
}

/**
 * #2670 — Copy to… and Move to…: a folder picked from the library's own tree. A move that
 * takes files out of a project's folder says which project loses them before it runs.
 */
export function TransferLibraryFilesDialog({ mode, files, folders, onClose, onConfirm }: Props) {
  const [target, setTarget] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const open = files.length > 0
  const one = files.length === 1
  const verb = mode === 'copy' ? 'Copy' : 'Move'

  // Each opening starts on the folder the files are in.
  const from = files[0]?.folder_id ?? null
  useEffect(() => {
    if (open) setTarget(from)
    else setError(null)
  }, [open, from])

  const staying = mode === 'move' ? files.filter((file) => (file.folder_id ?? null) === target) : []
  const going = mode === 'move' ? files.filter((file) => !staying.includes(file)) : files
  const losses = mode === 'move' ? projectLosses(going, folders, target) : []

  function close() {
    if (busy) return
    onClose()
  }

  async function confirm() {
    setBusy(true)
    setError(null)
    try {
      await onConfirm(going, target)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog
      open={open}
      title={one ? `${verb} ${files[0]!.filename} to…` : `${verb} ${files.length} files to…`}
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="primary"
            data-testid={`library-${mode}-confirm`}
            onClick={() => void confirm()}
            disabled={busy || going.length === 0}
            {...USER_ONLY}
          >
            {busy ? <Spinner /> : `${verb} to ${folderLabel(folders, target)}`}
          </Button>
        </>
      }
    >
      {!one && (
        <ul aria-label={`Files to ${mode}`} className="mb-2 list-disc pl-5 text-[13px] break-all text-ink">
          {files.slice(0, LISTED).map((file) => (
            <li key={file.id}>{file.filename}</li>
          ))}
          {files.length > LISTED && <li className="list-none text-muted">and {files.length - LISTED} more</li>}
        </ul>
      )}
      <p className="mb-1 text-[13px] text-muted">
        {mode === 'copy'
          ? 'Pick the folder for the copies. Each copy is a new file; the original stays where it is.'
          : 'Pick the folder to move to. Each file keeps its print history and remembered choices.'}
      </p>
      <div className="max-h-64 overflow-y-auto rounded-[6px] border border-line p-1">
        <LibraryTree
          folders={folders}
          selected={target}
          onSelect={setTarget}
          label="Destination folder"
          testIdPrefix={`library-${mode}-folder`}
        />
      </div>
      {staying.length > 0 && (
        <p className="mt-2 text-[13px] text-muted" data-testid="library-move-staying">
          {going.length === 0
            ? `${one ? 'It is' : 'They are'} in ${folderLabel(folders, target)} already. Pick another folder.`
            : `${names(staying)} ${staying.length === 1 ? 'is' : 'are'} there already and stay${staying.length === 1 ? 's' : ''}.`}
        </p>
      )}
      {losses.map((loss) => (
        <p key={loss.projectId} className="mt-2 text-[13px] text-warn" data-testid="library-move-project-loss">
          The project {loss.name} will lose {names(loss.files)}: a Bambuddy project lists only the files in its own
          folder.
        </p>
      ))}
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
