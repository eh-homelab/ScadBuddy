import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { NozzlePlan, PrintRunRequest, SlicePreview } from '../api/types'
import { sourceApi, sourceKey, type PrintSource } from './printSource'
import { useDebounced } from './useDebounced'
import { useLatest } from './useLatest'

/** How long the choices settle before they are resliced. */
export const RESLICE_DEBOUNCE_MS = 1500
/** How often a slice under way is read. */
export const RESLICE_POLL_MS = 2000

export interface PreviewSliceState {
  /** The newest finished slice for this source; kept while a newer one runs. */
  result: SlicePreview | null
  /** The nozzle plan the slice was made with. */
  plan: NozzlePlan | null
  /** A slice for the choices on screen is still on its way: `result` is for older ones. */
  stale: boolean
  /** Slicing now. */
  slicing: boolean
  error: string | null
}

interface Shown {
  source: string | undefined
  request: string
  result: SlicePreview
  plan: NozzlePlan | null
}

const FINISHED = new Set(['completed', 'failed', 'cancelled'])

/**
 * #2169 — the print dialog's automatic reslice. After the choices settle the run's own
 * path slices them in the background (`POST …/preview-slice`, nothing queued), and the
 * job is read until it finishes. The last result stays on screen, marked stale, while
 * the next one runs. `request: null` slices nothing.
 */
export function usePreviewSlice(source: PrintSource | undefined, request: PrintRunRequest | null): PreviewSliceState {
  const latest = useLatest(source)
  const own = sourceKey(source)
  const wanted = request === null ? null : JSON.stringify(request)
  const key = useDebounced(wanted, RESLICE_DEBOUNCE_MS)
  const [shown, setShown] = useState<Shown | null>(null)
  const [slicing, setSlicing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const attempt = useRef(0)

  useEffect(() => {
    const current = latest.current
    if (key === null || !current) return
    const token = ++attempt.current
    let timer: ReturnType<typeof setTimeout> | undefined
    const live = () => token === attempt.current
    setSlicing(true)
    setError(null)
    const follow = async (jobId: number, plan: NozzlePlan | null) => {
      const read = await api.getPreviewSlice(jobId)
      if (!live()) return
      if (!FINISHED.has(read.status)) {
        timer = setTimeout(() => void follow(jobId, plan).catch(fail), RESLICE_POLL_MS)
        return
      }
      setSlicing(false)
      if (read.status === 'completed') setShown({ source: own, request: key, result: read, plan })
      else setError(read.failure ?? 'The slice failed.')
    }
    const fail = (cause: unknown) => {
      if (!live()) return
      setSlicing(false)
      setError(cause instanceof ApiError ? cause.detail : 'The background slice could not run.')
    }
    sourceApi(current)
      .preview(JSON.parse(key) as PrintRunRequest)
      .then((started) => (live() ? follow(started.job_id, started.nozzle_plan ?? null) : undefined))
      .catch(fail)
    return () => {
      attempt.current += 1
      if (timer) clearTimeout(timer)
    }
  }, [own, key, latest])

  const mine = shown?.source === own ? shown : null
  return {
    result: mine?.result ?? null,
    plan: mine?.plan ?? null,
    stale: mine !== null && mine.request !== wanted,
    slicing: slicing && wanted !== null,
    error,
  }
}
