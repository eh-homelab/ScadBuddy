import { Link } from 'react-router'
import type { PriceSheetRow } from '../../../api/filamentPurchasing'
import type { SpoolType } from '../../../api/filament'
import { TD, TH } from '../formClasses'
import { RgbaSwatch } from '../RgbaSwatch'
import { PriceCell } from './PriceCell'
import { changeLabel } from './priceFormat'
import type { ShopMoney } from './useMoney'


/** A rise in price is a cost going up (warn); a fall is good news (ok); no change is neutral. */
function changeTone(pct: string): string {
  if (pct.startsWith('-')) return 'text-ok'
  return /^[0.]+$/.test(pct) ? 'text-muted-foreground' : 'text-warn'
}

export function ChangeChip({ pct }: { pct: string | null }) {
  if (pct === null) return <span className="text-muted-foreground" aria-label="No change to show">—</span>
  return <span className={`rounded-full border px-2 py-0.5 text-[12px] font-medium tabular-nums ${changeTone(pct)}`}>{changeLabel(pct)}</span>
}

/**
 * #6266 — a supplier's price sheet: each product's newest price per pack, the one before it,
 * and how the price per kg moved between them. A product with one point has no change.
 */
export function PriceSheet({ rows, types, money }: { rows: PriceSheetRow[]; types: Map<string, SpoolType>; money: ShopMoney }) {
  if (rows.length === 0) return <p className="text-[13px] text-muted-foreground">No products yet. Add one above, then its prices.</p>
  return (
    <div className="relative overflow-x-auto rounded-md border">
      <table aria-label="Price sheet" className="w-full text-left text-[13px]">
        <thead className="bg-muted/50 text-muted-foreground">
          <tr>
            <th scope="col" className={TH}>Type</th>
            <th scope="col" className={TH}>SKU</th>
            <th scope="col" className={`${TH} text-right`}>Newest</th>
            <th scope="col" className={`${TH} text-right`}>Previous</th>
            <th scope="col" className={`${TH} text-right`}>Change per kg</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const type = types.get(row.product.type_id)
            return (
              <tr key={row.product.id} data-product={row.product.id} className="border-t align-top">
                <td className={TD}>
                  <Link to={`/filament/types/${row.product.type_id}`} className="inline-flex items-center gap-2 underline-offset-4 hover:underline">
                    {type && <RgbaSwatch rgba={type.rgba ?? '#FFFFFFFF'} size="sm" />}
                    <span className="font-medium">{row.type_code}</span>
                  </Link>
                </td>
                <td className={`${TD} font-mono text-[12px]`}>{row.product.sku}</td>
                <td className={`${TD} text-right`}>
                  <PriceCell point={row.newest} money={money} />
                </td>
                <td className={`${TD} text-right`}>
                  <PriceCell point={row.previous} money={money} />
                </td>
                <td className={`${TD} text-right`}>
                  <ChangeChip pct={row.change_pct} />
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
