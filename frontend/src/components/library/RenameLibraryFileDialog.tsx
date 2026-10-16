import { useEffect, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError } from '../../api/client'
import type { LibraryEntry } from '../../api/types'
import { splitExtension } from '../../lib/libraryTransfer'
import { Spinner } from '../ui/Spinner'
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

interface Props {
  file: LibraryEntry | null
  onClose: () => void
  /** Renames it; a refusal (Bambuddy's, for a name a printer would not take) is thrown and shown. */
  onConfirm: (file: LibraryEntry, filename: string) => Promise<void>
}

/** #2670 — a library file's new name. Only the stem is edited: the extension says what the file is. */
export function RenameLibraryFileDialog({ file, onClose, onConfirm }: Props) {
  const { stem, extension } = splitExtension(file?.filename ?? '')
  const [value, setValue] = useState(stem)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setValue(stem)
    setError(null)
  }, [file?.id, stem])

  const next = `${value.trim()}${extension}`
  const usable = value.trim() !== '' && !/[/\\]/.test(value) && next !== file?.filename

  function close() {
    if (busy) return
    onClose()
  }

  async function confirm() {
    if (!file || !usable) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm(file, next)
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : String(caught))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Dialog open={file !== null} onOpenChange={(open) => !open && close()}>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="break-all leading-snug">{file ? `Rename ${file.filename}` : 'Rename'}</DialogTitle>
          <DialogDescription>It keeps its id, so its print history and remembered choices stay with it.</DialogDescription>
        </DialogHeader>
        <form
          id="library-rename-form"
          className="grid gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            void confirm()
          }}
        >
          <Label htmlFor="library-rename-input">New name</Label>
          <span className="flex items-center gap-2">
            <Input
              id="library-rename-input"
              autoFocus
              value={value}
              onChange={(event) => setValue(event.target.value)}
              maxLength={255 - extension.length}
            />
            {extension && <span className="shrink-0 text-sm text-muted-foreground">{extension}</span>}
          </span>
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
            form="library-rename-form"
            data-testid="library-rename-confirm"
            disabled={busy || !usable}
            {...USER_ONLY}
          >
            {busy ? <Spinner /> : 'Rename'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
