import type { ReactNode } from 'react'
import type { OrderCharge, OrderTotals } from '../../api/types'
import { formatMoney } from '../../lib/money'

const KIND_TEXT: Record<OrderCharge['kind'], string> = { shipping: 'Shipping', discount: 'Discount', fee: 'Fee', tax: 'Tax', purchase: 'Bought for the customer', handling: 'Handling' }

/** One line of the summary: its label, an optional badge beside it, and its amount. */
function SummaryRow({ label, badge, amount, className = 'text-muted-foreground', amountClassName = '' }: { label: string; badge?: ReactNode; amount: string; className?: string; amountClassName?: string }) {
  return (
    <div className="contents">
      <dt className={className}>
        <span>{label}</span>
        {badge != null && <span className="ml-1 inline-flex align-middle">{badge}</span>}
      </dt>
      <dd className={`sb-num text-right ${amountClassName}`.trim()}>{amount}</dd>
    </div>
  )
}

/**
 * #4955 step 4, spec 2026-11-07 §4.1 — the order's summary: the items' subtotal (and the part
 * of it not yet accepted), each charge on its own line in the order it was added (a discount
 * is negative), the total, what was paid and the balance (`orders/totals.py`). `chargeBadge`
 * puts a small mark beside a charge's label (such as a computed tax's). #5281: the auto tax
 * row comes last among the charges, as the backend lists them; what of the deposit is still
 * due has its own row, and a note says when the unpaid balance holds the shipment.
 */
export function OrderSummary({
  currency,
  exponent,
  totals,
  charges,
  chargeBadge,
}: {
  currency: string
  exponent: number
  totals: OrderTotals
  charges: OrderCharge[]
  chargeBadge?: (charge: OrderCharge) => ReactNode
}) {
  const money = (minor: number) => formatMoney(minor, currency, exponent)
  const pending = totals.pending_minor ?? 0
  const depositDue = totals.deposit_due_minor ?? 0
  return (
    <div className="flex flex-col gap-1" data-testid="order-totals">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[13px]">
        <SummaryRow label="Subtotal" amount={money(totals.subtotal_minor)} />
        {pending !== 0 && <SummaryRow label="of which not accepted" amount={money(pending)} className="pl-3 text-xs text-muted-foreground" amountClassName="text-xs text-muted-foreground" />}
        {[...charges]
          .sort((a, b) => Number(a.auto) - Number(b.auto) || a.position - b.position)
          .map((c) => (
            <SummaryRow key={c.id} label={`${KIND_TEXT[c.kind]} · ${c.label}`} badge={chargeBadge?.(c)} amount={money(c.amount_minor)} />
          ))}
        <SummaryRow label="Total" amount={money(totals.total_minor)} className="font-medium" amountClassName="font-medium" />
        <SummaryRow label="Paid" amount={money(totals.paid_minor)} />
        <SummaryRow label={pending !== 0 ? 'Balance due (provisional)' : 'Balance due'} amount={money(totals.balance_minor)} />
        {depositDue > 0 && <SummaryRow label="Deposit due" amount={money(depositDue)} className="text-warn" amountClassName="text-warn" />}
      </dl>
      {totals.shipping_held && <p className="text-xs text-warn">Shipping held until the balance is paid</p>}
    </div>
  )
}
