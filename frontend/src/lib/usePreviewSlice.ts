import { useCallback, useEffect, useRef, useState } from 'react'
import { ApiError, type Slicer } from '../api/client'
import type { NozzlePlan, PrintRunRequest, SlicePreview } from '../api/types'
import { sourceApi, sourceKey, type PrintSource } from './printSource'
import { getRealtime } from './realtime'
import { useDebounced } from './useDebounced'
import { useLatest } from './useLatest'

/** How long the choices settle before they are resliced. */
export const RESLICE_DEBOUNCE_MS = 1500
/** How often a slice under way is read while the realtime socket is unavailable (#265). */
export const RESLICE_POLL_MS = 2000

/**
 * #265 — the topic a background slice's changes come on: the backend follows the slice
 * from each read of it and publishes `preview_slice.*` there (`preview_follow/follows.py`).
 */
export function previewSliceTopic(slicer: Slicer, jobId: number): string {
  return `preview-slice:${slicer}:${jobId}`
}

export interface PreviewSliceState {
  /** The newest finished slice for this source; kept while a newer one runs. */
  result: SlicePreview | null
  /**
   * #2714 review — the plate `result` was sliced for. A stale result can be another
   * plate's, so a caller matches this, not the plate on screen.
   */
  plateId: number | null
  /** The nozzle plan the slice was made with. */
  plan: NozzlePlan | null
  /** A slice for the choices on screen is still on its way: `result` is for older ones. */
  stale: boolean
  /** Slicing now. */
  slicing: boolean
  /**
   * How far the slice under way has got, in percent: the native slicer's, from each read
   * (plan 2026-10-16 Task 14). `null` when it is not slicing, or does not say (Bambuddy's).
   */
  progress: number | null
  /** `result`'s toolpath, read from the slicer that made it; `null` with no result. */
  toolpathUrl: string | null
  error: string | null
  /**
   * #4433 — slice the choices on screen again, with no finished slice reused. `result`
   * stays, marked stale, until the new one lands.
   */
  reslice: () => void
  /** Whether `reslice` can start: there are choices, they have settled and nothing slices. */
  canReslice: boolean
  /** A slice that `reslice` started is under way. */
  reslicing: boolean
}

interface Shown {
  source: string | undefined
  request: string
  result: SlicePreview
  plan: NozzlePlan | null
  /** The slicer its start answered with, which every read of it names. */
  slicer: Slicer
  /**
   * #4433 review — a Reslice of it failed. It stays on screen, marked stale, until a later
   * slice completes: the person asked for new figures and has none.
   */
  superseded?: boolean
}

const FINISHED = new Set(['completed', 'failed', 'cancelled'])

/**
 * #2169 — the print dialog's automatic reslice. After the choices settle the run's own
 * path slices them in the background (`POST …/preview-slice`, nothing queued), and the
 * job is read until it finishes. The last result stays on screen, marked stale, while
 * the next one runs. `request: null` slices nothing.
 *
 * #265 — the job is read once now and once per signal on its topic
 * (`previewSliceTopic`), one read at a time. It is read on a timer only while the
 * realtime socket is unavailable, or while the last read said the backend does not
 * follow the slice (`followed: false`).
 */
