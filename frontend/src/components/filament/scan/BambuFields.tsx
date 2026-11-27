import type { ReactNode } from 'react'
import { RgbaSwatch } from '../RgbaSwatch'

/**
 * The fields of a Bambu MIFARE read as the server decoded them (#8148, `bambu` in the read's
 * answer, `tools/go/core/internal/bambutag` `Tag`). A field whose block the phone did not read
 * is absent, so this lists only what was read and says how much of the tag that was. The hex
 * view is a later change.
 */
export interface BambuTag {
  complete?: boolean
  missing_blocks?: number[]
  variant_id?: string
  material_id?: string
  filament_type?: string
  detailed_filament_type?: string
  color?: string
  second_color?: string
  spool_weight_g?: number
  filament_diameter_mm?: number
  drying_temperature_c?: number
  drying_time_h?: number
  hotend_min_temperature_c?: number
  hotend_max_temperature_c?: number
  tray_uid?: string
  production_datetime?: string
  production_date?: string
}

/** The 48 data blocks of a MIFARE Classic 1K tag (16 sectors of 3). */
const DATA_BLOCKS = 48

export function BambuFields({ bambu }: { bambu: BambuTag }) {
  const rows: [string, ReactNode][] = []
  const material = bambu.detailed_filament_type || bambu.filament_type
  if (material) rows.push(['Material', material])
  if (bambu.material_id || bambu.variant_id) rows.push(['Bambu code', [bambu.material_id, bambu.variant_id].filter(Boolean).join(' / ')])
  for (const [term, rgba] of [
    ['Colour', bambu.color],
    ['Second colour', bambu.second_color],
  ] as const) {
    if (!rgba) continue
    rows.push([
      term,
      <span key={term} className="inline-flex items-center gap-2">
        <RgbaSwatch rgba={rgba} size="sm" />
        {rgba}
      </span>,
    ])
  }
  if (bambu.spool_weight_g) rows.push(['Weight', `${bambu.spool_weight_g} g`])
  if (bambu.filament_diameter_mm) rows.push(['Diameter', `${Math.round(bambu.filament_diameter_mm * 100) / 100} mm`])
  const lo = bambu.hotend_min_temperature_c
  const hi = bambu.hotend_max_temperature_c
  if (lo || hi) rows.push(['Nozzle', lo && hi ? `${lo}–${hi} °C` : `${lo || hi} °C`])
  if (bambu.drying_temperature_c || bambu.drying_time_h) {
    rows.push(['Drying', [bambu.drying_temperature_c && `${bambu.drying_temperature_c} °C`, bambu.drying_time_h && `${bambu.drying_time_h} h`].filter(Boolean).join(' for ')])
  }
  const made = bambu.production_datetime?.replace('T', ' ').slice(0, 16) ?? bambu.production_date
  if (made) rows.push(['Made', made])
  if (bambu.tray_uid) rows.push(['Tray UUID', <span key="tray" className="break-all tabular-nums">{bambu.tray_uid}</span>])
  const missing = bambu.missing_blocks?.length ?? 0
  return (
    <section aria-label="On the Bambu tag" className="rounded-xl border border-border bg-card p-4">
      <h2 className="mb-2 font-medium">On the Bambu tag</h2>
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1">
        {rows.map(([term, value]) => (
          <div key={term} className="contents">
            <dt className="text-muted-foreground">{term}</dt>
            <dd className="min-w-0 break-words">{value}</dd>
          </div>
        ))}
      </dl>
      {!bambu.complete && missing > 0 && (
        <p className="mt-2 text-warn">
          The phone read {DATA_BLOCKS - missing} of {DATA_BLOCKS} blocks. Hold the tag still and tap again to read the rest.
        </p>
      )}
    </section>
  )
}
