import type { MaterialNumberStats } from '../../api/filament'
import { filamentLinks } from '../../lib/filamentLinks'
import { EntityLink } from './EntityLink'
import { TD, TH } from './formClasses'
import type { ShopMoney } from './purchasing/useMoney'
import { grams } from './spoolsQuery'

const g = (value: number) => `${grams(value)} g`

/**
 * #7152 — the stats by material number (a type's code, case folded): its types, the spools in
 * stock, their grams and value at cost, and the grams the range's plates used, waste among them
 * (R-7152-5 to R-7152-8). Each type links to its page; an archived one says so.
 */
export function MaterialNumberTable({ stats, money }: { stats: MaterialNumberStats; money: ShopMoney }) {
  if (stats.rows.length === 0) return <p className="text-[13px] text-muted-foreground">No spools in stock and no prints in this range.</p>
  return (
    <div className="overflow-x-auto rounded-md border">
      <table aria-label="Stats by material number" className="w-full min-w-[36rem] text-[13px]">
        <thead className="bg-muted/40 text-left text-[12px] text-muted-foreground">
          <tr>
            <th className={TH}>Material number</th>
            <th className={TH}>Types</th>
            <th className={`${TH} text-right`}>Spools</th>
            <th className={`${TH} text-right`}>On hand</th>
            <th className={`${TH} text-right`}>Used</th>
            <th className={`${TH} text-right`}>Value at cost</th>
          </tr>
        </thead>
        <tbody>
          {stats.rows.map((row) => (
            <tr key={row.key ?? ''} data-key={row.key ?? ''} className="border-t align-top">
              <td className={`${TD} font-medium ${row.key == null ? 'text-muted-foreground' : ''}`}>{row.label}</td>
              <td className={TD}>
                {row.types.length === 0 ? (
                  <span className="text-muted-foreground">None</span>
                ) : (
                  <ul className="flex flex-col gap-0.5">
                    {row.types.map((type) => (
                      <li key={type.id}>
                        <EntityLink to={filamentLinks.type(type.id)}>{type.code}</EntityLink>
                        {type.archived && <span className="ml-1.5 text-[12px] text-muted-foreground">archived</span>}
                      </li>
                    ))}
                  </ul>
                )}
              </td>
              <td className={`${TD} text-right tabular-nums`}>{row.spools}</td>
              <td className={`${TD} text-right tabular-nums`}>{g(row.grams_left)}</td>
              <td className={`${TD} text-right tabular-nums`}>
                {g(row.used_g)}
                {row.wasted_g > 0 && <span className="block text-[12px] text-muted-foreground">{g(row.wasted_g)} wasted</span>}
              </td>
              <td className={`${TD} text-right tabular-nums`}>{row.value == null ? <span className="text-muted-foreground">No cost</span> : money.format(row.value, stats.currency)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
