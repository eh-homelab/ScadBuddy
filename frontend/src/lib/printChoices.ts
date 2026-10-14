import type { ChoicesView, NozzleChoice, PresetRef, PrintChoices, PrintRunRequest, PrintSequence } from '../api/types'
import type { PrintSelection } from './usePrintChoices'

/** Both sides at 0.4 mm, standard flow: the defaults when the server names none. */
export const DEFAULT_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'standard' },
  { size: '0.4', flow: 'standard' },
]

/**
 * What the dialog opens on with nothing remembered: the chosen printer's defaults,
 * High Flow on each side that has a High Flow nozzle of the size (#1895).
 */
export function defaultsOf(choices: ChoicesView): NozzleChoice[] {
  const defaults = choices.default_nozzles ?? []
  return defaults.length > 0 ? defaults : DEFAULT_NOZZLES
}

/** A preset's identity as one string, for a `<select>` value. */
export function refKey(ref: PresetRef): string {
  return `${ref.source}:${ref.id}`
}

/**
 * The run request's `choices` for a selection, or null until the bed type is known:
 * what `POST /print/outputs/{id}/run` sends and the analyzers judge (#284).
 */
export function printChoicesOf(selection: PrintSelection): PrintChoices | null {
  const { nozzles, tier, processName, bedType, overrides, sides } = selection
  return bedType === null
    ? null
    : {
        nozzles,
        tier,
        process_name: processName,
        bed_type: bedType,
        filament_overrides: overrides,
        // Sent only when a side was chosen by hand (#2166), so a request is what it was.
        ...(Object.keys(sides).length > 0 ? { sides } : {}),
      }
}


/**
 * The request fields, beside the plan and the choices, that change what a run slices
 * (#2825): today the print sequence, which the backend folds into the process overrides.
 * The check, the background slice and the run all send these, from here, so the slice
 * the dialog reuses is the one the run would make.
 */
export function sliceOverrides(printSequence: PrintSequence | null): Pick<PrintRunRequest, 'print_sequence'> {
  return printSequence === null ? {} : { print_sequence: printSequence }
}
