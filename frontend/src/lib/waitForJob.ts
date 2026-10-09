import { api } from '../api/client'
import type { Job } from '../api/types'
import { getRealtime, type RealtimeEvent } from './realtime'

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
  return followUntil(`job:${jobId}`, {
    ...opts,
    read: () => api.getJob(jobId),
    done: settled,
    // A step starting changes nothing a read would show.
    relevant: (event) => event.kind !== 'job.progress',
    onRead: opts.onJob,
    stillRunning: () => new JobStillRunning(jobId),
  })
}

/**
 * `read()` once `done` holds of it, reading once per signal on `topic` that
 * `relevant` admits (and on the subscription's confirmation, so a change made before
 * it is still seen), and on a `pollMs` timer only while the socket is unavailable.
 * `onRead` sees every read that is not done. An aborted `signal` rejects with its
 * reason; `waitMs` passing rejects with `stillRunning()`, after one read if none has
 * answered yet (waiting at most `pollMs` more for it); a failed read rejects with its error.
 */
export function followUntil<T>(
  topic: string,
  opts: {
    read: () => Promise<T>
    done: (value: T) => boolean
    relevant?: (event: RealtimeEvent) => boolean
    onRead?: ((value: T) => void) | undefined
    stillRunning: () => Error
    pollMs: number
    waitMs?: number | undefined
    signal?: AbortSignal | undefined
  },
): Promise<T> {
  const { signal } = opts
  return new Promise<T>((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason as Error)
      return
    }
    const realtime = getRealtime()
    let ended = false
    let reading = false
    let again = false
    /** A read has answered: until one has, the limit reads once before it gives up. */
    let answered = false
    let expired = false
    let poll: ReturnType<typeof setTimeout> | undefined
    /** Bounds that one look: fetch has no timeout, so a read can hang (#2045 review). */
    let grace: ReturnType<typeof setTimeout> | undefined
    const giveUp = () => end(() => reject(opts.stillRunning()))
    const limit =
      opts.waitMs === undefined
        ? undefined
        : setTimeout(() => {
            expired = true
            // A limit that passes before the first read (a caller's budget already
            // spent) still looks once (#2038), for at most `pollMs` more: the limit
            // stays a bound however long that read takes.
            if (answered) giveUp()
            else {
              grace = setTimeout(giveUp, opts.pollMs)
              if (!reading) void read()
            }
          }, opts.waitMs)

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
        const value = await opts.read()
        if (ended) return
        answered = true
        if (opts.done(value)) end(() => resolve(value))
        else {
          opts.onRead?.(value)
          if (expired) giveUp()
        }
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

    const unfollow = realtime.subscribe(topic, (event) => {
      if (event !== 'resync' && opts.relevant && !opts.relevant(event)) return
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
      clearTimeout(grace)
      signal?.removeEventListener('abort', onAbort)
      settle()
    }
  })
}
