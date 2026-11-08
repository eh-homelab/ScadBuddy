import { useState } from 'react'
import type { Filament, SlotNeed } from '../../api/types'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { filamentLabel } from './FilamentsEditor'

/**
 * #4250 — the print dialog's quote mode (plan Ruling 2): beside the spool each slot is
 * sliced with, the price-book filament it is priced with. The spool slices; the filament
 * prices; nothing checks that one is the other until spools are imported as records
 * (phase 5). `value` is keyed as the item keys its colours (`colours[].slot`, from 0),
 * which is the dialog's `slot_id` less one.
 *
 * #4625 (order operations spec §6.3) — `support` is the price-book filament of each sliced
 * slot that is no colour slot of the item (a support or support interface filament, PETG
 * under PLA on the H2C), keyed the same way: priced as its own `Support, …` line from the
 * slice's grams, never written onto the item.
 */
export function QuoteFilaments({
  slots,
  filaments,
  value,
  onChange,
  support = {},
  onSupportChange,
}: {
  slots: SlotNeed[]
  filaments: Filament[]
  value: Record<string, string>
  onChange: (next: Record<string, string>) => void
  support?: Record<string, string>
  onSupportChange?: (next: Record<string, string>) => void
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
            <FilamentSelect
              id={id}
              label={`Filament for slot ${slot.slot_id}`}
              filaments={filaments}
              value={value[key] ?? ''}
              onChange={(next) => onChange({ ...value, [key]: next })}
            />
          </div>
        )
      })}
      {onSupportChange && (
        <SupportFilaments
          colourSlots={new Set(slots.map((slot) => slot.slot_id))}
          filaments={filaments}
          value={support}
          onChange={onSupportChange}
        />
      )}
    </fieldset>
  )
}

function FilamentSelect({ id, label, filaments, value, onChange }: { id: string; label: string; filaments: Filament[]; value: string; onChange: (next: string) => void }) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger id={id} className="w-64" aria-label={label}>
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
  )
}

/** The support slots: each a slot number (as the dialog numbers slots, from 1) and its filament. */
function SupportFilaments({
  colourSlots,
  filaments,
  value,
  onChange,
}: {
  colourSlots: Set<number>
  filaments: Filament[]
  value: Record<string, string>
  onChange: (next: Record<string, string>) => void
}) {
  const [slot, setSlot] = useState('')
  const [error, setError] = useState<string | null>(null)
  const entries = Object.entries(value).sort((a, b) => Number(a[0]) - Number(b[0]))

  function add() {
    const number = Number(slot)
    if (!/^\d+$/.test(slot.trim()) || number < 1) return setError('Enter the slot number the support prints from, such as 5.')
    if (colourSlots.has(number)) return setError(`Slot ${number} is a colour slot: it is priced with its own filament above.`)
    if (String(number - 1) in value) return setError(`Slot ${number} has a support filament already.`)
    setError(null)
    setSlot('')
    onChange({ ...value, [String(number - 1)]: '' })
  }

  return (
    <div className="grid gap-2" role="group" aria-label="Support filaments">
      <p className="text-xs text-muted-foreground">
        A slot that prints only support or a support interface (PETG under PLA, say) is priced as its own support line, from the grams the slice reads for it.
      </p>
      {entries.map(([key, filament]) => {
        const shown = Number(key) + 1
        return (
          <div key={key} className="flex flex-wrap items-center gap-2">
            <Label htmlFor={`quote-support-${shown}`} className="w-20 shrink-0">
              Support {shown}
            </Label>
            <FilamentSelect
              id={`quote-support-${shown}`}
              label={`Support filament for slot ${shown}`}
              filaments={filaments}
              value={filament}
              onChange={(next) => onChange({ ...value, [key]: next })}
            />
            <Button
              type="button"
              size="xs"
              variant="ghost"
              aria-label={`Remove the support filament for slot ${shown}`}
              onClick={() => onChange(Object.fromEntries(entries.filter(([other]) => other !== key)))}
            >
              Remove
            </Button>
          </div>
        )
      })}
      <div className="flex flex-wrap items-end gap-2">
        <div className="flex flex-col gap-1">
          <Label htmlFor="quote-support-slot">Support slot</Label>
          <Input id="quote-support-slot" className="w-20" inputMode="numeric" placeholder="5" value={slot} onChange={(event) => setSlot(event.target.value)} />
        </div>
        <Button type="button" size="sm" variant="outline" onClick={add}>
          Add support filament
        </Button>
      </div>
      {error && (
        <p role="alert" className="text-xs text-warn">
          {error}
        </p>
      )}
    </div>
  )
}
