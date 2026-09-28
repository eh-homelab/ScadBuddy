import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { Output, SendResult } from '../api/types'
import { openExternal } from '../lib/embed'
import { useAsync } from '../lib/useAsync'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  open: boolean
  output: Output | undefined
  onClose: () => void
  onSent: (result: SendResult) => void
}

/**
 * The send bar: upload this output's 3MF to the Bambuddy library, nothing more (#312).
 * Slicing and queueing is the Print dialog's job, so there is no mode, copies box or
 * print options here.
 */
export function SendDialog({ open, output, onClose, onSent }: Props) {
  // Only to tell "no link was configured" apart from "Bambuddy refused the note":
  // the send result reports an absent link the same way for both. Read each time the
  // dialog opens, not once per mount — the dialog outlives every send on the page,
  // and the setting can change between them.
  const settings = useAsync(async () => (open ? await api.getSettings() : null), [open])
  const publicUrl = settings.data?.public_url ?? null
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<SendResult | null>(null)

  function close() {
    setError(null)
    setResult(null)
    setSending(false)
    onClose()
  }

  async function send() {
    if (!output) return
    setSending(true)
    setError(null)
    try {
      const sent = await api.sendOutput(output.id, { mode: 'library' })
      setResult(sent)
      onSent(sent)
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Send failed. Check the connection.')
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog
      open={open}
      title="Send to Bambuddy"
      description={result ? undefined : 'The file is uploaded from ScadBuddy, not your browser.'}
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            {result.bambuddy_url && (
              <Button variant="primary" onClick={() => openExternal(result.bambuddy_url as string)}>
                Open in library
              </Button>
            )}
          </>
        ) : (
          <>
            <Button onClick={close} disabled={sending}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={() => void send()}
              disabled={sending || !output}
              {...USER_ONLY}
            >
              {sending && <Spinner />}
              {sending ? 'Sending' : 'Send'}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <>
          <p className="text-[13px] text-ink">
            Added to the library as <span className="sb-num">{result.filename}</span> (#
            {result.library_file_id}).
          </p>
          {/* The note is best-effort, so say which way it went rather than implying
              the link is on the file when Bambuddy refused it. Silence when no public
              URL is set: there was no link to attach, which is not a failure. */}
          {result.edit_url ? (
            <p className="mt-1.5 text-[12px] text-muted">
              Bambuddy has the link back to these parameters.
            </p>
          ) : publicUrl ? (
            <p className="mt-1.5 text-[12px] text-muted">
              Bambuddy would not take the link back to these parameters.
            </p>
          ) : null}
        </>
      ) : (
        <>
          <p className="text-[13px] text-ink">
            Adds the 3MF to the Bambuddy library, laid out for the printer set in Settings.
          </p>
          <p className="mt-1.5 text-[12px] text-muted">
            To slice and queue it, use Print instead.
          </p>
          {error && (
            <p role="alert" className="mt-3 text-[13px] text-warn">
              {error}
            </p>
          )}
        </>
      )}
    </Dialog>
  )
}
