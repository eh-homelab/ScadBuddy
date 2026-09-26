import { useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { ModelSummary } from '../api/types'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  open: boolean
  onClose: () => void
  onImported: (model: ModelSummary) => void
}

const inputClass =
  'h-8 w-full rounded-[6px] border border-line bg-surface-2 px-2 text-[13px] outline-none focus:border-line-strong'

/** #153 — the URL is fetched by the server, so a failure's reason comes back as a problem. */
export function ImportDialog({ open, onClose, onImported }: Props) {
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    if (!url.trim()) return
    setImporting(true)
    setError(null)
    try {
      const model = await api.importModel({
        url: url.trim(),
        name: name.trim() || null,
        force: false,
      })
      onImported(model)
      reset()
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Import failed. Try again.')
    } finally {
      setImporting(false)
    }
  }

  function reset() {
    setUrl('')
    setName('')
    setError(null)
    onClose()
  }

  return (
    <Dialog
      open={open}
      title="Import from URL"
      description="Paste an https link to a .scad file, such as a GitHub or gist raw link. The server fetches it and reads its customizer parameters."
      onClose={reset}
      footer={
        <>
          <Button onClick={reset} disabled={importing}>
            Cancel
          </Button>
          <Button
            variant="primary"
            onClick={() => void submit()}
            disabled={!url.trim() || importing}
          >
            {importing && <Spinner />}
            {importing ? 'Importing' : 'Import'}
          </Button>
        </>
      }
    >
      <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-3">
        <label className="flex flex-col gap-1 text-[13px] text-muted">
          URL
          <input
            type="url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://raw.githubusercontent.com/…/model.scad"
            className={inputClass}
          />
        </label>
        <label className="flex flex-col gap-1 text-[13px] text-muted">
          Name (optional)
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Taken from the file name"
            className={inputClass}
          />
        </label>
      </form>
      {error && (
        <p role="alert" className="mt-3 text-[13px] text-warn">
          {error}
        </p>
      )}
    </Dialog>
  )
}
