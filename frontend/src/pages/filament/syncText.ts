import type { AmsSyncResult } from '../../api/filament'

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`

/** What one AMS weights sync did, in words: the spools it booked, then each kind it skipped (#6268). */
export function syncText(result: Pick<AmsSyncResult, 'booked' | 'locked' | 'printing' | 'no_remain' | 'stale'>): string {
  const done = `Synced ${plural(result.booked, 'spool', 'spools')}.`
  const skipped = [
    result.locked > 0 && `${result.locked} locked`,
    result.printing > 0 && `${result.printing} on a printing printer`,
    result.no_remain > 0 && `${result.no_remain} with no AMS weight`,
    result.stale > 0 && `${result.stale} whose printer has not reported lately`,
  ].filter(Boolean)
  if (skipped.length > 0) return `${done} Skipped ${skipped.join(', ')}.`
  return result.booked === 0 ? `${done} No spool is loaded in an AMS.` : done
}
