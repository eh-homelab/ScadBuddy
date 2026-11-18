import type { AmsSyncResult } from '../../api/filament'

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`

/** What one AMS weights sync did, in words: a short title with the spools it booked, and a detail with each kind it skipped (#6268). */
export function syncText(result: Pick<AmsSyncResult, 'booked' | 'locked' | 'printing' | 'no_remain' | 'stale'>): { title: string; detail?: string } {
  const title = `Synced ${plural(result.booked, 'spool', 'spools')}`
  const skipped = [
    result.locked > 0 && `${result.locked} locked`,
    result.printing > 0 && `${result.printing} on a printing printer`,
    result.no_remain > 0 && `${result.no_remain} with no AMS weight`,
    result.stale > 0 && `${result.stale} whose printer has not reported lately`,
  ].filter(Boolean)
  if (skipped.length > 0) return { title, detail: `Skipped ${skipped.join(', ')}.` }
  return result.booked === 0 ? { title, detail: 'No spool is loaded in an AMS.' } : { title }
}
