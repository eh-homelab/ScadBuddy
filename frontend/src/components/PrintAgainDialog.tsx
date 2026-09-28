import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError } from '../api/client'
import type { PrintAgain, PrintDetail } from '../api/types'
import { openExternal } from '../lib/embed'
import { Button } from './ui/Button'
import { Dialog } from './ui/Dialog'
import { Spinner } from './ui/Spinner'

interface Props {
  open: boolean
  print: PrintDetail
  onClose: () => void
}

/**
 * #311 — "Print again", behind the same confirmation as a send: nothing is queued
 * until Queue is pressed, and only a person can press it (`USER_ONLY`). The backend
 * queues the archive itself (`POST /queue/` with `archive_id`; Bambuddy's reprint route
 * is gone, plan M6) on the printer and plate it printed on.
 */
export function PrintAgainDialog({ open, print, onClose }: Props) {
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<PrintAgain | null>(null)
  const printer =
    print.outcome.printer_name ??
    print.printer_name ??
    (print.printer_id !== null ? `printer #${print.printer_id}` : 'its printer')

  function close() {
    setSending(false)
    setError(null)
    setResult(null)
    onClose()
  }

  async function queue() {
    setSending(true)
    setError(null)
    try {
      setResult(await api.reprint(print.archive_id))
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.detail : 'Could not queue the print. Check the connection.')
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog
      open={open}
      title="Print again"
      description={result ? undefined : 'Bambuddy queues the same sliced file again.'}
      onClose={close}
      footer={
        result ? (
          <>
            <Button onClick={close}>Done</Button>
            <Button variant="primary" onClick={() => openExternal(result.bambuddy_url)}>
              Open in queue
            </Button>
          </>
        ) : (
          <>
            <Button onClick={close} disabled={sending}>
              Cancel
            </Button>
            <Button variant="primary" onClick={() => void queue()} disabled={sending} {...USER_ONLY}>
              {sending && <Spinner />}
              {sending ? 'Queueing' : 'Queue'}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <p className="text-[13px] text-ink">
          Queued as <span className="sb-num">#{result.queue_item_id}</span>.
        </p>
      ) : (
        <>
          <p className="text-[13px] text-ink">
            Queue <span className="font-medium">{print.output_name ?? print.slug}</span> again on{' '}
            <span className="font-medium">{printer}</span>, with Bambuddy&apos;s default print options.
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
