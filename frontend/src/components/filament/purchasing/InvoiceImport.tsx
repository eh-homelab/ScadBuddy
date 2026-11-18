import { SparklesIcon } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useAssistantOpener } from '../../../agent/chat/opener'
import { api, ApiError } from '../../../api/client'
import { runningImportId, startInvoiceImport, type InvoiceDetail } from '../../../api/filamentPurchasing'
import { Button } from '@/components/ui/button'

/**
 * #6267 — "Import with the assistant" on a draft that has its file: it starts an assistant session
 * that reads the file and fills the draft, and opens that session in the assistant panel
 * (`useAssistantOpener`). While the invoice's `import_run_id` names a session that still takes
 * messages, the page says "Import running" and offers "Open" instead, so a second press never
 * starts a second session. A 409 `import_running` opens the session that runs; a 409 `confirmed`
 * removes the button (the invoice is reloaded). A 422 shows the sentence it carries; any other
 * answer is a plain error with Retry. Without the assistant (AI off) there is nothing to open, so
 * nothing shows.
 */
export function InvoiceImport({ invoice, onChanged }: { invoice: Pick<InvoiceDetail, 'id' | 'status' | 'file' | 'import_run_id'>; onChanged: () => void }) {
  const opener = useAssistantOpener()
  const [running, setRunning] = useState<string | null>(null)
  const [checked, setChecked] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<{ text: string; retry: boolean } | null>(null)
  const [closed, setClosed] = useState(false)
  // A second press in the same tick, before `busy` has rendered, must not send a second request.
  const pressed = useRef(false)
  const linked = invoice.import_run_id

  useEffect(() => {
    if (linked === null || linked === running) return
    let live = true
    api
      .getAiSession(linked)
      .then((session) => {
        if (!live) return
        const ended = session.archived === true || session.status === 'done' || session.status === 'failed'
        setRunning(ended ? null : linked)
        setChecked(linked)
      })
      .catch((cause) => {
        if (!live) return
        // A session that is gone (404) is not running. Another failure leaves the button, which the agent refuses with the session's id if one still runs.
        if (!(cause instanceof ApiError)) console.warn('invoice import: could not read the session', cause)
        setRunning(null)
        setChecked(linked)
      })
    return () => {
      live = false
    }
  }, [linked, running])

  if (!opener || closed || invoice.status !== 'draft' || !invoice.file) return null
  if (linked !== null && running === null && checked !== linked) return null

  function open(sessionId: string) {
    setRunning(sessionId)
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
      const session = runningImportId(cause)
      if (session) {
        open(session)
        onChanged()
      } else if (cause instanceof ApiError && cause.status === 409 && (cause.problem as { type?: string }).type === 'confirmed') {
        setClosed(true)
        onChanged()
      } else if (cause instanceof ApiError && cause.status === 422) {
        setError({ text: cause.detail, retry: false })
      } else {
        setError({ text: cause instanceof ApiError && cause.status === 502 ? cause.detail : 'The import could not start. Try again.', retry: true })
      }
    } finally {
      pressed.current = false
      setBusy(false)
    }
  }

  if (running !== null) {
    return (
      <div className="flex items-center gap-2 text-[13px]">
        <span aria-hidden className="size-2 rounded-full bg-primary motion-safe:animate-pulse" />
        <span>Import running</span>
        <Button type="button" variant="outline" size="sm" onClick={() => opener.openSession(running)}>
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
