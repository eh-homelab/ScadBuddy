import { useEffect, useId, useRef, useState } from 'react'
import { api, newRequestId } from '../../api/client'
import type { CostEntry, TimeEntry } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { toMinor } from '../../lib/money'
import { refusalText } from './refusal'

export const TIME_KINDS: Record<TimeEntry['kind'], string> = {
  design: 'Design',
  setup: 'Setup',
  post_processing: 'Post-processing',
  packing: 'Packing',
  other: 'Other',
}

export const COST_KINDS: Record<CostEntry['kind'], string> = {
  material: 'Materials',
  payment_fee: 'Payment fee',
  assistant: 'Assistant',
  other: 'Other',
}

/** #5326 §4.3 — the running timer lives in this browser only, so an abandoned one costs nothing. */
export const timerKey = (scope: string) => `scadbuddy.order-timer.${scope}`

/** §4.3 — past this, the page offers to log or discard a timer someone may have left running. */
export const STALE_TIMER_MS = 12 * 60 * 60_000

export function readTimer(scope: string): string | null {
  try {
    return window.localStorage.getItem(timerKey(scope))
  } catch {
    return null
  }
}

function writeTimer(scope: string, startedAt: string | null): void {
  try {
    if (startedAt === null) window.localStorage.removeItem(timerKey(scope))
    else window.localStorage.setItem(timerKey(scope), startedAt)
  } catch {
    // Storage refused: the timer lasts as long as the page.
  }
}

/** Whole minutes since `startedAt`, rounded up, at least one. */
export function elapsedMinutes(startedAt: string, at: number = Date.now()): number {
  return Math.max(1, Math.ceil((at - new Date(startedAt).getTime()) / 60_000))
}

/** Whether a timer started at `startedAt` has run past `STALE_TIMER_MS`. */
export function isStale(startedAt: string, at: number = Date.now()): boolean {
  return at - new Date(startedAt).getTime() > STALE_TIMER_MS
}

/**
 * A form's submission key, `manual:<uuid>`: the same for a resubmit of the same fields, so
 * a retry after a lost reply writes once; new once a field changes or the entry is saved.
 */
function useSubmission() {
  const last = useRef<{ fields: string; key: string } | null>(null)
  return {
    source(...fields: unknown[]): string {
      const text = JSON.stringify(fields)
      // getRandomValues, not randomUUID: the Bambuddy iframe may be served without a secure context.
      if (last.current?.fields !== text) last.current = { fields: text, key: `manual:${newRequestId()}` }
      return last.current.key
    },
    done() {
      last.current = null
    },
  }
}

type Scope = { orderId: string; itemId: string | null; onDone: () => void }

/**
 * #5326 §4.3 — "Log time" on an item or the order: minutes typed, or a timer. Start writes
 * nothing; Stop writes one entry with the elapsed minutes, rounded up to the minute.
 */
