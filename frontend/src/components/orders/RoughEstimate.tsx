import { useState } from 'react'
import { api } from '../../api/client'
import type { OrderEstimate } from '../../api/types'
import { formatMoney } from '../../lib/money'
import { useAsync } from '../../lib/useAsync'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { filamentLabel } from './FilamentsEditor'
import { refusalText } from './refusal'

/**
 * The rough estimate of an output's filament (§5.3, #4250, `GET /orders/estimate`): pick a
 * price-book filament and read about how many grams it takes, and what they cost. Never a
 * quote, and nothing is recorded. Shown only when the price book has filaments. Both
 * `AddToOrderDialog` and `AddItemDialog` show it, so the two cannot drift (#4727).
 */
export function RoughEstimate({ outputId, idPrefix = 'order-estimate' }: { outputId: string; idPrefix?: string }) {
  const [filamentId, setFilamentId] = useState('')
  const book = useAsync(() => api.priceBook(), [])
  const estimate = useAsync(
    () => (filamentId ? api.orderEstimate(outputId, filamentId) : Promise.resolve(null as OrderEstimate | null)),
    [outputId, filamentId],
  )
  const filaments = book.data?.filaments ?? []
  if (filaments.length === 0) return null
  return (
    <div className="flex flex-col gap-1">
      <Label htmlFor={`${idPrefix}-filament`}>Estimate with</Label>
      <Select value={filamentId} onValueChange={setFilamentId}>
        <SelectTrigger id={`${idPrefix}-filament`} aria-label="Estimate with">
          <SelectValue placeholder="Pick a filament for a rough estimate" />
        </SelectTrigger>
        <SelectContent position="popper">
          {filaments.map((f) => (
            <SelectItem key={f.id} value={f.id}>
              {filamentLabel(f)}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {estimate.data && (
        <p className="text-[13px] text-muted-foreground" data-testid="order-estimate">
          An estimate: about <span className="sb-num text-foreground">{estimate.data.grams} g</span>, filament{' '}
          <span className="sb-num text-foreground">{formatMoney(estimate.data.filament_minor, estimate.data.currency, estimate.data.minor_exponent)}</span>
          {estimate.data.unknown_parts > 0 ? `, leaving out ${estimate.data.unknown_parts} part(s) with no volume` : ''}. Quote it from a slice once it is on an
          order.
        </p>
      )}
      {estimate.error && (
        <p className="text-[13px] text-muted-foreground" data-testid="order-estimate">
          No estimate: {refusalText(estimate.error)}
        </p>
      )}
    </div>
  )
}
