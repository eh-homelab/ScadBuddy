import { useState } from 'react'
import type { PrintCheck, PrintRunRequest } from '../api/types'
import { sourceApi, sourceKey, type PrintSource } from './printSource'
import { ANALYSIS_DEBOUNCE_MS } from './useAnalysis'
import { useAsync } from './useAsync'
import { useDebounced } from './useDebounced'
import { useLatest } from './useLatest'

interface Answer {
  source: string | undefined
  request: string
  verdict: PrintCheck
}

export interface PrintCheckState {
  /** The newest verdict for this source; it stays on screen while a re-check reads. */
  verdict: PrintCheck | null
  /** Whether `verdict` answers the request as it stands now, not an earlier one. */
  current: boolean
  error: Error | undefined
}

/**
 * #755 — the run's nozzle verdict for the print the dialog would send, read before
 * Print (`POST …/check`, the same `plan_extruders` the run refuses with). The choices
 * settle for as long as the analyzers' do before it is read. `request: null` reads
 * nothing.
 */
export function usePrintCheck(
  source: PrintSource | undefined,
  request: PrintRunRequest | null,
): PrintCheckState {
  const latest = useLatest(source)
  const own = sourceKey(source)
  const wanted = request === null ? null : JSON.stringify(request)
  const key = useDebounced(wanted, ANALYSIS_DEBOUNCE_MS)
  const state = useAsync<Answer | null>(async () => {
    const current = latest.current
    if (key === null || !current) return null
    const verdict = await sourceApi(current).check(JSON.parse(key) as PrintRunRequest)
    return { source: own, request: key, verdict }
  }, [own, key])

  // Kept across re-checks of the same source, so a change of choice does not blank the
  // verdict while the new one is on its way.
  const [kept, setKept] = useState<Answer | null>(null)
  if (state.data && state.data !== kept) setKept(state.data)
  const answer = kept?.source === own && wanted !== null ? kept : null

  return {
    verdict: state.error ? null : (answer?.verdict ?? null),
    current: !state.error && answer?.request === wanted,
    error: state.error,
  }
}
