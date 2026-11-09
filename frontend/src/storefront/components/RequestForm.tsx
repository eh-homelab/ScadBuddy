import { useId, useRef, useState, type FormEvent } from 'react'
import { Link } from 'react-router'
import { ActionKey, StoreError, errorText, store } from '../api/client'
import { useDraftChanged } from '../draftCount'
import { useTurnstileSiteKey } from '../turnstile'
import { Turnstile } from './Turnstile'

/** The edge's caps (`storefront/wire.py`): the page says so before a byte is sent. */
const MAX_FILES = 10
const MAX_FILE_BYTES = 25 * 1024 * 1024
const MAX_TEXT = 5000
const KINDS = /^(image|video)\/|^application\/pdf$/

function filesProblem(files: readonly File[]): string | null {
  if (files.length > MAX_FILES) return `Add at most ${MAX_FILES} files.`
  if (files.some((file) => file.size > MAX_FILE_BYTES)) return 'Each file must be 25 MB or smaller.'
  if (files.some((file) => !KINDS.test(file.type))) return 'Add only photos, videos or PDFs.'
  return null
}

/**
 * "Ask for a design" (spec §5.4): a text and up to ten photos, videos or PDFs, added to the
 * visitor's order as a request. One `Idempotency-Key` per request: sent again after a
 * failure, the same text and files are the same request to the shop.
 */
export function RequestForm() {
  const id = useId()
  const [text, setText] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sent, setSent] = useState(false)
  const key = useRef(new ActionKey())
  const draftChanged = useDraftChanged()
  const problem = filesProblem(files)
  // The bot check (spec §5.8): a widget while it is on, and a new token after each send.
  const siteKey = useTurnstileSiteKey()
  const [token, setToken] = useState<string | null>(null)
  const [widget, setWidget] = useState(0)
  const checking = siteKey !== null
  const needsCheck = checking && token === null

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (problem || needsCheck) return
    if (!text.trim() && files.length === 0) {
      setError('Tell us what to make, or add a photo.')
      return
    }
    const what = JSON.stringify([text, files.map((f) => [f.name, f.size, f.lastModified])])
    setSending(true)
    setError(null)
    try {
      const draft = await store.addRequest(text, files, key.current.for(what), token)
      key.current.done()
      draftChanged(draft)
      setSent(true)
      setText('')
      setFiles([])
    } catch (caught) {
      const wait = caught instanceof StoreError && caught.status === 429 && caught.retryAfter !== null
      setError(wait ? `${errorText(caught)} Try again in ${(caught as StoreError).retryAfter} seconds.` : errorText(caught))
    } finally {
      setSending(false)
      // A token passes one check only.
      if (checking) setWidget((n) => n + 1)
    }
  }

  if (sent) {
    return (
      <div className="sf-panel" role="status">
        <p className="sf-lead">Your request is in your order.</p>
        <p className="sf-actions">
          <Link className="sf-button" to="/draft">
            See your order
          </Link>
          <button type="button" className="sf-link" onClick={() => setSent(false)}>
            Ask for another design
          </button>
        </p>
      </div>
    )
  }

  return (
    <form className="sf-panel sf-form" onSubmit={submit} noValidate>
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-text`}>
          What should we make?
        </label>
        <textarea
          id={`${id}-text`}
          className="sf-input sf-textarea"
          value={text}
          maxLength={MAX_TEXT}
          rows={6}
          placeholder="Sizes, colours, what it is for."
          onChange={(event) => setText(event.target.value)}
        />
      </div>
      <div className="sf-field">
        <label className="sf-label" htmlFor={`${id}-files`}>
          Photos, videos or PDFs
        </label>
        <input
          id={`${id}-files`}
          className="sf-file"
          type="file"
          multiple
          accept="image/*,video/*,application/pdf"
          aria-describedby={`${id}-files-hint`}
          onChange={(event) => setFiles(Array.from(event.target.files ?? []))}
        />
        <p id={`${id}-files-hint`} className={problem ? 'sf-hint sf-hint-bad' : 'sf-hint'}>
          {problem ?? `Up to ${MAX_FILES} files, 25 MB each.`}
        </p>
        {files.length > 0 && (
          <ul className="sf-filelist">
            {files.map((file) => (
              <li key={`${file.name}-${file.size}-${file.lastModified}`}>{file.name}</li>
            ))}
          </ul>
        )}
      </div>
      {siteKey && <Turnstile siteKey={siteKey} action="request" onToken={setToken} resetKey={widget} />}
      {error && (
        <p className="sf-error" role="alert">
          {error}
        </p>
      )}
      <button type="submit" className="sf-button" disabled={sending || problem !== null || needsCheck}>
        {sending ? 'Sending…' : 'Send request'}
      </button>
    </form>
  )
}
