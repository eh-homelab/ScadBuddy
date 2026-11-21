import type { TypeKValueInput } from '../../api/filament'

export interface KPrinter {
  id: string
  name: string
}

const DECIMAL = /^\d+(\.\d+)?$/

/** The K value as typed, for the field: the API sends and takes it as a decimal string. */
const text = (value: TypeKValueInput['k_value']) => String(value)

export const keyOf = (row: Pick<TypeKValueInput, 'printer_id' | 'extruder' | 'nozzle_diameter'>) => `${row.printer_id}|${row.extruder}|${row.nozzle_diameter.trim()}`

/** Why `rows` cannot be saved, or `null`. A K value is keyed by printer, extruder and nozzle. */
export function kValueRowsError(rows: readonly TypeKValueInput[]): string | null {
  const seen = new Set<string>()
  for (const row of rows) {
    if (!row.printer_id) return 'Choose the printer of each K value.'
    if (!DECIMAL.test(row.nozzle_diameter.trim())) return `"${row.nozzle_diameter}" is not a nozzle. Enter a diameter in mm, such as 0.4.`
    if (!DECIMAL.test(text(row.k_value).trim())) return 'Enter each K value as a number, such as 0.020.'
    if (seen.has(keyOf(row))) return 'Two K values have the same printer, extruder and nozzle. Keep one.'
    seen.add(keyOf(row))
  }
  return null
}
