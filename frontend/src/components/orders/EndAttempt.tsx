import { useState } from 'react'
import { api } from '../../api/client'
import type { Fulfilment, FulfilmentInput } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { OUTCOMES } from './outcomes'
import { refusalText } from './refusal'


/** Whether `attempt` is still open: not accepted, no outcome and no end recorded. A claim
 * with no run is open too: after a lost reply or a timed-out start it blocks every later
 * print until a person ends it. While its print operation still runs, the backend refuses
 * the end as `print_starting`, and the form shows that refusal. */
function attemptOpen(attempt: Fulfilment): boolean {
  return !attempt.accepted_at && attempt.outcome === 'unknown' && attempt.finished_at === null
}

/** `date` as a `datetime-local` value, in the browser's time zone. */
function localInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

/**
 * #5316 — ends an open attempt by hand: its outcome and its end time. An attempt that never
 * ends blocks every later print of its item (`print_in_flight`, #5315). The PATCH re-reads
 * the attempt whole, so the fields this form does not change are sent as they are. `made`
 * asks how many were made (a production attempt); a test print makes none. Nothing is
 * shown for an attempt that is accepted or already ended.
 */
export function EndAttempt({
  orderId,
  itemId,
  attempt,
  made,
  onChanged,
}: {
  orderId: string
  itemId: string
  attempt: Fulfilment
  made: boolean
  onChanged: () => void
}) {
  const [open, setOpen] = useState(false)
  const [outcome, setOutcome] = useState<FulfilmentInput['outcome']>('succeeded')
  const [count, setCount] = useState('')
  const [reason, setReason] = useState('')
  const [ended, setEnded] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const name = `End attempt ${attempt.attempt}`
  const id = `end-${attempt.id}`

  if (!attemptOpen(attempt)) return null
  /** The form starts over each time it opens, and the end time is the time it opens: the
   * drawer may have been open for hours. */
  function start() {
    setOutcome('succeeded')
    setCount(String(attempt.copies ?? attempt.quantity))
    setReason(attempt.failure_reason ?? '')
    setEnded(localInput(new Date()))
    setError(null)
    setOpen(true)
  }

  if (!open) {
    return (
      <Button size="xs" variant="outline" onClick={start}>
        {name}
      </Button>
    )
  }

  async function save() {
    // Digits only: `Number` reads '' as 0 and takes '1e1' and '0x2' as 10 and 2.
    const quantity = made ? (/^\d+$/.test(count.trim()) ? Number(count.trim()) : NaN) : attempt.quantity
    if (!Number.isSafeInteger(quantity)) {
      setError('Enter how many were made, 0 or more.')
      return
    }
    const finished = new Date(ended)
    if (Number.isNaN(finished.getTime())) {
      setError('Enter when the attempt ended.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.updateFulfilment(orderId, itemId, attempt.id, {
        quantity,
        outcome,
        // The backend takes a failed outcome with no reason; any other outcome keeps the
        // reason the attempt has.
        failure_reason: outcome === 'failed' ? reason.trim() || null : attempt.failure_reason,
        started_at: attempt.started_at,
        finished_at: finished.toISOString(),
        notes: attempt.notes,
      })
      setOpen(false)
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <form
      aria-label={name}
      className="flex w-full flex-wrap items-end gap-2 rounded-md border p-2"
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-outcome`}>Outcome</Label>
        <Select value={outcome} onValueChange={(v) => setOutcome(v as FulfilmentInput['outcome'])}>
          <SelectTrigger id={`${id}-outcome`} aria-label="Outcome" className="w-32">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {OUTCOMES.map((o) => (
              <SelectItem key={o} value={o}>
                {o}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {made && (
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-made`}>Made</Label>
          <Input id={`${id}-made`} inputMode="numeric" className="w-20" value={count} onChange={(e) => setCount(e.target.value)} />
        </div>
      )}
      {outcome === 'failed' && (
        <div className="flex flex-col gap-1">
          <Label htmlFor={`${id}-reason`}>Why it failed (optional)</Label>
          <Input id={`${id}-reason`} className="w-40" value={reason} onChange={(e) => setReason(e.target.value)} />
        </div>
      )}
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-ended`}>Ended at</Label>
        <Input id={`${id}-ended`} type="datetime-local" className="w-fit" value={ended} onChange={(e) => setEnded(e.target.value)} />
      </div>
      <Button type="submit" size="sm" disabled={busy}>
        Save outcome
      </Button>
      <Button type="button" size="sm" variant="ghost" disabled={busy} onClick={() => setOpen(false)}>
        Cancel
      </Button>
      {error && (
        <p role="alert" className="w-full text-[13px] text-warn">
          {error}
        </p>
      )}
    </form>
  )
}
