import type { FilamentPresetOption, PresetRef, SlotNeed } from '../../api/types'
import { refKey } from '../../lib/printChoices'

type Props = {
  size: string
  slots: SlotNeed[]
  presets: FilamentPresetOption[]
  overrides: Record<string, PresetRef>
  onChange: (slotId: number, key: string) => void
}

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
    <fieldset className="rounded-[6px] border border-line bg-surface-2 px-3 py-2">
      <legend className="px-1 text-[13px] text-ink">Filament presets — {size} mm nozzle</legend>
      <div className="mt-1.5 flex flex-col gap-2">
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
            <div key={slot.slot_id} className="flex flex-col gap-1">
              <label htmlFor={id} className="text-[12px] text-muted">
                Preset for slot {slot.slot_id}
              </label>
              <select
                id={id}
                value={chosen ? refKey(chosen) : ''}
                onChange={(event) => onChange(slot.slot_id, event.target.value)}
                className="sb-field"
              >
                <option value="">The spool&apos;s own preset</option>
                {offered.map((row) => (
                  <option key={refKey(row.ref)} value={refKey(row.ref)}>
                    {row.name}
                  </option>
                ))}
              </select>
            </div>
          )
        })}
      </div>
    </fieldset>
  )
}
