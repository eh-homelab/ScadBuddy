import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { PrintProgress } from '../api/types'

const POLL_MS = 2000

export interface PrintProgressState {
  /** The last answer for this output, or `null` when it has never been printed. */
  progress: PrintProgress | null
  error: Error | undefined
  /** True from the first read until the print settles. */
  polling: boolean
}

/**
 * Follows a print to its queue entries (#89), polling until the backend says it has
 * settled. A sibling of `useRenderJob`: one chained timeout, superseded by generation,
 * cleared on unmount.
 *
 * Three things about the backend's answer decide when this stops, and none of them can
 * be worked out in the browser:
 *
 * - **`settled` is the only stop condition.** A pipeline run whose slice failed keeps
 *   reporting `status: "in_progress"` with `copies_in_progress: 1` for ever — the
 *   recorded `pipeline-run.json` is exactly that — so a poll that waited for `stage` to
 *   move, or for the counters to account for every copy, would never end. The backend
 *   settles it from `completed_at`; re-deriving "finished" here would reintroduce the
 *   infinite poll.
 * - **`null` is an answer**, not an error: the output has never been printed. There is
 *   nothing to wait for, so the poll stops rather than retrying.
 * - **The queue entries do not exist when the run is accepted.** `run` answers 202 and
 *   creates them in a background task, which is why this polls at all rather than
 *   reading the run response once.
 */
export function usePrintProgress(
  outputId: string | undefined,
  enabled: boolean,
): PrintProgressState {
  const [progress, setProgress] = useState<PrintProgress | null>(null)
  const [error, setError] = useState<Error | undefined>(undefined)
  const [polling, setPolling] = useState(false)
  const generation = useRef(0)

  useEffect(() => {
    if (!outputId || !enabled) return

    const mine = ++generation.current
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false

    const isStale = () => stopped || generation.current !== mine

    // Progress describes one output's print, so the previous output's answer is wrong
    // rather than merely stale — and a slow read for it must not land on top of this one.
    setProgress(null)
    setError(undefined)
    setPolling(true)

    async function poll(id: string) {
      try {
        const next = await api.getPrintProgress(id)
        if (isStale()) return
        setProgress(next)
        if (next === null || next.settled) {
          setPolling(false)
          return
        }
        timer = setTimeout(() => void poll(id), POLL_MS)
      } catch (cause) {
        if (isStale()) return
        setError(cause instanceof Error ? cause : new Error(String(cause)))
        setPolling(false)
      }
    }

    void poll(outputId)

    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [outputId, enabled])

  return { progress, error, polling }
}
