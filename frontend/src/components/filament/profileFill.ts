import type { FillField, TypeFill } from '../../api/filament'

/** The type page's names for the fields a profile fills. */
export const FILL_LABELS: Record<FillField, string> = {
  nozzle_temp_min: 'Nozzle temperature, min (°C)',
  nozzle_temp_max: 'Nozzle temperature, max (°C)',
  bed_temp_min: 'Bed temperature, min (°C)',
  bed_temp_max: 'Bed temperature, max (°C)',
  density_g_cm3: 'Density (g/cm³)',
  diameter_mm: 'Diameter (mm)',
  drying_temp_c: 'Drying temperature (°C)',
  drying_hours: 'Drying time (h)',
}

/** Where a profile came from, as a person reads it. */
export function sourceText(source: NonNullable<TypeFill['source']>): string {
  const kind = source.origin === 'scadbuddy' ? 'ScadBuddy preset' : 'Bambu Studio preset'
  return `${source.name}, the ${kind} ${source.preset_id} for the ${source.printer_model} ${source.nozzle_diameter} mm nozzle`
}
