import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { PrintProgress } from '../api/types'
import { getRealtime } from './realtime'

/** Only while the realtime socket is unavailable (#268): otherwise events drive the reads. */
const POLL_MS = 2000
/**
 * While the socket is up, one read at least this often anyway: the watcher's longest
 * wait (`MAX_INTERVAL`). A dialog then never freezes on a print no watcher here is
 * following (another replica holds it and, until #374, its events stay there; or the
 * watcher stopped), and each read re-arms the watcher (the progress route).
 */
const LIVE_BACKSTOP_MS = 30_000

export interface PrintProgressState {
  /** The last answer for this output, or `null` when it has never been printed. */
  progress: PrintProgress | null
  error: Error | undefined
  /** True from the first read until the print settles: "watching". */
  polling: boolean
}

/**
 * Follows a print to its queue entries (#89) until the backend says it has settled.
 * #268: the backend's own watcher (`bambuddy/watcher.py`) follows the print and
 * publishes `print.*` on `print:<output id>`; this reads once now and once per
 * event, polls every 2 s while the realtime socket is unavailable, and reads every
 * 30 s regardless (`LIVE_BACKSTOP_MS`). A
 * sibling of `useRenderJob`, superseded by generation, cleared on unmount.
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
 *   creates them in a background task, which is why this follows the print at all
 *   rather than reading the run response once.
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
    let unfollow: (() => void) | undefined
    let stopped = false
    let finished = false
    let lastRead = 0
    let reading = false
    let again = false

    const isStale = () => stopped || generation.current !== mine

    // Progress describes one output's print, so the previous output's answer is wrong
    // rather than merely stale — and a slow read for it must not land on top of this one.
    setProgress(null)
    setError(undefined)
    setPolling(true)

    const finish = () => {
      finished = true
      unfollow?.()
      unfollow = undefined
      if (timer) clearTimeout(timer)
      setPolling(false)
    }

    /** One read at a time; events that arrive during one cause one more. */
    async function read(id: string) {
      if (finished || isStale()) return
      if (reading) {
        again = true
        return
      }
      reading = true
      lastRead = Date.now()
      try {
        const next = await api.getPrintProgress(id)
        if (isStale()) return
        setProgress(next)
        if (next === null || next.settled) finish()
      } catch (cause) {
        if (isStale()) return
        setError(cause instanceof Error ? cause : new Error(String(cause)))
        finish()
      } finally {
        reading = false
        if (again) {
          again = false
          void read(id)
        }
      }
    }

    const id = outputId
    const realtime = getRealtime()
    void read(id)
    unfollow = realtime.subscribe(`print:${id}`, () => void read(id))
    const fallback = () => {
      timer = setTimeout(() => {
        if (finished || isStale()) return
        if (realtime.status === 'unavailable' || Date.now() - lastRead >= LIVE_BACKSTOP_MS) {
          void read(id)
        }
        fallback()
      }, POLL_MS)
    }
    fallback()

    return () => {
      stopped = true
      unfollow?.()
      if (timer) clearTimeout(timer)
    }
  }, [outputId, enabled])

  return { progress, error, polling }
}
