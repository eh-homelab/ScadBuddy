import type { PricePoint } from '../../../api/filamentPurchasing'
import type { ShopMoney } from './useMoney'

/** A price point: the pack price, then the price per kg and the day it was seen. A dash for none. */
export function PriceCell({ point, money }: { point: PricePoint | null | undefined; money: ShopMoney }) {
  if (!point) return <span className="text-muted-foreground">—</span>
  return (
    <div className="flex flex-col items-end leading-tight">
      <span className="font-medium tabular-nums">{money.format(point.price, point.currency)}</span>
      <span className="flex gap-2 text-[12px] text-muted-foreground tabular-nums">
        <span>{point.price_per_kg ? `${money.format(point.price_per_kg, point.currency)}/kg` : 'no weight'}</span>
        <time dateTime={point.on_date}>{new Date(`${point.on_date}T00:00`).toLocaleDateString(undefined, { dateStyle: 'medium' })}</time>
      </span>
    </div>
  )
}
