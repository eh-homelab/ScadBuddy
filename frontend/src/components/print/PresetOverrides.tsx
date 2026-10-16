import { Label } from '@/components/ui/label'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { StepCard } from './StepCard'
import type { FilamentPresetOption, PresetRef, SlotNeed } from '../../api/types'
import { refKey } from '../../lib/printChoices'

type Props = {
  size: string
  slots: SlotNeed[]
  presets: FilamentPresetOption[]
  overrides: Record<string, PresetRef>
  onChange: (slotId: number, key: string) => void
}

/** The select's value for the spool's own preset: Radix's select takes no empty value. */
const OWN = 'own'

/** "PLA" for "PLA Basic" or "PLA-CF": the material a preset and a slot are matched by. */
function materialFamily(material: string | null | undefined): string {
  return (material ?? '').trim().split(/[\s-]/)[0]?.toUpperCase() ?? ''
}

/**
 * Advanced only — a filament preset per slot, in place of the spool's own. A slot is
 * offered only presets of its own material (#2166), and any preset when its material
 * is unknown; the one chosen stays listed.
 */
export function PresetOverrides({ size, slots, presets, overrides, onChange }: Props) {
  return (
    <StepCard title={`Filament presets — ${size} mm nozzle`}>
      {slots.map((slot) => {
        const id = `preset-override-${slot.slot_id}`
        const chosen = overrides[String(slot.slot_id)]
        const family = materialFamily(slot.material)
        const offered = presets.filter(
          (row) =>
            !family ||
            !row.filament_type ||
            materialFamily(row.filament_type) === family ||
            (chosen !== undefined && refKey(row.ref) === refKey(chosen)),
        )
        return (
          <div key={slot.slot_id} className="grid gap-1.5">
            <Label htmlFor={id} className="text-xs font-normal text-muted-foreground">
              Preset for slot {slot.slot_id}
            </Label>
            <Select
              value={chosen ? refKey(chosen) : OWN}
              onValueChange={(next) => onChange(slot.slot_id, next === OWN ? '' : next)}
            >
              <SelectTrigger id={id} className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={OWN}>The spool&apos;s own preset</SelectItem>
                {offered.map((row) => (
                  <SelectItem key={refKey(row.ref)} value={refKey(row.ref)}>
                    {row.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        )
      })}
    </StepCard>
  )
}
