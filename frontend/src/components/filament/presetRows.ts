import type { PresetInput } from '../../api/filament'

const DIAMETER = /^\d+(\.\d+)?$/
export const keyOf = (row: Pick<PresetInput, 'printer_model' | 'nozzle_diameter'>) => `${row.printer_model.trim().toUpperCase()}|${row.nozzle_diameter.trim()}`

/**
 * Why `rows` cannot be saved, or `null`. A preset is keyed by printer model and nozzle
 * diameter; `*` was a nozzle once (#6262 made presets per printer and per nozzle) and no
 * longer is, so it is refused here before the server refuses it.
 */
export function presetRowsError(rows: readonly PresetInput[]): string | null {
  const seen = new Set<string>()
  for (const row of rows) {
    const nozzle = row.nozzle_diameter.trim()
    if (!row.printer_model.trim()) return 'Name the printer model of each preset, such as H2C.'
    if (!DIAMETER.test(nozzle)) return `"${nozzle}" is not a nozzle. Enter a diameter in mm, such as 0.4.`
    if (!row.slicer_filament.trim()) return `Enter the slicer filament of ${row.printer_model} ${nozzle}, such as GFA00.`
    if (seen.has(keyOf(row))) return `${row.printer_model} ${nozzle} has two presets. Keep one.`
    seen.add(keyOf(row))
  }
  return null
}
