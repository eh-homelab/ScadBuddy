import { useEffect, useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { ApiError } from '../../api/client'
import type { LibraryEntry } from '../../api/types'
import { Button } from '../ui/Button'
import { Dialog } from '../ui/Dialog'
import { splitExtension } from '../../lib/libraryTransfer'
import { Spinner } from '../ui/Spinner'

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
    <Dialog
      open={file !== null}
      title={file ? `Rename ${file.filename}` : 'Rename'}
      onClose={close}
      footer={
        <>
          <Button variant="ghost" onClick={close} disabled={busy}>
            Cancel
          </Button>
          <Button
            variant="primary"
            data-testid="library-rename-confirm"
            onClick={() => void confirm()}
            disabled={busy || !usable}
            {...USER_ONLY}
          >
            {busy ? <Spinner /> : 'Rename'}
          </Button>
        </>
      }
    >
      <form
        onSubmit={(event) => {
          event.preventDefault()
          void confirm()
        }}
      >
        <label className="block text-[13px] text-ink">
          New name
          <span className="mt-1 flex items-center gap-1">
            <input
              autoFocus
              value={value}
              onChange={(event) => setValue(event.target.value)}
              aria-label="New name"
              maxLength={255 - extension.length}
              className="h-8 min-w-0 flex-1 rounded-[6px] border border-line bg-bg px-2 py-1 text-[13px] text-ink"
            />
            {extension && <span className="shrink-0 text-muted">{extension}</span>}
          </span>
        </label>
      </form>
      <p className="mt-2 text-[12px] text-faint">
        It keeps its id, so its print history and remembered choices stay with it.
      </p>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
