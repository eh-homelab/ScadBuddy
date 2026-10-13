import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError } from '../../api/client'
import type { LibraryEntry } from '../../api/types'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { Spinner } from '../ui/Spinner'

/** How many names the dialog lists before "and n more". */
const LISTED = 8

interface Props {
  files: LibraryEntry[]
  onClose: () => void
  /** Deletes them; a refusal is thrown and shown in the dialog. */
  onConfirm: (files: LibraryEntry[]) => Promise<void>
}

/**
 * #2167 — the confirmation before files leave Bambuddy's library: which files, that
 * they go to Bambuddy's trash, and which of them (external ones) cannot come back.
 */
export function DeleteLibraryFilesDialog({ files, onClose, onConfirm }: Props) {
  const [deleting, setDeleting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const external = files.filter((file) => file.is_external)
  const outputs = files.filter((file) => file.output_id)
  const one = files.length === 1

  function close() {
    if (deleting) return
    setError(null)
    onClose()
  }

  async function confirm() {
    setDeleting(true)
    setError(null)
    try {
      await onConfirm(files)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setDeleting(false)
    }
  }

  return (
    <Dialog
      open={files.length > 0}
      title={one ? `Delete ${files[0]!.filename}?` : `Delete ${files.length} files?`}
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={deleting}>
            Cancel
          </Button>
          <Button
            variant="danger"
            data-testid="library-delete-confirm"
            onClick={() => void confirm()}
            disabled={deleting}
            {...USER_ONLY}
          >
            {deleting ? <Spinner /> : one ? 'Delete file' : `Delete ${files.length} files`}
          </Button>
        </>
      }
    >
      {!one && (
        <ul aria-label="Files to delete" className="mb-2 list-disc pl-5 text-[13px] break-all text-ink">
          {files.slice(0, LISTED).map((file) => (
            <li key={file.id}>{file.filename}</li>
          ))}
          {files.length > LISTED && <li className="list-none text-muted">and {files.length - LISTED} more</li>}
        </ul>
      )}
      <p className="text-[13px] text-muted">
        {one ? 'It goes' : 'They go'} to Bambuddy&apos;s trash. You can undo this straight after, or restore{' '}
        {one ? 'it' : 'them'} from Bambuddy&apos;s trash later.
      </p>
      {external.length > 0 && (
        <p className="mt-2 text-[13px] text-warn" data-testid="library-delete-external">
          {one
            ? 'This file is linked from an external folder. Bambuddy removes it for good, and it cannot be restored.'
            : `${external.map((file) => file.filename).join(', ')} ${external.length === 1 ? 'is' : 'are'} linked from an external folder. Bambuddy removes ${external.length === 1 ? 'it' : 'them'} for good, and ${external.length === 1 ? 'it' : 'they'} cannot be restored.`}
        </p>
      )}
      {outputs.length > 0 && (
        <p className="mt-2 text-[12px] text-faint">
          ScadBuddy keeps the {outputs.length === 1 ? 'output this file was made from' : 'outputs these files were made from'}{' '}
          and uploads a new copy the next time it is printed.
        </p>
      )}
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
