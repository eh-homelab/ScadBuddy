import type { NozzleChoice, PresetRef } from '../api/types'

/** Both sides at 0.4 mm, standard flow: what the dialog opens on with nothing remembered. */
export const DEFAULT_NOZZLES: NozzleChoice[] = [
  { size: '0.4', flow: 'standard' },
  { size: '0.4', flow: 'standard' },
]

/** A preset's identity as one string, for a `<select>` value. */
export function refKey(ref: PresetRef): string {
  return `${ref.source}:${ref.id}`
}
