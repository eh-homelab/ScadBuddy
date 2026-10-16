import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError } from '../../api/client'
import type { LibraryEntry } from '../../api/types'
import { Spinner } from '../ui/Spinner'
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
    <AlertDialog open={files.length > 0} onOpenChange={(open) => !open && close()}>
      <AlertDialogContent className="data-[size=default]:sm:max-w-md">
        <AlertDialogHeader>
          <AlertDialogTitle className="break-all">
            {one ? `Delete ${files[0]!.filename}?` : `Delete ${files.length} files?`}
          </AlertDialogTitle>
          <AlertDialogDescription>
            {one ? 'It goes' : 'They go'} to Bambuddy&apos;s trash. You can undo this straight after, or restore{' '}
            {one ? 'it' : 'them'} from Bambuddy&apos;s trash later.
          </AlertDialogDescription>
        </AlertDialogHeader>
        {!one && (
          <ul aria-label="Files to delete" className="list-disc pl-5 text-sm break-all">
            {files.slice(0, LISTED).map((file) => (
              <li key={file.id}>{file.filename}</li>
            ))}
            {files.length > LISTED && <li className="list-none text-muted-foreground">and {files.length - LISTED} more</li>}
          </ul>
        )}
        {external.length > 0 && (
          <p className="text-sm text-destructive" data-testid="library-delete-external">
            {one
              ? 'This file is linked from an external folder. Bambuddy removes it for good, and it cannot be restored.'
              : `${external.map((file) => file.filename).join(', ')} ${external.length === 1 ? 'is' : 'are'} linked from an external folder. Bambuddy removes ${external.length === 1 ? 'it' : 'them'} for good, and ${external.length === 1 ? 'it' : 'they'} cannot be restored.`}
          </p>
        )}
        {outputs.length > 0 && (
          <p className="text-xs text-muted-foreground">
            ScadBuddy keeps the {outputs.length === 1 ? 'output this file was made from' : 'outputs these files were made from'}{' '}
            and uploads a new copy the next time it is printed.
          </p>
        )}
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={deleting}>Cancel</AlertDialogCancel>
          <Button
            variant="destructive"
            data-testid="library-delete-confirm"
            onClick={() => void confirm()}
            disabled={deleting}
            {...USER_ONLY}
          >
            {deleting ? <Spinner /> : one ? 'Delete file' : `Delete ${files.length} files`}
          </Button>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}
