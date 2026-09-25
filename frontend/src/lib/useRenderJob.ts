import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import type { Job } from '../api/types'
import type { ParamValues } from './params'

export const RENDER_DEBOUNCE_MS = 400
const POLL_MS = 400

export interface RenderState {
  /** The job currently being rendered, or the last one that finished. */
  job: Job | undefined
  /** True from submit until the job reaches `done` or `failed`. */
  rendering: boolean
  error: Error | undefined
}

/**
 * Submits a render for `params` and polls until it settles (spec §5.3: the preview
 * *is* the render). A newer submission supersedes an older one — its result is
 * dropped rather than shown out of order.
 */
export function useRenderJob(
  slug: string | undefined,
  params: ParamValues | undefined,
  /** #90 — render this revision rather than the one the model is currently at. */
  version?: string,
): RenderState {
  const [job, setJob] = useState<Job | undefined>(undefined)
  const [rendering, setRendering] = useState(false)
  const [error, setError] = useState<Error | undefined>(undefined)
  const generation = useRef(0)

  useEffect(() => {
    if (!slug || !params || Object.keys(params).length === 0) return

    const mine = ++generation.current
    let timer: ReturnType<typeof setTimeout> | undefined
    let stopped = false

    const isStale = () => stopped || generation.current !== mine

    setRendering(true)
    setError(undefined)

    async function poll(jobId: string) {
      try {
        const next = await api.getJob(jobId)
        if (isStale()) return
        setJob(next)
        if (next.status === 'done' || next.status === 'failed') {
          setRendering(false)
          return
        }
        timer = setTimeout(() => void poll(jobId), POLL_MS)
      } catch (cause) {
        if (isStale()) return
        setError(cause instanceof Error ? cause : new Error(String(cause)))
        setRendering(false)
      }
    }

    api
      .render(slug, params, version)
      .then(({ job_id }) => {
        if (isStale()) return
        void poll(job_id)
      })
      .catch((cause: unknown) => {
        if (isStale()) return
        setError(cause instanceof Error ? cause : new Error(String(cause)))
        setRendering(false)
      })

    return () => {
      stopped = true
      if (timer) clearTimeout(timer)
    }
  }, [slug, params, version])

  return { job, rendering, error }
}
