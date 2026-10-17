import { useState } from 'react'
import { USER_ONLY } from '../agent/dom'
import { api, ApiError, mayHaveRun } from '../api/client'
import type { PrintAgain, PrintDetail } from '../api/types'
import { openExternal } from '../lib/embed'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Spinner } from './ui/spinner'

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
  /**
   * The answer never said whether it queued (review #1316 (13) 1a): Queue again would
   * send a new key, so a second print. Closing the dialog is the way back to it.
   */
  const [unsure, setUnsure] = useState(false)
  const printer =
    print.outcome.printer_name ??
    print.printer_name ??
    (print.printer_id !== null ? `printer #${print.printer_id}` : 'its printer')

  function close() {
    setSending(false)
    setError(null)
    setResult(null)
    setUnsure(false)
    onClose()
  }

  async function queue() {
    setSending(true)
    setError(null)
    try {
      setResult(await api.reprint(print.archive_id))
    } catch (cause) {
      if (mayHaveRun(cause)) {
        setUnsure(true)
        setError(`${(cause as ApiError).detail} It may have been queued anyway: check Bambuddy's queue before queueing it again.`)
        return
      }
      setError(cause instanceof ApiError ? cause.detail : 'Could not queue the print. Check the connection.')
    } finally {
      setSending(false)
    }
  }

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (!next) close()
      }}
    >
      {/* Bounded by the visible height, the body scrolling, so the buttons stay reachable. */}
      <DialogContent showCloseButton={false} className="flex max-h-[calc(100dvh-2rem)] flex-col gap-0 p-0 sm:max-w-lg">
        <DialogHeader className="shrink-0 border-b px-5 py-4">
          <DialogTitle>Print again</DialogTitle>
          {!result && <DialogDescription>Bambuddy queues the same sliced file again.</DialogDescription>}
        </DialogHeader>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">
          {result ? (
            <p className="text-[13px] text-ink">
              Queued as <span className="sb-num">#{result.queue_item_id}</span>.
            </p>
          ) : (
            <>
              <p className="text-[13px] text-ink">
                Queue <span className="font-medium">{print.output_name ?? print.slug ?? `print #${print.archive_id}`}</span> again on{' '}
                <span className="font-medium">{printer}</span>, with the print options remembered for this
                printer and model, as a print from the print dialog uses.
              </p>
              {error && (
                <p role="alert" className="mt-3 text-[13px] text-warn">
                  {error}
                </p>
              )}
            </>
          )}
        </div>
        <DialogFooter className="m-0 shrink-0 flex-row flex-wrap items-center justify-end px-5 py-3">
          {result ? (
            <>
              <Button type="button" variant="outline" onClick={close}>Done</Button>
              <Button type="button" onClick={() => openExternal(result.bambuddy_url)}>
                Open in queue
              </Button>
            </>
          ) : (
            <>
              <Button type="button" variant="outline" onClick={close} disabled={sending}>
                {unsure ? 'Close' : 'Cancel'}
              </Button>
              {!unsure && (
                <Button type="button" onClick={() => void queue()} disabled={sending} {...USER_ONLY}>
                  {sending && <Spinner />}
                  {sending ? 'Queueing' : 'Queue'}
                </Button>
              )}
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}
