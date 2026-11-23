import { useEffect, useState } from 'react'
import { insertWeightOffer, type InsertWeightCheck } from '../../api/filament'

/**
 * #7280 — the check of a scale reading against the spool's insert, read about 300 ms after
 * the reading stops changing. `readingG` null reads nothing. An answer for an older reading
 * is dropped, so the check returned is always for `readingG` or null.
 */
export function useInsertWeightCheck(spoolId: string | undefined, readingG: number | null): InsertWeightCheck | null {
  const [answer, setAnswer] = useState<{ spoolId: string; readingG: number; check: InsertWeightCheck } | null>(null)
  useEffect(() => {
    if (!spoolId || readingG == null) return
    let live = true
    const timer = setTimeout(() => {
      insertWeightOffer(spoolId, readingG).then(
        (check) => {
          if (live) setAnswer({ spoolId, readingG, check })
        },
        // No offer is shown when the check fails; the weighing still works as before.
        () => {},
      )
    }, 300)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [spoolId, readingG])
  return answer && answer.spoolId === spoolId && answer.readingG === readingG ? answer.check : null
}
