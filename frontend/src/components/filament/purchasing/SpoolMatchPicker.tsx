import type { Spool } from '../../../api/filament'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { RgbaSwatch } from '../RgbaSwatch'

/**
 * #6266 — "Fills existing spools": a line may fill spools already on the shelf (on no
 * invoice) instead of making new ones. `candidates` are the spools it may fill; no more
 * than `quantity` can be ticked. A spool with a cost says the line's cost replaces it (#6700).
 * A used-up (archived) spool is marked so (#6707): it takes the line's cost and stays archived.
 */
export function SpoolMatchPicker({
  line,
  candidates,
  quantity,
  value,
  money,
  onChange,
}: {
  /** The line's number, for the button's name. */
  line: number
  candidates: Spool[]
  quantity: number
  value: string[]
  /** A money amount in the invoice's currency. */
  money: (amount: string) => string
  onChange: (next: string[]) => void
}) {
  const full = value.length >= quantity
  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button type="button" variant="outline" size="sm" disabled={candidates.length === 0 && value.length === 0} aria-label={`Fills existing spools, line ${line}`}>
          {candidates.length === 0 && value.length === 0 ? 'No spool to fill' : `Fills existing spools${value.length ? ` (${value.length})` : ''}`}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="start" className="w-72">
        <p className="mb-2 text-[12px] text-muted-foreground">Tick the spools this line fills in, up to {quantity}. Each takes the line&apos;s cost and type (a used-up spool takes the cost only and stays archived); the rest are made new.</p>
        <ul className="flex max-h-56 flex-col gap-1 overflow-y-auto">
          {candidates.map((spool) => {
            const on = value.includes(spool.id)
            return (
              <li key={spool.id}>
                <label className="flex items-center gap-2 text-[13px]">
                  <Checkbox checked={on} disabled={!on && full} onCheckedChange={(next) => onChange(next === true ? [...value, spool.id] : value.filter((id) => id !== spool.id))} />
                  <RgbaSwatch rgba={spool.rgba ?? '#FFFFFFFF'} size="sm" />
                  <span>Spool #{spool.number}</span>
                  {spool.colour_name && <span className="text-muted-foreground">{spool.colour_name}</span>}
                  {spool.archived_at && <span className="rounded-sm border border-border px-1 text-[11px] text-muted-foreground">used up</span>}
                  {spool.cost_per_kg !== null && <span className="ml-auto text-[12px] text-muted-foreground">replaces {money(spool.cost_per_kg)}/kg</span>}
                </label>
              </li>
            )
          })}
        </ul>
      </PopoverContent>
    </Popover>
  )
}
