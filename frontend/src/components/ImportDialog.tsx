import { useEffect, useRef, useState, type FormEvent } from 'react'
import { api, ApiError } from '../api/client'
import type { ModelSummary } from '../api/types'
import { Spinner } from './ui/spinner'
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
  open: boolean
  onClose: () => void
  onImported: (model: ModelSummary) => void
}

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
  const inFlight = useRef(false)

  useEffect(() => {
    if (wait <= 0) return
    const tick = setTimeout(() => setWait((left) => left - 1), 1000)
    return () => clearTimeout(tick)
  }, [wait])

  async function submit(event?: FormEvent) {
    event?.preventDefault()
    // Enter submits the form too: an import in flight and the wait hold it as they
    // hold the button. Each call mints its own Idempotency-Key, so a second Enter
    // would be a second import.
    // `importing` is this render's: two submits in one tick both see it false, so the
    // ref, set at once, holds the second (#2558). The other way round, `finally` clears
    // the ref before a refusal's wait renders, and the in-flight render's `importing`,
    // still true, holds a submit in that gap.
    if (!url.trim() || importing || inFlight.current || wait > 0) return
    inFlight.current = true
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
      inFlight.current = false
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
    <Dialog open={open} onOpenChange={(next) => !next && reset()}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Import from URL</DialogTitle>
          <DialogDescription>
            Paste an https link to a .scad file, such as a GitHub or gist raw link. The server fetches it and reads its
            customizer parameters.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
          <div className="grid gap-2">
            <Label htmlFor="import-url">URL</Label>
            <Input
              id="import-url"
              type="url"
              value={url}
              onChange={(event) => setUrl(event.target.value)}
              placeholder="https://raw.githubusercontent.com/…/model.scad"
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="import-name">Name (optional)</Label>
            <Input
              id="import-name"
              value={name}
              onChange={(event) => setName(event.target.value)}
              placeholder="Taken from the file name"
            />
          </div>
          {/* A form of two fields with no submit button ignores Enter (implicit
              submission, https://html.spec.whatwg.org/multipage/form-control-infrastructure.html#implicit-submission),
              and the Import button sits in the dialog's footer, outside the form (#2114). */}
          <button type="submit" hidden tabIndex={-1} />
        </form>
        {error && (
          <p role="alert" className="text-sm text-destructive">
            {error}
          </p>
        )}
        {busy && (
          <p role="status" className="text-sm text-muted-foreground">
            {wait > 0 ? `Try again in ${wait} s.` : 'You can try again now.'}
          </p>
        )}
        <DialogFooter>
          <Button variant="outline" onClick={reset} disabled={importing}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={!url.trim() || importing || wait > 0}>
            {importing && <Spinner />}
            {importing ? 'Importing' : busy ? 'Try again' : 'Import'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