export function usePreviewSlice(source: PrintSource | undefined, request: PrintRunRequest | null): PreviewSliceState {
  const latest = useLatest(source)
  const own = sourceKey(source)
  const wanted = request === null ? null : JSON.stringify(request)
  const key = useDebounced(wanted, RESLICE_DEBOUNCE_MS)
  const [shown, setShown] = useState<Shown | null>(null)
  const [slicing, setSlicing] = useState(false)
  const [progress, setProgress] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [reslicing, setReslicing] = useState(false)
  // #4433 — each Reslice bumps `forced`; the run that sees a new value asks for a fresh slice.
  const [forced, setForced] = useState(0)
  const used = useRef(0)
  const attempt = useRef(0)

  useEffect(() => {
    const current = latest.current
    if (key === null || !current) return
    const fresh = forced !== used.current
    used.current = forced
    const token = ++attempt.current
    let timer: ReturnType<typeof setTimeout> | undefined
    let unfollow: (() => void) | undefined
    const live = () => token === attempt.current
    const supersede = () => {
      if (fresh) setShown((old) => (old ? { ...old, superseded: true } : old))
    }
    setSlicing(true)
    setReslicing(fresh)
    setProgress(null)
    setError(null)
    const follow = (jobId: number, plan: NozzlePlan | null, slicer: Slicer) => {
      const realtime = getRealtime()
      let ended = false
      let reading = false
      let again = false
      // #265 review: the backend follows at most so many slices at once; a read past that
      // says `followed: false`, and no event will come, so the timer reads instead.
      let unfollowed = false
      const end = () => {
        ended = true
        unfollow?.()
        unfollow = undefined
        if (timer) clearTimeout(timer)
      }
      const read = async () => {
        if (ended || !live()) return
        if (reading) {
          again = true
          return
        }
        reading = true
        try {
          const answer = await sourceApi(current).readPreview(jobId, slicer)
          if (ended || !live()) return
          if (!FINISHED.has(answer.status)) {
            unfollowed = answer.followed === false
            setProgress(answer.progress ?? null)
            return
          }
          end()
          setSlicing(false)
          setReslicing(false)
          setProgress(null)
          if (answer.status === 'completed') setShown({ source: own, request: key, result: answer, plan, slicer })
          else {
            supersede()
            setError(answer.failure ?? 'The slice failed.')
          }
        } catch (cause) {
          if (ended) return
          end()
          fail(cause)
        } finally {
          reading = false
          if (again) {
            again = false
            void read()
          }
        }
      }
      unfollow = realtime.subscribe(previewSliceTopic(slicer, jobId), () => void read())
      // An open socket confirms the subscription within a round trip, which reads.
      if (!realtime.willResync) void read()
      const fallback = () => {
        timer = setTimeout(() => {
          if (ended || !live()) return
          if (unfollowed || realtime.status === 'unavailable') void read()
          fallback()
        }, RESLICE_POLL_MS)
      }
      fallback()
    }
    const fail = (cause: unknown) => {
      if (!live()) return
      setSlicing(false)
      setReslicing(false)
      setProgress(null)
      supersede()
      setError(cause instanceof ApiError ? cause.detail : 'The background slice could not run.')
    }
    sourceApi(current)
      .preview(JSON.parse(key) as PrintRunRequest, fresh)
      .then((started) => {
        if (live()) follow(started.job_id, started.nozzle_plan ?? null, started.slicer ?? 'bambuddy')
      })
      .catch(fail)
    return () => {
      attempt.current += 1
      unfollow?.()
      if (timer) clearTimeout(timer)
    }
  }, [own, key, latest, forced])
  const reslice = useCallback(() => setForced((count) => count + 1), [])

  const mine = shown?.source === own ? shown : null
  const toolpathUrl = mine && source ? sourceApi(source).toolpathUrl(mine.result.job_id, mine.slicer) : null
  return {
    result: mine?.result ?? null,
    plateId: mine ? ((JSON.parse(mine.request) as PrintRunRequest).plate_id ?? 1) : null,
    plan: mine?.plan ?? null,
    // A Reslice marks the shown slice stale until the new one lands, and a failed one
    // until a later slice completes (#4433).
    stale: mine !== null && (mine.request !== wanted || (reslicing && slicing) || mine.superseded === true),
    slicing: slicing && wanted !== null,
    // Only the slice of the choices on screen: while new ones wait out the debounce, the
    // old slice's percentage would say how far the wrong slice got (review M5).
    progress: slicing && wanted !== null && wanted === key ? progress : null,
    toolpathUrl,
    error,
    reslice,
    canReslice: wanted !== null && wanted === key && !slicing && source !== undefined,
    reslicing: reslicing && slicing && wanted !== null,
  }
}
