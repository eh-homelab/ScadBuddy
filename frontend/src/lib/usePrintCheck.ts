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
  /** #5627 — the check asked for the process's part settings. */
  processValues: boolean
  verdict: PrintCheck
}

export interface PrintCheckState {
  /** The newest verdict for this source; it stays on screen while a re-check reads. */
  verdict: PrintCheck | null
  /** Whether `verdict` answers the request as it stands now, not an earlier one. */
  current: boolean
  /** The request as it stands now, serialised once: a key for what was asked, or `null`. */
  request: string | null
  error: Error | undefined
  /** Read the verdict again, as after a failed read. */
  reload: () => void
  /** #5627 — `verdict` came from a check that read the process's part settings. */
  processValuesRead: boolean
}

/**
 * #755, #760 — what the run would refuse for the print the dialog would send, read
 * before Print (`POST …/check`, the run's own pre-upload refusals). The choices
 * settle for as long as the analyzers' do before it is read. `request: null` reads
 * nothing. `processValues` (#5627) asks for the process's part settings too; asking
 * reads again, though the request is the same.
 */
export function usePrintCheck(
  source: PrintSource | undefined,
  request: PrintRunRequest | null,
  processValues = false,
): PrintCheckState {
  const latest = useLatest(source)
  const own = sourceKey(source)
  const wanted = request === null ? null : JSON.stringify(request)
  const key = useDebounced(wanted, ANALYSIS_DEBOUNCE_MS)
  const state = useAsync<Answer | null>(async () => {
    const current = latest.current
    if (key === null || !current) return null
    const verdict = await sourceApi(current).check(JSON.parse(key) as PrintRunRequest, processValues)
    return { source: own, request: key, processValues, verdict }
  }, [own, key, processValues])

  // Kept across re-checks of the same source, so a change of choice does not blank the
  // verdict while the new one is on its way.
  const [kept, setKept] = useState<Answer | null>(null)
  if (state.data && state.data !== kept) setKept(state.data)
  const answer = kept?.source === own && wanted !== null ? kept : null

  return {
    verdict: state.error ? null : (answer?.verdict ?? null),
    current: !state.error && answer?.request === wanted,
    request: wanted,
    error: state.error,
    reload: state.reload,
    processValuesRead: !state.error && answer?.processValues === true,
  }
}
