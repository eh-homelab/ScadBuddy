import type { PricePoint } from '../../../api/filamentPurchasing'
import { filamentLinks } from '../../../lib/filamentLinks'
import { EntityLink } from '../EntityLink'
import type { ShopMoney } from './useMoney'

/** A price point: the pack price, then the price per kg and the day it was seen, linked to its invoice line when an invoice set it (#6869). A dash for none. */
export function PriceCell({ point, money }: { point: PricePoint | null | undefined; money: ShopMoney }) {
  if (!point) return <span className="text-muted-foreground">—</span>
  const day = <time dateTime={point.on_date}>{new Date(`${point.on_date}T00:00`).toLocaleDateString(undefined, { dateStyle: 'medium' })}</time>
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="font-medium tabular-nums">{money.format(point.price, point.currency)}</span>
      <span className="flex gap-2 text-[12px] text-muted-foreground tabular-nums">
        <span>{point.price_per_kg ? `${money.format(point.price_per_kg, point.currency)}/kg` : 'no weight'}</span>
        {point.invoice_id && point.invoice_line_id ? (
          <EntityLink to={filamentLinks.invoiceLine(point.invoice_id, point.invoice_line_id)} title="From this invoice" className="underline">
            {day}
          </EntityLink>
        ) : (
          day
        )}
      </span>
    </div>
  )
}
