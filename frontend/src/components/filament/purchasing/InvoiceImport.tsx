import { SparklesIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useAssistantOpener } from '../../../agent/chat/opener'
import { api, ApiError } from '../../../api/client'
import { runningImportId, startInvoiceImport, type InvoiceDetail } from '../../../api/filamentPurchasing'
import { Button } from '@/components/ui/button'

const POLL_MS = 15_000
const START_FAILED = 'The import could not start. Try again.'

type Phase = 'working' | 'waiting'
type Problem = { text: string; retry: boolean }

/** What the person reads for a refused start. A forwarded backend answer (it has a `type`) is not written for people. */
function explain(cause: unknown): Problem {
  if (!(cause instanceof ApiError)) return { text: START_FAILED, retry: true }
  const { type } = cause.problem as { type?: string }
  switch (cause.status) {
    case 404:
      return { text: 'This invoice no longer exists.', retry: false }
    case 403:
      return { text: 'You cannot import invoices from here.', retry: false }
    case 409:
      return { text: START_FAILED, retry: true }
    case 429:
    case 503:
      // The agent's own reason (a rate limit, a start that is not ready): it is written for people.
      if (type === 'session_refused') return { text: cause.detail, retry: true }
      if (cause.status === 429) return { text: START_FAILED, retry: true }
      return { text: 'The assistant is unavailable right now. Try again later.', retry: true }
    case 422:
      return type === undefined || type === 'unreadable_file'
        ? { text: cause.detail, retry: false }
        : { text: 'The invoice could not be read, so it cannot be imported.', retry: false }
    // A 502 may name a session, but the agent archived it and ran no turn: there is nothing to open.
    default:
      return { text: START_FAILED, retry: true }
  }
}

/**
 * #6267 — "Import with the assistant" on a draft that has its file: it starts an assistant session
 * that reads the file and fills the draft, and opens that session in the assistant panel
 * (`useAssistantOpener`). While the invoice's `import_run_id` names a session that still takes
 * messages, the page does not offer a second import. A session with a turn under way reads "Import
 * running" (pulsing); an idle one, or one that waits for an answer or an approval, reads "The
 * assistant is waiting for you". Both offer "Open". The session is read again on window focus and
 * every 15 s while one is linked, so a session that ends during a visit brings the button back.
 * A 409 `import_running` names the running session and opens it; a 409 `confirmed` removes the
 * button (the invoice is reloaded). A refusal reads as a plain sentence. Without the assistant (AI off) there
 * is nothing to open, so nothing shows.
 */
export function InvoiceImport({ invoice, onChanged }: { invoice: Pick<InvoiceDetail, 'id' | 'status' | 'file' | 'import_run_id'>; onChanged: () => void }) {
  const opener = useAssistantOpener()
  const [session, setSession] = useState<{ id: string; phase: Phase } | null>(null)
  const [ended, setEnded] = useState<string | null>(null)
  const [checked, setChecked] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<Problem | null>(null)
  const [closed, setClosed] = useState(false)
  // A second press in the same tick, before `busy` has rendered, must not send a second request.
  const pressed = useRef(false)
  const linked = invoice.import_run_id
  // The session to watch: the one just started or opened, else the one the invoice names, until it ends.
  const target = session?.id ?? linked
  const watched = target !== null && target !== ended ? target : null

  useEffect(() => {
    // No assistant (AI off): there is no session to read, so nothing polls.
    if (opener === null || watched === null) return
    const id = watched
    let live = true
    let sent = 0
    let applied = 0
    // An answer older than one already applied is dropped, so a slow read never overwrites a newer one.
    function apply(mine: number, next: { id: string; phase: Phase } | null) {
      if (!live || mine < applied) return
      applied = mine
      setSession(next)
      setEnded(next === null ? id : null)
      setChecked(id)
    }
    function read() {
      const mine = ++sent
      api
        .getAiSession(id)
        .then((found) => {
          const over = found.archived === true || found.status === 'done' || found.status === 'failed'
          apply(mine, over ? null : { id, phase: found.status === 'running' ? 'working' : 'waiting' })
        })
        .catch((cause) => {
          // Only a 404 means the session is gone. Any other failure keeps what shows, and the next tick or focus reads again.
          if (cause instanceof ApiError && cause.status === 404) apply(mine, null)
          else console.warn('invoice import: could not read the session', cause)
        })
    }
    read()
    const timer = window.setInterval(read, POLL_MS)
    window.addEventListener('focus', read)
    return () => {
      live = false
      window.clearInterval(timer)
      window.removeEventListener('focus', read)
    }
  }, [watched, opener])

  if (!opener || closed || invoice.status !== 'draft' || !invoice.file) return null
  if (linked !== null && session === null && ended !== linked && checked !== linked) return null

  function open(sessionId: string) {
    setSession({ id: sessionId, phase: 'working' })
    setEnded(null)
    opener?.openSession(sessionId)
  }

  async function start() {
    if (pressed.current) return
    pressed.current = true
    setBusy(true)
    setError(null)
    try {
      const { session_id } = await startInvoiceImport(invoice.id)
      open(session_id)
      onChanged()
    } catch (cause) {
      const running = runningImportId(cause)
      if (running) {
        open(running)
        onChanged()
      } else if (cause instanceof ApiError && cause.status === 409 && (cause.problem as { type?: string }).type === 'confirmed') {
        setClosed(true)
        onChanged()
      } else {
        setError(explain(cause))
      }
    } finally {
      pressed.current = false
      setBusy(false)
    }
  }

  if (session !== null) {
    const working = session.phase === 'working'
    return (
      <div className="flex items-center gap-2 text-[13px]">
        {working && <span aria-hidden className="size-2 rounded-full bg-primary motion-safe:animate-pulse" />}
        <span>{working ? 'Import running' : 'The assistant is waiting for you'}</span>
        <Button type="button" variant="outline" size="sm" onClick={() => opener.openSession(session.id)}>
          Open
        </Button>
      </div>
    )
  }

  return (
    <>
      <Button type="button" variant="outline" size="sm" disabled={busy} onClick={() => void start()}>
        <SparklesIcon aria-hidden />
        {busy ? 'Starting the import…' : error?.retry ? 'Retry' : 'Import with the assistant'}
      </Button>
      {error && (
        <p role="alert" className="basis-full text-[13px] text-warn">
          {error.text}
        </p>
      )}
    </>
  )
}
