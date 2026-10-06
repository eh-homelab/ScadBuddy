import { useEffect, useRef, useState } from 'react'
import { ApiError, api } from '../api/client'
import type { Job } from '../api/types'
import { joinInputs, NO_EXTRA, type InputsExtra } from './inputs'
import type { ParamValues } from './params'
import { getRealtime } from './realtime'
import { useLatest } from './useLatest'

export const RENDER_DEBOUNCE_MS = 400
/** Only while the realtime socket is unavailable (#267): otherwise events drive the reads. */
const POLL_MS = 400

/** A render's steps, as `job.progress` names them (backend `core/metrics.py` `RenderStage`). */
export type RenderStage = 'source' | 'render' | 'split' | 'solids' | 'thumbnail' | 'write'

const STAGES: readonly string[] = ['source', 'render', 'split', 'solids', 'thumbnail', 'write']

/** A submit this hook made: what it rendered, and the job id it was answered with. */
interface Submission {
  slug: string
  version: string | undefined
  /** Undefined when the submit failed: there is nothing to supersede. */
  jobId: Promise<string | undefined>
}

export interface RenderState {
  /** The job currently being rendered, or the last one that finished. */
  job: Job | undefined
  /** True from submit until the job reaches `done`, `failed` or `cancelled`. */
  rendering: boolean
  error: Error | undefined
  /**
   * #254 — the exact `params` object the settled `job` (or `error`) answers, by identity.
   * Between a new submission's commit and its `rendering` flag, `job` is still the
   * previous one; this is how a caller waiting on "the render of these values" tells.
   * Only the newest submission sets it: a superseded one is stale before it settles.
   */
  settledFor: ParamValues | undefined
  /**
   * Seconds until the submit is tried again, while the server's render queue is
   * full (503 with `retry_after`, only when SCADBUDDY_RENDER_QUEUE_MAX is set).
   * Not an error: the preview is still coming.
   */
  busy: number | undefined
  /** #267 — the step the current render is on, while it is running and the socket says. */
  stage: RenderStage | undefined
}

/** How long a refused render asks to wait: only a queue-full 503 carries it. */
function retryAfterSeconds(cause: unknown): number | undefined {
  if (!(cause instanceof ApiError) || cause.status !== 503) return undefined
  const seconds = cause.problem['retry_after']
  return typeof seconds === 'number' && seconds > 0 ? seconds : undefined
}

const STALE_CHECK_MS = 250

/**
 * Submits a render for `params` and follows it until it settles (spec §5.3: the preview
 * *is* the render). #267: the job is followed on the realtime socket (`job:<id>`) and
 * read once per event; `GET /jobs/:id` is polled only while the socket is unavailable. A newer submission supersedes an older one — its result is
 * dropped rather than shown out of order, and the server is told: each submit names the
 * previous one of the same model and revision as the job it `supersedes`, which the
 * server drops if no worker has started it.
 */
