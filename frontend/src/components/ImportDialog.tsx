import { useEffect, useState, type FormEvent } from 'react'
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

/**
 * The seconds a 503 asks to wait (#1295): the server's import budget is spent, and it
 * says when a fetch slot frees, in the body and as `Retry-After` (the client copies the
 * header in). Anything else is not a wait.
 */
function retryAfterSeconds(cause: unknown): number | undefined {
  if (!(cause instanceof ApiError) || cause.status !== 503) return undefined
  const seconds = cause.problem['retry_after']
  return typeof seconds === 'number' && seconds > 0 ? seconds : undefined
}

/** #153 — the URL is fetched by the server, so a failure's reason comes back as a problem. */
export function ImportDialog({ open, onClose, onImported }: Props) {
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [importing, setImporting] = useState(false)
  /**
   * #1295 — seconds left before a refused import may be tried again. Never retried on
   * its own: an import is the user's action, so the button only waits.
   */
  const [wait, setWait] = useState(0)
  /** The last import was refused for a spent budget, so the button offers it again. */
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (wait <= 0) return
    const tick = setTimeout(() => setWait((left) => left - 1), 1000)
    return () => clearTimeout(tick)
  }, [wait])

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    // Enter submits the form too: the wait holds it as it holds the button.
    if (!url.trim() || wait > 0) return
    setImporting(true)
    setError(null)
    setBusy(false)
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
      const seconds = retryAfterSeconds(cause)
      setBusy(seconds !== undefined)
      setWait(Math.ceil(seconds ?? 0))
    } finally {
      setImporting(false)
    }
  }

  function reset() {
    setUrl('')
    setName('')
    setError(null)
    setWait(0)
    setBusy(false)
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
            disabled={!url.trim() || importing || wait > 0}
          >
            {importing && <Spinner />}
            {importing ? 'Importing' : busy ? 'Try again' : 'Import'}
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
      {busy && (
        <p role="status" className="mt-1 text-[13px] text-muted">
          {wait > 0 ? `Try again in ${wait} s.` : 'You can try again now.'}
        </p>
      )}
    </Dialog>
  )
}
