import { api } from '../../api/client'
import type { ItemCost, OrderCosts as Costs } from '../../api/types'
import { formatDuration } from '../../lib/format'
import { formatMoney } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'

/** `20 g · 10m · $0.40`, of what is known. */
function figures(costs: Costs, grams: number | null, seconds: number | null, minor: number | null): string {
  const parts = [
    grams === null ? null : `${grams} g`,
    seconds === null ? null : formatDuration(seconds),
    minor === null ? null : formatMoney(minor, costs.currency, costs.minor_exponent),
  ].filter((p): p is string => p !== null)
  return parts.length ? parts.join(' · ') : '—'
}

function bambuddy(item: ItemCost): string | null {
  const shown = item.attempts.filter((a) => a.bambuddy_cost != null)
  if (!shown.length) return null
  return shown.map((a) => `attempt ${a.attempt}: ${a.bambuddy_cost!.toFixed(2)}`).join(', ')
}

/**
 * #4251 §6.4 — the order's real cost: each item's accepted quote beside what its
 * attempts used, failures included, priced at the quote's own price per gram.
 * Bambuddy's cost is shown beside it and never summed.
 */
export function OrderCosts({ orderId }: { orderId: string }) {
  const view = useAsync(() => api.orderCosts(orderId), [orderId], [`order:${orderId}`])
  const costs = view.data
  if (!costs) return null
  const used = costs.items.some((i) => i.attempts.length > 0)
  return (
    <section className="flex flex-col gap-2" aria-label="Cost">
      <h2 className="text-sm font-medium">Cost</h2>
      {!used ? (
        <p className="text-[13px] text-muted-foreground">No attempt has used anything yet.</p>
      ) : (
        <table className="w-full text-[13px]">
          <thead className="text-left text-xs text-muted-foreground">
            <tr>
              <th className="font-normal">Item</th>
              <th className="font-normal">Quoted</th>
              <th className="font-normal">Used</th>
              <th className="font-normal">Failed</th>
            </tr>
          </thead>
          <tbody>
            {costs.items.map((item) => {
              const extra = bambuddy(item)
              return (
                <tr key={item.item_id} aria-label={item.title} className="align-top">
                  <td>
                    {item.title}
                    {extra && <div className="text-xs text-muted-foreground">Bambuddy: {extra}</div>}
                  </td>
                  <td className="sb-num" data-testid="cost-quoted">
                    {figures(costs, item.quoted_grams, item.quoted_seconds, item.quoted_filament_minor)}
                  </td>
                  <td className="sb-num" data-testid="cost-actual">
                    {figures(costs, item.actual_grams, item.actual_seconds, item.actual_filament_minor)}
                  </td>
                  <td className="sb-num" data-testid="cost-failed">
                    {item.failed_attempts === 0 ? '—' : `${item.failed_attempts} failed · ${figures(costs, item.failed_grams, null, item.failed_filament_minor)}`}
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
      {costs.bambuddy_unavailable && <p className="text-xs text-warn">Bambuddy could not be read: its costs are missing.</p>}
    </section>
  )
}
