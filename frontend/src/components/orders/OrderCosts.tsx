import { Fragment, useState } from 'react'
import { api } from '../../api/client'
import type { AttemptCost, CostEntry, ItemCost, OrderCosts as Costs, TimeEntry } from '../../api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'
import { formatDuration } from '../../lib/format'
import { formatMoney } from '../../lib/money'
import { AddCost, COST_KINDS, LogTime, TIME_KINDS } from './CostEntryForms'
import { refusalText } from './refusal'

type MarginView = Pick<Costs, 'margin_minor' | 'margin_percent' | 'margin_state' | 'estimated' | 'unknown'>

const PART: Record<string, string> = { prints: 'prints', labour: 'labour' }
const FIGURE: Record<string, string> = { grams: 'grams', seconds: 'time', filament: 'filament price', machine: 'machine rate' }

/** A loss row is red, a thin one amber; an unknown one is never tinted as safe. */
export function marginTint(state: MarginView['margin_state']): string {
  if (state === 'loss') return 'bg-destructive/10 text-destructive'
  if (state === 'thin') return 'bg-warn/10 text-warn'
  return ''
}

/** #5326 §6.1 — the order header's badge, when the order is a loss or thin. */
export function MarginBadge({ state }: { state: MarginView['margin_state'] }) {
  if (state !== 'loss' && state !== 'thin') return null
  return (
    <Badge variant="outline" className={cn('font-normal', state === 'loss' ? 'border-destructive/40 bg-destructive/10 text-destructive' : 'border-warn/40 bg-warn/10 text-warn')}>
      {state === 'loss' ? 'Loss' : 'Thin margin'}
    </Badge>
  )
}

function why(view: MarginView): string | undefined {
  const parts = [
    (view.estimated ?? []).length ? `Estimated: ${(view.estimated ?? []).map((p) => PART[p] ?? p).join(', ')}` : null,
    (view.unknown ?? []).length ? `Unknown: ${(view.unknown ?? []).map((p) => PART[p] ?? p).join(', ')}` : null,
  ].filter(Boolean)
  return parts.length ? parts.join('. ') : undefined
}

function MarginCell({ view, money }: { view: MarginView; money: (minor: number) => string }) {
  if (view.margin_minor == null) return <span className="text-muted-foreground">—</span>
  const approx = (view.estimated ?? []).length > 0
  return (
    <span title={why(view)} data-testid="margin">
      {approx ? '≈ ' : ''}
      {money(view.margin_minor)}
      {view.margin_percent != null ? ` (${view.margin_percent}%)` : ''}
      {view.margin_state === 'unknown' && <span className="ml-1 text-warn">(incomplete)</span>}
    </span>
  )
}

function attemptLine(a: AttemptCost, money: (minor: number) => string): string {
  const figures = [
    a.grams != null ? `${a.grams} g` : null,
    a.seconds != null ? formatDuration(a.seconds) : null,
    a.filament_minor != null ? `filament ${money(a.filament_minor)}` : null,
    a.machine_minor != null ? `machine ${money(a.machine_minor)}` : null,
  ].filter(Boolean)
  const estimated = (a.estimated ?? []).length ? ` · estimated ${(a.estimated ?? []).map((f) => FIGURE[f] ?? f).join(', ')}` : ''
  const unknown = a.unknown ? ' · part unknown' : ''
  return `Attempt ${a.attempt} (${a.purpose ?? 'production'}, ${a.outcome}): ${figures.join(' · ') || 'nothing recorded'}${estimated}${unknown}`
}

function Entries({
  orderId,
  time,
  other,
  money,
  onChanged,
}: {
  orderId: string
  time: TimeEntry[]
  other: CostEntry[]
  money: (minor: number) => string
  onChanged: () => void
}) {
  const [error, setError] = useState<string | null>(null)
  if (!time.length && !other.length) return null
  const remove = (run: () => Promise<unknown>) => {
    setError(null)
    run()
      .then(onChanged)
      .catch((e: unknown) => setError(refusalText(e)))
  }
  return (
    <ul className="flex flex-col gap-0.5" aria-label="Entries">
      {time.map((e) => (
        <li key={e.id} className="flex items-center justify-between gap-2">
          <span>
            {TIME_KINDS[e.kind]}, {e.minutes} min
            {e.rate_minor_per_hour != null ? ` · ${money(Math.round((e.minutes * e.rate_minor_per_hour) / 60))}` : ' · no rate'}
            {e.note ? ` · ${e.note}` : ''}
          </span>
          <Button size="sm" variant="ghost" aria-label={`Remove ${TIME_KINDS[e.kind]} time, ${e.minutes} min`} onClick={() => remove(() => api.removeTimeEntry(orderId, e.id))}>
            Remove
          </Button>
        </li>
      ))}
      {other.map((e) => (
        <li key={e.id} className="flex items-center justify-between gap-2">
          <span>
            {COST_KINDS[e.kind]}, {money(e.amount_minor)}
            {e.note ? ` · ${e.note}` : ''}
          </span>
          <Button size="sm" variant="ghost" aria-label={`Remove ${COST_KINDS[e.kind]} cost, ${money(e.amount_minor)}`} onClick={() => remove(() => api.removeCostEntry(orderId, e.id))}>
            Remove
          </Button>
        </li>
      ))}
      {error && (
        <li role="alert" className="text-xs text-destructive">
          {error}
        </li>
      )}
    </ul>
  )
}