export function LogTime({ orderId, itemId, onDone }: Scope) {
  const id = useId()
  const scope = itemId ?? `order-${orderId}`
  const [kind, setKind] = useState<TimeEntry['kind']>('setup')
  const [minutes, setMinutes] = useState('')
  const [note, setNote] = useState('')
  const [started, setStarted] = useState<string | null>(() => readTimer(scope))
  const [, tick] = useState(0)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!started) return
    const timer = window.setInterval(() => tick((n) => n + 1), 30_000)
    return () => window.clearInterval(timer)
  }, [started])

  const submission = useSubmission()

  async function save(count: number, startedAt: string | null, source: string): Promise<boolean> {
    setBusy(true)
    setError(null)
    try {
      // One entry per order and source, whatever its kind (the index), so a retry after a
      // lost reply writes once even with another kind picked by then.
      await api.addTimeEntry(orderId, {
        order_item_id: itemId,
        kind,
        minutes: count,
        note: note.trim(),
        started_at: startedAt,
        source,
      })
      submission.done()
      setMinutes('')
      setNote('')
      onDone()
      return true
    } catch (e) {
      // A failed Stop keeps the timer: say so, or the time may be logged twice by hand.
      setError(startedAt ? `${refusalText(e)} The timer is still running; stop it again to log it.` : refusalText(e))
      return false
    } finally {
      setBusy(false)
    }
  }

  function stop() {
    if (!started) return
    const startedAt = started
    void save(elapsedMinutes(startedAt), startedAt, `timer:${scope}:${startedAt}`).then((saved) => {
      if (!saved) return
      writeTimer(scope, null)
      setStarted(null)
    })
  }

  const stale = started !== null && isStale(started)

  return (
    <form
      className="flex flex-wrap items-end gap-2"
      aria-label={itemId ? 'Log time on this item' : 'Log time on the order'}
      onSubmit={(event) => {
        event.preventDefault()
        const count = Number(minutes)
        if (!Number.isInteger(count) || count < 1) {
          setError('Enter whole minutes, such as 15.')
          return
        }
        void save(count, null, submission.source(itemId, kind, count, note.trim()))
      }}
    >
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-kind`}>Time on</Label>
        <Select value={kind} onValueChange={(value) => setKind(value as TimeEntry['kind'])}>
          <SelectTrigger id={`${id}-kind`} aria-label="Time on" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {Object.entries(TIME_KINDS).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-minutes`}>Minutes</Label>
        <Input id={`${id}-minutes`} inputMode="numeric" className="w-20" value={minutes} onChange={(e) => setMinutes(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-note`}>Note</Label>
        <Input id={`${id}-note`} className="w-40" value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <Button type="submit" size="sm" variant="outline" disabled={busy}>
        Log time
      </Button>
      {started ? (
        <>
          <Button type="button" size="sm" disabled={busy} onClick={stop}>
            Stop timer ({elapsedMinutes(started)} min)
          </Button>
          <span className="self-center text-xs text-muted-foreground">on this browser only</span>
        </>
      ) : (
        <Button
          type="button"
          size="sm"
          variant="ghost"
          disabled={busy}
          onClick={() => {
            const at = new Date().toISOString()
            writeTimer(scope, at)
            setStarted(at)
          }}
        >
          Start timer
        </Button>
      )}
      {stale && started && (
        <div role="status" className="flex basis-full flex-wrap items-center gap-2 text-xs">
          <span>
            This timer has run since {new Date(started).toLocaleString()}. Log the {elapsedMinutes(started)} minutes, or discard it?
          </span>
          <Button type="button" size="sm" variant="outline" disabled={busy} onClick={stop}>
            Log {elapsedMinutes(started)} min
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            disabled={busy}
            onClick={() => {
              writeTimer(scope, null)
              setStarted(null)
            }}
          >
            Discard timer
          </Button>
        </div>
      )}
      {error && (
        <p role="alert" className="basis-full text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  )
}

/** #5326 §4.4 — "Add cost" by hand: materials, a fee, anything else. */
export function AddCost({ orderId, itemId, exponent, currency, onDone }: Scope & { exponent: number; currency: string }) {
  const id = useId()
  const [kind, setKind] = useState<CostEntry['kind']>('material')
  const [amount, setAmount] = useState('')
  const [note, setNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const submission = useSubmission()
  return (
    <form
      className="flex flex-wrap items-end gap-2"
      aria-label={itemId ? 'Add a cost to this item' : 'Add a cost to the order'}
      onSubmit={(event) => {
        event.preventDefault()
        const minor = toMinor(amount, exponent)
        if (minor === null || minor < 1) {
          setError('Enter the amount spent, such as 2.50.')
          return
        }
        const source = submission.source(itemId, kind, minor, note.trim())
        setBusy(true)
        setError(null)
        api
          .addCostEntry(orderId, { order_item_id: itemId, kind, amount_minor: minor, note: note.trim(), source })
          .then(() => {
            submission.done()
            setAmount('')
            setNote('')
            onDone()
          })
          .catch((e: unknown) => setError(refusalText(e)))
          .finally(() => setBusy(false))
      }}
    >
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-kind`}>Cost of</Label>
        <Select value={kind} onValueChange={(value) => setKind(value as CostEntry['kind'])}>
          <SelectTrigger id={`${id}-kind`} aria-label="Cost of" className="w-40">
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            {Object.entries(COST_KINDS).map(([value, label]) => (
              <SelectItem key={value} value={value}>
                {label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-amount`}>Amount ({currency})</Label>
        <Input id={`${id}-amount`} inputMode="decimal" className="w-24" value={amount} onChange={(e) => setAmount(e.target.value)} />
      </div>
      <div className="flex flex-col gap-1">
        <Label htmlFor={`${id}-note`}>Note</Label>
        <Input id={`${id}-note`} className="w-40" value={note} onChange={(e) => setNote(e.target.value)} />
      </div>
      <Button type="submit" size="sm" variant="outline" disabled={busy}>
        Add cost
      </Button>
      {error && (
        <p role="alert" className="basis-full text-xs text-destructive">
          {error}
        </p>
      )}
    </form>
  )
}
