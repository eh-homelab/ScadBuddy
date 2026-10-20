import { useState } from 'react'
import { api } from '../../api/client'
import type { Fulfilment, FulfilmentInput } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { refusalText } from './refusal'

const OUTCOMES: FulfilmentInput['outcome'][] = ['succeeded', 'partial', 'failed', 'unknown']

/**
 * Fulfilment attempts by hand (§6.1, plan Ruling 3): the attempts so far, each accepted
 * once its quantity counts as made, and a form for the next one. `enabled` once the
 * order is accepted and the item open.
 */
export function FulfilmentForm({
  orderId,
  itemId,
  quantity,
  attempts,
  enabled,
  onChanged,
}: {
  orderId: string
  itemId: string
  quantity: number
  attempts: Fulfilment[]
  enabled: boolean
  onChanged: () => void
}) {
  const made = attempts.filter((a) => a.accepted_at).reduce((sum, a) => sum + a.quantity, 0)
  const [count, setCount] = useState(String(Math.max(1, quantity - made)))
  const [outcome, setOutcome] = useState<FulfilmentInput['outcome']>('succeeded')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function run(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="flex flex-col gap-2" aria-label="Fulfilment">
      <h3 className="text-xs text-muted-foreground">
        Fulfilment <span className="sb-num">({made} of {quantity} made)</span>
      </h3>
      {attempts.length > 0 && (
        <ol className="flex flex-col gap-1 text-[13px]" aria-label="Attempts">
          {attempts.map((attempt) => (
            <li key={attempt.id} className="flex flex-wrap items-center gap-2">
              <span>
                Attempt {attempt.attempt}: {attempt.outcome}, <span className="sb-num">{attempt.quantity}</span> made
                {attempt.failure_reason ? ` (${attempt.failure_reason})` : ''}
              </span>
              {attempt.accepted_at ? (
                <span className="text-xs text-primary">Accepted</span>
              ) : (
                enabled &&
                attempt.outcome !== 'failed' && (
                  <Button size="xs" variant="outline" disabled={busy} onClick={() => void run(() => api.acceptFulfilment(orderId, itemId, attempt.id))}>
                    Accept attempt {attempt.attempt}
                  </Button>
                )
              )}
            </li>
          ))}
        </ol>
      )}
      {enabled && (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault()
            const made = Number(count)
            if (!Number.isInteger(made) || made < 0) {
              setError('Enter how many were made, 0 or more.')
              return
            }
            void run(() =>
              api.recordFulfilment(orderId, itemId, {
                quantity: made,
                outcome,
                ...(reason.trim() ? { failure_reason: reason.trim() } : {}),
              }),
            )
          }}
        >
          <div className="flex flex-col gap-1">
            <Label htmlFor={`made-${itemId}`}>Made</Label>
            <Input id={`made-${itemId}`} inputMode="numeric" className="w-20" value={count} onChange={(e) => setCount(e.target.value)} />
          </div>
          <div className="flex flex-col gap-1">
            <Label htmlFor={`outcome-${itemId}`}>Outcome</Label>
            <Select value={outcome} onValueChange={(v) => setOutcome(v as FulfilmentInput['outcome'])}>
              <SelectTrigger id={`outcome-${itemId}`} aria-label="Outcome" className="w-32">
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
          {outcome === 'failed' && (
            <div className="flex flex-col gap-1">
              <Label htmlFor={`reason-${itemId}`}>Why it failed</Label>
              <Input id={`reason-${itemId}`} className="w-40" value={reason} onChange={(e) => setReason(e.target.value)} />
            </div>
          )}
          <Button type="submit" size="sm" disabled={busy}>
            Record attempt
          </Button>
        </form>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </section>
  )
}
