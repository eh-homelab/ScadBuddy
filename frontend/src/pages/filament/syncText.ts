import type { AmsSyncResult } from '../../api/filament'

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`

/** The stale printers by name, or by their short id when a printer has no name (#6679). */
const printerNames = (printers: AmsSyncResult['stale_printers'] = []) =>
  printers.length > 0 ? ` (${printers.map((p) => p.name ?? `printer ${p.id.slice(0, 8)}`).join(', ')})` : ''

/** What one AMS weights sync did, in words: a short title with the spools it booked, and a detail with each kind it skipped (#6268). */
export function syncText(
  result: Pick<AmsSyncResult, 'booked' | 'locked' | 'printing' | 'no_remain' | 'stale' | 'stale_printers'>,
): { title: string; detail?: string } {
  const title = `Synced ${plural(result.booked, 'spool', 'spools')}`
  const skipped = [
    result.locked > 0 && `${result.locked} locked`,
    result.printing > 0 && `${result.printing} on a printing printer`,
    result.no_remain > 0 && `${result.no_remain} with no AMS weight`,
    result.stale > 0 && `${result.stale} whose printer has not reported lately${printerNames(result.stale_printers)}`,
  ].filter(Boolean)
  if (skipped.length > 0) return { title, detail: `Skipped ${skipped.join(', ')}.` }
  return result.booked === 0 ? { title, detail: 'No spool is loaded in an AMS.' } : { title }
}