export function useRenderJob(
  slug: string | undefined,
  params: ParamValues | undefined,
  /** #90 — render this revision rather than the one the model is currently at. */
  version?: string,
  /** A template UI's state (spec 2026-09-27 §4.3), sent with the params; alone it never re-renders. */
  extra: InputsExtra = NO_EXTRA,
): RenderState {
  const [job, setJob] = useState<Job | undefined>(undefined)
  const [rendering, setRendering] = useState(false)
  const [error, setError] = useState<Error | undefined>(undefined)
  const [settledFor, setSettledFor] = useState<ParamValues | undefined>(undefined)
  const [busy, setBusy] = useState<number | undefined>(undefined)
  const [stage, setStage] = useState<RenderStage | undefined>(undefined)
  const generation = useRef(0)
  const last = useRef<Submission | undefined>(undefined)
  const extraRef = useLatest(extra)

  useEffect(() => {
    // An empty `params` is a model with no customizer parameters (#941): its defaults
    // are still a render. A caller with nothing to render yet passes undefined.
    if (!slug || !params) return

    const mine = ++generation.current
    let timer: ReturnType<typeof setTimeout> | undefined
    let unfollow: (() => void) | undefined
    let stopped = false

    const isStale = () => stopped || generation.current !== mine

    setRendering(true)
    setStage(undefined)
    setError(undefined)
    setBusy(undefined)

    /** Wait out a refusal, but give up as soon as a newer submit supersedes this one. */
    async function waitUnlessStale(seconds: number) {
      const until = Date.now() + seconds * 1000
      while (Date.now() < until && !isStale()) {
        await new Promise((resolve) => setTimeout(resolve, STALE_CHECK_MS))
      }
    }

    /** Reads the job on every signal for it until it settles; one read at a time. */
    function follow(jobId: string) {
      const realtime = getRealtime()
      let reading = false
      let again = false
      let settled = false

      const finish = () => {
        settled = true
        setStage(undefined)
        unfollow?.()
        unfollow = undefined
        if (timer) clearTimeout(timer)
        setRendering(false)
        setSettledFor(params)
      }

      const read = async () => {
        if (settled || isStale()) return
        if (reading) {
          again = true
          return
        }
        reading = true
        try {
          const next = await api.getJob(jobId)
          if (isStale()) return
          setJob(next)
          if (next.status === 'done' || next.status === 'failed' || next.status === 'cancelled') finish()
        } catch (cause) {
          if (isStale()) return
          setError(cause instanceof Error ? cause : new Error(String(cause)))
          finish()
        } finally {
          reading = false
          if (again) {
            again = false
            void read()
          }
        }
      }

      // The subscription's confirmation is the first read, so a job that settled
      // before it was followed is still seen.
      unfollow = realtime.subscribe(`job:${jobId}`, (signal) => {
        // A step starting changes nothing a read would show: take it from the event.
        if (signal !== 'resync' && signal.kind === 'job.progress') {
          const next = signal.data['stage']
          if (!isStale() && typeof next === 'string' && STAGES.includes(next)) {
            setStage(next as RenderStage)
          }
          return
        }
        void read()
      })
      const fallback = () => {
        timer = setTimeout(() => {
          if (settled || isStale()) return
          if (realtime.status === 'unavailable') void read()
          fallback()
        }, POLL_MS)
      }
      fallback()
    }

    // Awaited even when the previous effect has gone stale by the time its answer
    // arrives: its job exists either way, and this is the only submit that can name
    // it. Never across models or revisions -- that job is someone else's to keep.
    const prior = last.current
    const supersedable = prior && prior.slug === slug && prior.version === version
    const submitted = (async () => {
      const supersedes = supersedable ? await prior.jobId : undefined
      // A full queue is transient ("about one render"): retry after the delay it
      // names rather than showing a failure. A refused submit created no job, so
      // the same `supersedes` still applies.
      for (;;) {
        try {
          const { job_id } = await api.render(slug, joinInputs(params, extraRef.current), version, supersedes)
          if (!isStale()) setBusy(undefined)
          return job_id
        } catch (cause) {
          const wait = retryAfterSeconds(cause)
          if (wait === undefined || isStale()) throw cause
          setBusy(wait)
          await waitUnlessStale(wait)
          if (isStale()) throw cause
        }
      }
    })()
    last.current = { slug, version, jobId: submitted.catch(() => undefined) }

    submitted
      .then((job_id) => {
        if (isStale()) return
        follow(job_id)
      })
      .catch((cause: unknown) => {
        if (isStale()) return
        setBusy(undefined)
        setError(cause instanceof Error ? cause : new Error(String(cause)))
        setRendering(false)
        setSettledFor(params)
      })

    return () => {
      stopped = true
      unfollow?.()
      if (timer) clearTimeout(timer)
      // The job is no longer followed, so its last step is no longer news: the
      // preview must not name it through the debounce before the next submit.
      setStage(undefined)
    }
  }, [slug, params, version, extraRef])

  return { job, rendering, error, busy, settledFor, stage }
}
