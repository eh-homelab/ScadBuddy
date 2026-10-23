import type { Filament, SlotNeed } from '../../api/types'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { filamentLabel } from './FilamentsEditor'

/**
 * #4250 — the print dialog's quote mode (plan Ruling 2): beside the spool each slot is
 * sliced with, the price-book filament it is priced with. The spool slices; the filament
 * prices; nothing checks that one is the other until spools are imported as records
 * (phase 5). `value` is keyed as the item keys its colours (`colours[].slot`, from 0),
 * which is the dialog's `slot_id` less one.
 */
export function QuoteFilaments({
  slots,
  filaments,
  value,
  onChange,
}: {
  slots: SlotNeed[]
  filaments: Filament[]
  value: Record<string, string>
  onChange: (next: Record<string, string>) => void
}) {
  return (
    <fieldset className="grid gap-2" aria-label="Priced with">
      <legend className="text-sm font-medium">Priced with</legend>
      {filaments.length === 0 && (
        <p className="text-xs text-warn">
          The price book has no filaments yet: add them in Settings, Orders.
        </p>
      )}
      {slots.map((slot) => {
        const key = String(slot.slot_id - 1)
        const id = `quote-filament-${slot.slot_id}`
        return (
          <div key={slot.slot_id} className="flex flex-wrap items-center gap-2">
            <Label htmlFor={id} className="w-20 shrink-0">
              <span aria-hidden="true" className="inline-block size-3 rounded-sm border" style={{ background: slot.colour ?? undefined }} />
              Slot {slot.slot_id}
            </Label>
            <Select value={value[key] ?? ''} onValueChange={(next) => onChange({ ...value, [key]: next })}>
              <SelectTrigger id={id} className="w-64" aria-label={`Filament for slot ${slot.slot_id}`}>
                <SelectValue placeholder="Choose a filament" />
              </SelectTrigger>
              <SelectContent>
                {filaments.map((filament) => (
                  <SelectItem key={filament.id} value={filament.id}>
                    {filamentLabel(filament)} · {filament.cost_per_kg}/kg
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )
      })}
    </fieldset>
  )
}
