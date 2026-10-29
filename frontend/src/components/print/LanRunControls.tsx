import { useState } from 'react'
import { ApiError, api } from '../../api/client'
import type { LanRunControl } from '../../api/types'
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'
import { Spinner } from '@/components/ui/spinner'
import { cn } from '@/lib/utils'

type Sent = 'stop' | 'clear'

/**
 * #4942, #4939 — the one control surface of a native run's prints still to start: the
 * printer has no queue, so ScadBuddy starts each once the printer has ended the one
 * before. "Stop the rest" stops the ones not started (the print on the printer carries
 * on); while the run waits for a cleared plate (`lan_require_plate_clear`), "Plate is
 * clear" starts the next. The print dialog's panel and the Prints page card show it.
 */
export function LanRunControls({
  control,
  printerName,
  className,
}: {
  control: LanRunControl
  printerName: string | null
  className?: string
}) {
  const printer = printerName ?? 'the printer'
  // Keyed by the state it was sent for, so the next wait offers its buttons again.
  const state = `${control.run_id}:${control.waiting}:${control.awaiting_clear}`
  const [sent, setSent] = useState<{ state: string; what: Sent } | null>(null)
  const [busy, setBusy] = useState<Sent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState(false)
  const done = sent?.state === state ? sent.what : null

  async function send(what: Sent) {
    setBusy(what)
    setError(null)
    try {
      if (what === 'stop') await api.stopPrintRun(control.run_id)
      else await api.confirmPlateClear(control.run_id)
      setSent({ state, what })
      setConfirming(false)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(null)
    }
  }

  const waiting = control.waiting === 1 ? '1 print' : `${control.waiting} prints`
  return (
    <div data-testid="lan-run-controls" className={cn('space-y-1.5 text-[12.5px] text-muted-foreground', className)}>
      <p>
        {control.awaiting_clear
          ? `The printer has finished. Clear the plate of ${printer}, then start the next print. ${waiting} still to start.`
          : `${waiting} still to start, each once the printer has finished the one before.`}
      </p>
      <div className="flex flex-wrap gap-2">
        {control.awaiting_clear && (
          <Button size="sm" disabled={busy !== null || done !== null} onClick={() => void send('clear')}>
            {busy === 'clear' && <Spinner />}
            Plate is clear — start the next print
          </Button>
        )}
        <Button size="sm" variant="outline" disabled={busy !== null || done !== null} onClick={() => setConfirming(true)}>
          Stop the rest
        </Button>
      </div>
      {done === 'stop' && <p>Stopping the prints still to start…</p>}
      {done === 'clear' && <p>Starting the next print…</p>}
      {error && !confirming && (
        <p role="alert" className="text-destructive">
          {error}
        </p>
      )}
      <AlertDialog
        open={confirming}
        onOpenChange={(next) => {
          if (busy) return
          if (!next) setError(null)
          setConfirming(next)
        }}
      >
        <AlertDialogContent size="sm">
          <AlertDialogHeader>
            <AlertDialogTitle>{`Stop the ${waiting} still to start?`}</AlertDialogTitle>
            <AlertDialogDescription>
              {`The print on ${printer} now carries on: stop it on the printer if you need to. A print being started as you stop still starts. The rest are not started.`}
            </AlertDialogDescription>
          </AlertDialogHeader>
          {error && (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={busy !== null}>Keep them</AlertDialogCancel>
            <Button variant="destructive" disabled={busy !== null} onClick={() => void send('stop')}>
              {busy === 'stop' && <Spinner />}
              Stop them
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
