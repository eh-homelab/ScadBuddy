import { useState } from 'react'
import { USER_ONLY } from '../../agent/dom'
import { api } from '../../api/client'
import type { DepositSuggestion, Order } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { Button } from '@/components/ui/button'
import { refusalText } from './refusal'

const WHY: Record<Exclude<DepositSuggestion['reason'], 'changed'>, string> = {
  amount: 'matches the balance',
  order_number: 'names this order',
}

/**
 * #4961 — deposits of the Plaid-linked account that may have paid this order. A suggestion
 * records nothing: "Record this deposit as payment" records it once, as a `bank_transfer`
 * payment whose reference is the transaction id; "Not this order" hides it. A recorded deposit
 * whose amount Plaid changed since is shown as a warning, with no action: the shop corrects the
 * payment by hand. The list reloads when a sync suggests a deposit (`order.bank_deposits`).
 */
export function DepositSuggestions({ order, onChanged }: { order: Order; onChanged: () => void }) {
  const listed = useAsync(() => api.orderBankDeposits(order.id), [order.id], [`order:${order.id}`])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const all = listed.data ?? []
  const deposits = all.filter((d) => d.reason !== 'changed')
  const changed = all.filter((d) => d.reason === 'changed')
  const money = (minor: number, currency: string) => formatMoney(minor, currency, order.minor_exponent)
  if (all.length === 0 && !error) return null

  async function act(action: () => Promise<unknown>) {
    setBusy(true)
    setError(null)
    try {
      await action()
      onChanged()
    } catch (err) {
      setError(refusalText(err))
    } finally {
      listed.refresh()
      setBusy(false)
    }
  }

  return (
    <div className="space-y-1">
      <h3 className="text-[13px] font-medium">Suggested bank deposits</h3>
      {changed.map((d) => (
        <p key={d.transaction_id} role="status" className="text-[13px] text-warn">
          The bank changed a deposit recorded as a payment: it now reads {money(d.amount_minor, d.currency)}, recorded as{' '}
          {d.recorded_minor == null ? 'unknown' : money(d.recorded_minor, d.currency)} ({d.posted_on} · {d.description}). Correct the payment by hand.
        </p>
      ))}
      {deposits.length > 0 && (
      <ul className="flex flex-col gap-1 text-[13px]" aria-label="Suggested bank deposits">
        {deposits.map((d) => (
          <li key={d.transaction_id} className="flex flex-wrap items-center justify-between gap-2 rounded-md border px-2 py-1">
            <span>
              <span className="sb-num font-medium">{money(d.amount_minor, d.currency)}</span>
              <span className="text-muted-foreground">
                {' · '}
                {d.posted_on} · {d.description} · {d.reason !== 'changed' && WHY[d.reason]}
              </span>
            </span>
            <span className="flex gap-2">
              <Button size="sm" disabled={busy} onClick={() => void act(() => api.recordOrderBankDeposit(order.id, d.transaction_id))} {...USER_ONLY}>
                Record this deposit as payment
              </Button>
              <Button size="sm" variant="outline" disabled={busy} onClick={() => void act(() => api.dismissOrderBankDeposit(order.id, d.transaction_id))} {...USER_ONLY}>
                Not this order
              </Button>
            </span>
          </li>
        ))}
      </ul>
      )}
      {error && (
        <p role="alert" className="text-[13px] text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
