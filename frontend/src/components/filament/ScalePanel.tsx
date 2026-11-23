import { useState } from 'react'
import { ApiError } from '../../api/client'
import { getRfidStatus, tareScale, type SpoolBuddyDevice } from '../../api/filament'
import { useAsync } from '../../lib/useAsync'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button } from '@/components/ui/button'

const message = (cause: unknown) => (cause instanceof ApiError ? cause.detail : 'The server could not be reached. Try again.')

const when = (at: string) => new Date(at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })

/** What a device's pending command means for a tare (R-7155-4: a tare replaces it). */
function pendingLine(command: string | null | undefined): string | null {
  if (!command) return null
  if (command === 'tare') return 'A tare is queued. The scale zeroes when it next checks in.'
  if (command === 'write_tag') return 'A tag write is queued. Tare after the write is done or cancelled.'
  return `The command ${command} is queued. Tare after it is done.`
}

/**
 * #7155 — tares a SpoolBuddy scale through Bambuddy. The spool page shows it beside the tag panels,
 * on the page whose timeline shows the scale's readings. "Tare scale" asks first, since a tare with
 * a spool on the scale makes every later reading wrong. Bambuddy queues the tare and the scale zeroes
 * at its next check-in, so the answer says it is queued, not done. A device with another command
 * pending cannot be tared: the tare would replace that command.
 */
export function ScalePanel() {
  const status = useAsync(() => getRfidStatus(), [])
  const scales = (status.data?.devices ?? []).filter((device) => device.has_scale)
  const [asking, setAsking] = useState<SpoolBuddyDevice | null>(null)
  const [busy, setBusy] = useState(false)
  const [queued, setQueued] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  async function tare(device: SpoolBuddyDevice) {
    setBusy(true)
    setError(null)
    setQueued(null)
    try {
      await tareScale(device.device_id)
      setQueued(device.hostname)
      // Read the devices again: the tare is now the device's pending command, and another
      // device may have taken a command meanwhile. The list stays on screen while it reads.
      status.refresh()
    } catch (cause) {
      setError(message(cause))
    } finally {
      setBusy(false)
    }
  }

  if (status.loading) return null
  if (scales.length === 0)
    return <p className="text-[13px] text-muted-foreground">{status.error ? message(status.error) : 'No SpoolBuddy scale found in Bambuddy.'}</p>

  return (
    <div className="flex flex-col gap-2" data-testid="scale-panel">
      <ul className="flex flex-col gap-2">
        {scales.map((device) => {
          const pending = pendingLine(device.pending_command)
          const blocked = Boolean(device.pending_command) && device.pending_command !== 'tare'
          return (
            <li key={device.device_id} className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
              <div className="flex min-w-0 flex-col">
                <span className="text-[13px] font-medium">
                  {device.hostname}
                  {device.online ? '' : <span className="font-normal text-muted-foreground"> (offline)</span>}
                </span>
                {device.last_calibrated_at && (
                  <span className="text-[12px] text-muted-foreground">Last zeroed or calibrated {when(device.last_calibrated_at)}</span>
                )}
                {!device.scale_ok && <span className="text-[12px] text-warn">The scale does not answer. Check its cable.</span>}
                {pending && <span className="text-[12px] text-muted-foreground">{pending}</span>}
              </div>
              <Button
                type="button"
                size="sm"
                variant="outline"
                aria-label={`Tare scale ${device.hostname}`}
                disabled={busy || blocked}
                onClick={() => setAsking(device)}
              >
                Tare scale
              </Button>
            </li>
          )
        })}
      </ul>

      {queued && (
        <p role="status" className="text-[13px]">
          Tare queued on {queued}. The scale zeroes when it next checks in.
        </p>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}

      <AlertDialog open={asking !== null} onOpenChange={(open) => !open && setAsking(null)}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Tare {asking?.hostname}?</AlertDialogTitle>
            <AlertDialogDescription>
              Take everything off the scale first. The scale sets its zero to what it holds when it next checks in with Bambuddy.
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>Cancel</AlertDialogCancel>
            <AlertDialogAction
              onClick={() => {
                if (asking) void tare(asking)
                setAsking(null)
              }}
            >
              Tare scale
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
