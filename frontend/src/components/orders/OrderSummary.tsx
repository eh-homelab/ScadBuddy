import type { OrderCharge, OrderTotals } from '../../api/types'
import { formatMoney } from '../../lib/money'

const KIND_TEXT: Record<OrderCharge['kind'], string> = { shipping: 'Shipping', discount: 'Discount', fee: 'Fee', tax: 'Tax' }

/**
 * #4955 step 4, spec 2026-11-07 §4.1 — the order's summary: the items' subtotal (and the part
 * of it not yet accepted), each charge on its own line in the order it was added (a discount
 * is negative), the total, what was paid and the balance (`orders/totals.py`).
 */
export function OrderSummary({ currency, exponent, totals, charges }: { currency: string; exponent: number; totals: OrderTotals; charges: OrderCharge[] }) {
  const money = (minor: number) => formatMoney(minor, currency, exponent)
  const pending = totals.pending_minor ?? 0
  return (
    <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[13px]" data-testid="order-totals">
      <dt className="text-muted-foreground">Subtotal</dt>
      <dd className="sb-num text-right">{money(totals.subtotal_minor)}</dd>
      {pending !== 0 && (
        <>
          <dt className="pl-3 text-xs text-muted-foreground">of which not accepted</dt>
          <dd className="sb-num text-right text-xs text-muted-foreground">{money(pending)}</dd>
        </>
      )}
      {[...charges]
        .sort((a, b) => a.position - b.position)
        .map((c) => (
          <div key={c.id} className="contents">
            <dt className="text-muted-foreground">{`${KIND_TEXT[c.kind]} · ${c.label}`}</dt>
            <dd className="sb-num text-right">{money(c.amount_minor)}</dd>
          </div>
        ))}
      <dt className="font-medium">Total</dt>
      <dd className="sb-num text-right font-medium">{money(totals.total_minor)}</dd>
      <dt className="text-muted-foreground">Paid</dt>
      <dd className="sb-num text-right">{money(totals.paid_minor)}</dd>
      <dt className="text-muted-foreground">{pending !== 0 ? 'Balance due (provisional)' : 'Balance due'}</dt>
      <dd className="sb-num text-right">{money(totals.balance_minor)}</dd>
    </dl>
  )
}
