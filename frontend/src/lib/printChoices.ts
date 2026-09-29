import type { NozzleChoice, PresetRef, PrintChoices } from '../api/types'
import type { PrintSelection } from './usePrintChoices'

/** Both sides at 0.4 mm, standard flow: what the dialog opens on with nothing remembered. */
export const DEFAULT_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'standard' },
  { size: '0.4', flow: 'standard' },
]

/** A preset's identity as one string, for a `<select>` value. */
export function refKey(ref: PresetRef): string {
  return `${ref.source}:${ref.id}`
}

/**
 * The run request's `choices` for a selection, or null until the bed type is known:
 * what `POST /print/outputs/{id}/run` sends and the analyzers judge (#284).
 */
export function printChoicesOf(selection: PrintSelection): PrintChoices | null {
  const { nozzles, tier, processName, bedType, overrides } = selection
  return bedType === null
    ? null
    : { nozzles, tier, process_name: processName, bed_type: bedType, filament_overrides: overrides }
}
