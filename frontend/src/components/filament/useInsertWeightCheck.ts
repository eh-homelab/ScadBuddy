import { useCallback, useEffect, useState } from 'react'
import { insertWeightOffer, type InsertWeightCheck } from '../../api/filament'

/**
 * #7452 — how long a weighing waits for the check after the request leaves. The check is one
 * indexed read, so an answer slower than this means the API is stuck. Past it the weighing
 * works without an offer, as it does when the check fails; an answer that comes later is
 * still shown.
 */
export const CHECK_WAIT_MS = 10_000

type Answer = { spoolId: string; readingG: number; round: number; check: InsertWeightCheck | null; unanswered: boolean }

/**
 * #7280 — the check of a scale reading against the spool's insert, read about 300 ms after
 * the reading stops changing. `readingG` null reads nothing. An answer for an older reading
 * is dropped, so `check` is always for `readingG` or null. `pending` is true while the check
 * for `readingG` has not answered yet, so a weighing can wait for its offer, but for at most
 * `CHECK_WAIT_MS`; then `unanswered` is true. `recheck` reads the check again (#7450-2).
 */
export function useInsertWeightCheck(
  spoolId: string | undefined,
  readingG: number | null,
): { check: InsertWeightCheck | null; pending: boolean; unanswered: boolean; recheck: () => void } {
  const [answer, setAnswer] = useState<Answer | null>(null)
  const [round, setRound] = useState(0)
  const recheck = useCallback(() => setRound((was) => was + 1), [])
  useEffect(() => {
    if (!spoolId || readingG == null) return
    let live = true
    let waited: ReturnType<typeof setTimeout> | undefined
    const timer = setTimeout(() => {
      waited = setTimeout(() => {
        if (live) setAnswer((was) => (was && was.spoolId === spoolId && was.readingG === readingG && was.round === round ? was : { spoolId, readingG, round, check: null, unanswered: true }))
      }, CHECK_WAIT_MS)
      insertWeightOffer(spoolId, readingG).then(
        (check) => {
          if (live) setAnswer({ spoolId, readingG, round, check, unanswered: false })
        },
        // No offer is shown when the check fails; the weighing still works as before.
        () => {
          if (live) setAnswer({ spoolId, readingG, round, check: null, unanswered: false })
        },
      )
    }, 300)
    return () => {
      live = false
      clearTimeout(timer)
      clearTimeout(waited)
    }
  }, [spoolId, readingG, round])
  if (!spoolId || readingG == null) return { check: null, pending: false, unanswered: false, recheck }
  const current = answer && answer.spoolId === spoolId && answer.readingG === readingG && answer.round === round ? answer : null
  return { check: current?.check ?? null, pending: current === null, unanswered: current?.unanswered ?? false, recheck }
}
