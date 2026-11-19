/** #6269 — how the forecast reads: its burn rates and days, and the order of its rows (`SpoolForecast`, the Metrics tab). */
import type { SpoolForecast as SpoolRow, TypeForecast } from '../../api/filament'

/** A burn rate as a person reads it. */
export const burnText = (value: number | null | undefined) => (value == null ? '—' : `${Number(value.toFixed(1))} g/day`)
/** A calendar day (`YYYY-MM-DD`) as a person reads it, on that day wherever they are. */
export const dayText = (iso: string | null | undefined) =>
  iso ? new Date(`${iso}T00:00:00Z`).toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' }) : '—'

/** Soonest empty day first; a spool with none last, by number. */
export function bySpoolEmpty(a: SpoolRow, b: SpoolRow): number {
  if (a.empty_on && b.empty_on) return a.empty_on.localeCompare(b.empty_on) || a.number - b.number
  if (a.empty_on) return -1
  if (b.empty_on) return 1
  return a.number - b.number
}

/** "Reorder now" first, then the soonest reorder day; a type with none last, by code. */
export function byReorder(a: TypeForecast, b: TypeForecast): number {
  if (a.reorder_now !== b.reorder_now) return a.reorder_now ? -1 : 1
  if (a.reorder_on && b.reorder_on) return a.reorder_on.localeCompare(b.reorder_on) || a.label.localeCompare(b.label)
  if (a.reorder_on) return -1
  if (b.reorder_on) return 1
  return a.label.localeCompare(b.label)
}
