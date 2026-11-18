import { CircleAlertIcon, CircleCheckIcon } from 'lucide-react'
import { addDecimals, formatCost } from '../../../lib/decimal'

export interface InvoiceTotalsProps {
  currency: string
  /** The currency's minor-unit digits (`minor_exponent`), never `Intl`'s own. */
  exponent: number
  subtotal: string
  tax: string
  /** How the tax was spread, as the server says (`receipt-wide 7.0%`, `per line`), or `null` for none. */
  taxBasis: string | null
  shipping: string
  /** The lines' totals added, tax in and shipping out. */
  lineSum: string
  /** What the person typed from the receipt. */
  total: string
  /** The server's `total` less the lines and shipping; `reconciles` is its rule on it. */
  difference: string
  reconciles: boolean
}

/**
 * #6266 — an invoice's figures in the order a receipt reads, ending in the difference
 * between the typed total and the lines: settled within the server's tolerance, a warning
 * with the amount otherwise.
 */
export function InvoiceTotals({ currency, exponent, subtotal, tax, taxBasis, shipping, lineSum, total, difference, reconciles }: InvoiceTotalsProps) {
  const money = (amount: string) => formatCost(amount, currency, exponent)
  const rows: [string, string, string?][] = [
    ['Subtotal', money(subtotal)],
    ['Tax', money(tax), taxBasis ?? undefined],
    ['Shipping', money(shipping)],
    ['Lines and shipping', money(addDecimals(lineSum, shipping))],
    ['Total typed', money(total)],
  ]
  return (
    <div className="rounded-md border p-3 text-[13px]">
      <dl className="grid grid-cols-[1fr_auto] gap-x-4 gap-y-1">
        {rows.map(([label, value, note]) => (
          <div key={label} className="contents">
            <dt className="text-muted-foreground">
              {label}
              {note && <span className="ml-1 text-[12px]">({note})</span>}
            </dt>
            <dd className="text-right tabular-nums">{value}</dd>
          </div>
        ))}
        <dt className="border-t pt-1 font-medium">Difference</dt>
        <dd className="border-t pt-1 text-right font-medium tabular-nums">{money(difference)}</dd>
      </dl>
      {reconciles ? (
        <p role="status" className="mt-2 flex items-center gap-1.5 text-[12px] text-muted-foreground">
          <CircleCheckIcon aria-hidden className="size-3.5 text-primary" />
          Reconciles with the lines
        </p>
      ) : (
        <p role="status" className="mt-2 flex items-center gap-1.5 text-[12px] text-warn">
          <CircleAlertIcon aria-hidden className="size-3.5" />
          The total does not match the lines: {money(difference)} apart. Fix a price, the tax or the shipping.
        </p>
      )}
    </div>
  )
}
