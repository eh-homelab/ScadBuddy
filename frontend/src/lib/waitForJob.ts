import { api } from '../api/client'
import type { Job } from '../api/types'
import { getRealtime } from './realtime'

/** The wait's limit passed with the job still going. */
export class JobStillRunning extends Error {
  constructor(jobId: string) {
    super(`job ${jobId} is still running`)
    this.name = 'JobStillRunning'
  }
}

function settled(job: Job): boolean {
  return job.status === 'done' || job.status === 'failed' || job.status === 'cancelled'
}

/**
 * #1909 — the job once it has settled (`done`, `failed` or `cancelled`), followed as
 * `useRenderJob` follows one (#267): read once per signal on `job:<id>` (the
 * subscription's confirmation is the first read, so a job that settled before it was
 * followed is still seen), and on a `pollMs` timer only while the socket is
 * unavailable. `onJob` sees every read that has not settled. An aborted `signal`
 * rejects with its reason; `waitMs` passing rejects with `JobStillRunning`; a failed
 * read rejects with its error.
 */
export function waitForJob(
  jobId: string,
  opts: { pollMs: number; waitMs?: number; signal?: AbortSignal; onJob?: (job: Job) => void },
): Promise<Job> {
  const { signal } = opts
  return new Promise<Job>((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason as Error)
      return
    }
    const realtime = getRealtime()
    let ended = false
    let reading = false
    let again = false
    let poll: ReturnType<typeof setTimeout> | undefined
    const limit =
      opts.waitMs === undefined ? undefined : setTimeout(() => end(() => reject(new JobStillRunning(jobId))), opts.waitMs)

    const onAbort = () => end(() => reject(signal?.reason as Error))
    signal?.addEventListener('abort', onAbort, { once: true })

    const read = async () => {
      if (ended) return
      if (reading) {
        again = true
        return
      }
      reading = true
      try {
        const job = await api.getJob(jobId)
        if (ended) return
        if (settled(job)) end(() => resolve(job))
        else opts.onJob?.(job)
      } catch (cause) {
        end(() => reject(cause as Error))
      } finally {
        reading = false
        if (again) {
          again = false
          void read()
        }
      }
    }

    const unfollow = realtime.subscribe(`job:${jobId}`, (event) => {
      // A step starting changes nothing a read would show.
      if (event !== 'resync' && event.kind === 'job.progress') return
      void read()
    })
    const fallback = () => {
      poll = setTimeout(() => {
        if (realtime.status === 'unavailable') void read()
        fallback()
      }, opts.pollMs)
    }
    fallback()

    function end(settle: () => void) {
      if (ended) return
      ended = true
      unfollow()
      clearTimeout(poll)
      clearTimeout(limit)
      signal?.removeEventListener('abort', onAbort)
      settle()
    }
  })
}