function ItemParts({ costs, item, money, onChanged }: { costs: Costs; item: ItemCost; money: (minor: number) => string; onChanged: () => void }) {
  const bambuddy = item.attempts.filter((a) => a.bambuddy_cost != null)
  return (
    <div className="flex flex-col gap-2 py-2 text-[13px]">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5">
        <dt className="text-muted-foreground">Prints</dt>
        <dd className="sb-num">{money(item.prints_minor ?? 0)}</dd>
        <dt className="text-muted-foreground">Labour</dt>
        <dd className="sb-num">
          {money(item.labour_minor ?? 0)}
          {item.labour_default ? ' (default, no time logged)' : ''}
        </dd>
        <dt className="text-muted-foreground">Other</dt>
        <dd className="sb-num">{money(item.other_minor ?? 0)}</dd>
      </dl>
      {item.attempts.length > 0 && (
        <ul className="flex flex-col gap-0.5" aria-label="Attempts">
          {item.attempts.map((a) => (
            <li key={a.fulfilment_id}>{attemptLine(a, money)}</li>
          ))}
        </ul>
      )}
      {bambuddy.length > 0 && (
        <p className="text-xs text-muted-foreground">Bambuddy (not summed): {bambuddy.map((a) => `attempt ${a.attempt}: ${a.bambuddy_cost!.toFixed(2)}`).join(', ')}</p>
      )}
      <Entries orderId={costs.order_id} time={item.time_entries ?? []} other={item.cost_entries ?? []} money={money} onChanged={onChanged} />
      <LogTime orderId={costs.order_id} itemId={item.item_id} onDone={onChanged} />
      <AddCost orderId={costs.order_id} itemId={item.item_id} currency={costs.currency} exponent={costs.minor_exponent} onDone={onChanged} />
    </div>
  )
}

/**
 * #4251 §6.4, #5326 §6.1 — the order's money view: each item's charged figure, real cost
 * and margin (a loss red, a thin margin amber, `≈` when a part is estimated), each opening
 * to its attempts, time and costs, with the order's total and its own entries.
 */
export function OrderCosts({ costs, onChanged }: { costs: Costs; onChanged: () => void }) {
  const [open, setOpen] = useState<Set<string>>(new Set())
  const money = (minor: number) => formatMoney(minor, costs.currency, costs.minor_exponent)
  const charged = (minor: number | null | undefined) => (minor == null ? '—' : money(minor))
  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  const anything = (costs.cost_minor ?? 0) > 0 || costs.revenue_minor != null || costs.items.some((i) => i.revenue_minor != null || (i.cost_minor ?? 0) > 0)
  return (
    <section className="flex flex-col gap-2" aria-label="Cost">
      <h2 className="text-sm font-medium">Cost and margin</h2>
      {!anything && <p className="text-[13px] text-muted-foreground">No cost or charge yet.</p>}
      <table className="w-full text-[13px]">
        <thead className="text-left text-xs text-muted-foreground">
          <tr>
            <th className="font-normal">Item</th>
            <th className="font-normal">Charged</th>
            <th className="font-normal">Real cost</th>
            <th className="font-normal">Margin</th>
          </tr>
        </thead>
        <tbody>
          {costs.items.map((item) => {
            const expanded = open.has(item.item_id)
            return (
              <Fragment key={item.item_id}>
                <tr aria-label={item.title} className={cn('align-top', marginTint(item.margin_state))} data-state={item.margin_state ?? 'none'}>
                  <td>
                    <Button size="sm" variant="ghost" className="h-auto px-1 py-0 font-normal" aria-expanded={expanded} onClick={() => toggle(item.item_id)}>
                      {expanded ? '▾' : '▸'} {item.title}
                    </Button>
                  </td>
                  <td className="sb-num" data-testid="cost-charged">
                    {charged(item.revenue_minor)}
                  </td>
                  <td className="sb-num" data-testid="cost-real">
                    {(item.estimated ?? []).length ? '≈ ' : ''}
                    {money(item.cost_minor ?? 0)}
                  </td>
                  <td className="sb-num">
                    <MarginCell view={item} money={money} />
                  </td>
                </tr>
                {expanded && (
                  <tr>
                    <td colSpan={4}>
                      <ItemParts costs={costs} item={item} money={money} onChanged={onChanged} />
                    </td>
                  </tr>
                )}
              </Fragment>
            )
          })}
          <tr aria-label="Order total" className={cn('border-t font-medium', marginTint(costs.margin_state))} data-state={costs.margin_state ?? 'none'}>
            <td>Order</td>
            <td className="sb-num">{charged(costs.revenue_minor)}</td>
            <td className="sb-num">
              {(costs.estimated ?? []).length ? '≈ ' : ''}
              {money(costs.cost_minor ?? 0)}
            </td>
            <td className="sb-num">
              <MarginCell view={costs} money={money} />
            </td>
          </tr>
        </tbody>
      </table>
      {costs.revenue_minor == null && costs.items.length > 0 && <p className="text-xs text-muted-foreground">The order's margin shows once every item has an accepted quote.</p>}
      <div className="flex flex-col gap-2 text-[13px]" aria-label="Order costs">
        <p className="text-xs text-muted-foreground">
          On the order: shipping {money(costs.shipping_minor ?? 0)}, charges other than tax {money(costs.charges_minor ?? 0)}.
        </p>
        <Entries orderId={costs.order_id} time={costs.time_entries ?? []} other={costs.cost_entries ?? []} money={money} onChanged={onChanged} />
        <LogTime orderId={costs.order_id} itemId={null} onDone={onChanged} />
        <AddCost orderId={costs.order_id} itemId={null} currency={costs.currency} exponent={costs.minor_exponent} onDone={onChanged} />
      </div>
      {costs.bambuddy_unavailable && <p className="text-xs text-warn">Bambuddy could not be read: its costs are missing.</p>}
    </section>
  )
}
