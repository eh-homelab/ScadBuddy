import { useEffect, useState } from 'react'
import { closingPreview, type ArchiveReason, type ClosingPreview } from '../../api/filament'

interface Inputs {
  spoolId: string
  reason: ArchiveReason | null
  finalReadingG: number | null
  finalGrams: number | null
}

const same = (a: Inputs, b: Inputs) => a.spoolId === b.spoolId && a.reason === b.reason && a.finalReadingG === b.finalReadingG && a.finalGrams === b.finalGrams

/**
 * #7684 — what archiving with these inputs would record (`GET /filament/spools/{id}/closing`),
 * read about 300 ms after they stop changing, as `useInsertWeightCheck` reads an offer. An
 * answer for older inputs is dropped, so `preview` is always for the inputs given or null.
 * `pending` is true while the preview for them has not answered yet. A failed read shows no
 * preview; the archive still works.
 */
export function useClosingPreview(spoolId: string | undefined, reason: ArchiveReason | null, finalReadingG: number | null, finalGrams: number | null): { preview: ClosingPreview | null; pending: boolean } {
  const [answer, setAnswer] = useState<{ inputs: Inputs; preview: ClosingPreview | null } | null>(null)
  useEffect(() => {
    if (!spoolId) return
    const inputs: Inputs = { spoolId, reason, finalReadingG, finalGrams }
    let live = true
    const timer = setTimeout(() => {
      closingPreview(spoolId, reason, finalReadingG, finalGrams).then(
        (preview) => {
          if (live) setAnswer({ inputs, preview })
        },
        () => {
          if (live) setAnswer({ inputs, preview: null })
        },
      )
    }, 300)
    return () => {
      live = false
      clearTimeout(timer)
    }
  }, [spoolId, reason, finalReadingG, finalGrams])
  if (!spoolId) return { preview: null, pending: false }
  const current = answer && same(answer.inputs, { spoolId, reason, finalReadingG, finalGrams }) ? answer : null
  return { preview: current?.preview ?? null, pending: current === null }
}
