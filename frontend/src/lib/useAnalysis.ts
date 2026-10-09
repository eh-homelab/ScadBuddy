import { useState } from 'react'
import { api } from '../api/client'
import type { AnalysisReport, AnalysisRequest, AnalysisTarget } from '../api/types'
import { useAsync } from './useAsync'
import { useDebounced } from './useDebounced'

/** How long the dialog's choices settle before the print is judged again. */
export const ANALYSIS_DEBOUNCE_MS = 300

export interface Analysis {
  /** The newest report for this subject; the previous one stays while a re-run reads. */
  report: AnalysisReport | null
  error: Error | undefined
  /** A run for the current request has not answered yet. */
  checking: boolean
  reload: () => void
}

/**
 * #284 — the print analyzers' report for the print the dialog would send.
 *
 * `POST /analyzers/run` (#461) with the dialog's request as `request`; it reads only
 * (`post_run`, `backend/scadbuddy/api/analyzers.py`). `target` is the output, or the
 * library file (#1753), judged the same way. `detail: 'advanced'` because simple detail
 * drops each finding's location and explanation, and the suppressed ones (`_simple`,
 * `backend/scadbuddy/analyzers/runner.py`), which the dialog shows.
 *
 * Live on the `analyzers` topic, where the backend publishes every `analyzer.decision`
 * (`backend/scadbuddy/api/realtime.py:104`): a decision recorded anywhere is read again.
 * `request: null` (the dialog has no choices yet) runs nothing.
 */
export function useAnalysis(
  target: AnalysisTarget | undefined,
  request: AnalysisRequest | null,
): Analysis {
  const key = useDebounced(request === null ? null : JSON.stringify(request), ANALYSIS_DEBOUNCE_MS)
  const subject = target === undefined ? undefined : JSON.stringify(target)
  const live = subject !== undefined && key !== null
  const state = useAsync<AnalysisReport | null>(
    () =>
      live
        ? api.runAnalyzers({
            target: JSON.parse(subject) as AnalysisTarget,
            request: JSON.parse(key) as AnalysisRequest,
            detail: 'advanced',
          })
        : Promise.resolve(null),
    [subject, key],
    live ? ['analyzers'] : [],
  )

  // Kept across re-runs of the same subject, so a change of nozzle does not blank the list
  // while the new answer is on its way.
  const [previous, setPrevious] = useState<{ subject?: string; report: AnalysisReport | null }>({
    report: null,
  })
  if (state.data !== undefined && previous.report !== state.data) {
    setPrevious({ subject, report: state.data })
  }
  const kept = previous.subject === subject ? previous.report : null

  return {
    report: state.error ? null : (state.data ?? kept),
    error: state.error,
    checking: state.loading || (request !== null && JSON.stringify(request) !== key),
    reload: state.reload,
  }
}
