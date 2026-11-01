import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { ApiError, OFFLINE, STILL_ACCEPTING, TEMPORAL_UNAVAILABLE, UNANSWERED, api } from '../api/client'
import type { Job, RenderAccepted } from '../api/types'
import { fakeRealtime } from './realtime.fake'
import {
  QUEUE_READ_MS,
  READ_RETRIES,
  RENDER_AUTO_RETRIES,
  RENDER_AUTO_RETRY_MAX_MS,
  RENDER_AUTO_RETRY_MS,
  SNAPSHOT_PENDING_MAX_SECONDS,
  TRANSIENT_RETRIES,
  canRetry,
  useRenderAutoRetry,
  useRenderJob,
} from './useRenderJob'

const JOB_A = 'a'.repeat(32)
const JOB_B = 'b'.repeat(32)
const JOB_C = 'c'.repeat(32)

function job(id: string, status: Job['status']): Job {
  return {
    id,
    slug: 'demo',
    status,
    created_at: '2026-09-27T00:00:00Z',
    params: {},
    log_tail: [],
  } as Job
}

function accepted(id: string): RenderAccepted {
  return { job_id: id, status_url: `/api/v1/jobs/${id}` }
}

/** A submit this test answers by hand, so its answer can land after the next one. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

type Props = { slug: string; params: Record<string, number> | undefined; version?: string }

function mount(initialProps: Props) {
  return renderHook(({ slug, params, version }: Props) => useRenderJob(slug, params, version), {
    initialProps,
  })
}

describe('useRenderJob', () => {
  let submit: MockInstance<typeof api.render>
  let realtime: ReturnType<typeof fakeRealtime>

  beforeEach(() => {
    vi.useFakeTimers()
    realtime = fakeRealtime()
    let next = 0
    const ids = [JOB_A, JOB_B, JOB_C]
    submit = vi.spyOn(api, 'render').mockImplementation(async () => accepted(ids[next++]!))
    vi.spyOn(api, 'getJob').mockImplementation(async (id) => job(id, 'pending'))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('cancel withdraws the job it follows, and the render settles cancelled (#4968)', async () => {
    let status: Job['status'] = 'running'
    vi.spyOn(api, 'getJob').mockImplementation(async (id) => job(id, status))
    const withdraw = vi.spyOn(api, 'withdrawJob').mockImplementation(async (id) => {
      status = 'cancelled'
      return job(id, 'cancelled')
    })
    const { result } = mount({ slug: 'demo', params: { w: 1 } })
    await settle()
    expect(result.current.rendering).toBe(true)
    await act(async () => {
      await result.current.cancel()
    })
    await settle()
    expect(withdraw).toHaveBeenCalledWith(JOB_A)
    expect(result.current.job?.status).toBe('cancelled')
    expect(result.current.rendering).toBe(false)
    // Settled: nothing left to withdraw.
    await act(async () => {
      await result.current.cancel()
    })
    expect(withdraw).toHaveBeenCalledTimes(1)
  })

  it('drops every hold on a withdrawn job another caller still renders (review of #5119)', async () => {
    vi.spyOn(api, 'getJob').mockImplementation(async (id) => job(id, 'running'))
    // Another claimant keeps it running: the withdrawal took only this page's claim.
    const withdraw = vi.spyOn(api, 'withdrawJob').mockImplementation(async (id) => job(id, 'running'))
    const { result, rerender } = mount({ slug: 'demo', params: { w: 1 } })
    await settle()
    let withdrawn: string | undefined
    await act(async () => {
      withdrawn = await result.current.cancel()
    })
    await settle()
    expect(withdrawn).toBe(JOB_A)
    // Not followed any more: no Cancel to press, and a second one sends nothing.
    expect(result.current.rendering).toBe(false)
    await act(async () => {
      await result.current.cancel()
    })
    expect(withdraw).toHaveBeenCalledTimes(1)
    // The next render supersedes nothing: the withdrawn job's claims are others'.
    rerender({ slug: 'demo', params: { w: 2 } })
    await settle()
    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit.mock.calls[1]?.[3]).toBeUndefined()
  })

  it('submits inputs: the params plus the UI state', async () => {
    const params = { name: 'Hi' }
    const extra = { tab: 'lid' }
    renderHook(() => useRenderJob('name-keychain', params, undefined, extra))
    await settle()
    expect(submit).toHaveBeenCalledWith(
      'name-keychain',
      { params: { name: 'Hi' }, tab: 'lid' },
      undefined,
      undefined,
      expect.any(AbortSignal),
      expect.any(String),
    )
  })

  it('tells the server which render a new one replaces', async () => {
    const { rerender } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    rerender({ slug: 'demo', params: { n: 2 } })
    await settle()

    expect(submit).toHaveBeenNthCalledWith(1, 'demo', { params: { n: 1 } }, undefined, undefined, expect.any(AbortSignal), expect.any(String))
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { params: { n: 2 } }, undefined, JOB_A, expect.any(AbortSignal), expect.any(String))
  })

  it('supersedes a render whose answer arrives after the next one was asked for', async () => {
    const first = deferred<RenderAccepted>()
    submit.mockImplementationOnce(() => first.promise)
    const { rerender } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    rerender({ slug: 'demo', params: { n: 2 } })
    await settle()
    // The second submit waits for the first's job id rather than going without it.
    expect(submit).toHaveBeenCalledTimes(1)

    first.resolve(accepted(JOB_A))
    await settle()

    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { params: { n: 2 } }, undefined, JOB_A, expect.any(AbortSignal), expect.any(String))
  })

  it("aborts a superseded render's re-sends, never the request in flight (review #1066 2.2)", async () => {
    const first = deferred<RenderAccepted>()
    submit.mockImplementationOnce(() => first.promise)
    const { rerender } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    const signal = submit.mock.calls[0]![4]!
    expect(signal.aborted).toBe(false)

    rerender({ slug: 'demo', params: { n: 2 } })
    await settle()
    expect(signal.aborted).toBe(true)

    // Its answer still arrives, and names the job the next render supersedes.
    first.resolve(accepted(JOB_A))
    await settle()
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { params: { n: 2 } }, undefined, JOB_A, expect.any(AbortSignal), expect.any(String))
  })

  it('re-sends a render the server is still accepting with its key, so it stays one claim (review #1066 (7) 3)', async () => {
    const accepting = new ApiError({
      type: STILL_ACCEPTING,
      title: 'Service Unavailable',
      status: 503,
      detail: 'ScadBuddy is still checking this request. Send it again to follow it.',
      retry_after: 2,
    })
    submit.mockRejectedValueOnce(accepting)
    mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    expect(submit).toHaveBeenCalledTimes(2)
    const key = submit.mock.calls[0]![5]
    expect(key).toEqual(expect.any(String))
    expect(submit.mock.calls[1]![5]).toBe(key)
  })

  it('re-sends a render Temporal could not take with its key: a start may exist (review #1066 (8) 3)', async () => {
    // As the client reads it: the 503's Retry-After header becomes `retry_after`.
    submit.mockRejectedValueOnce(
      new ApiError({
        type: TEMPORAL_UNAVAILABLE,
        title: 'Service Unavailable',
        status: 503,
        detail: 'Temporal could not start this render right now.',
        retry_after: 5,
      }),
    )
    mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })

    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit.mock.calls[1]![5]).toBe(submit.mock.calls[0]![5])
  })

  it('retries a full queue under a new key: the refusal is all its key was answered', async () => {
    submit.mockRejectedValueOnce(
      new ApiError({ title: 'Service Unavailable', status: 503, detail: 'the render queue is full', retry_after: 3 }),
    )
    mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit.mock.calls[1]![5]).toEqual(expect.any(String))
    expect(submit.mock.calls[1]![5]).not.toBe(submit.mock.calls[0]![5])
  })

  it('retries a render the full queue refused, after the delay it names', async () => {
    const full = new ApiError({
      title: 'Service Unavailable',
      status: 503,
      detail: 'the render queue is full (16 jobs waiting for a worker); try again in 3 s',
      retry_after: 3,
    })
    submit.mockRejectedValueOnce(full)
    const { result } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()

    expect(result.current.busy).toEqual({ seconds: 3, reason: 'queue-full' })
    expect(result.current.error).toBeUndefined()
    expect(result.current.rendering).toBe(true)
    expect(submit).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { params: { n: 1 } }, undefined, undefined, expect.any(AbortSignal), expect.any(String))
    expect(result.current.busy).toBeUndefined()
    expect(result.current.error).toBeUndefined()
  })

  it('says a snapshot still uploading is why it waits, and waits it out as long as it lasts (#1422)', async () => {
    const pending = () =>
      new ApiError({
        title: 'Service Unavailable',
        status: 503,
        detail: "demo@abc: the revision's snapshot is still uploading",
        retry_after: 2,
        code: 'snapshot_pending',
      })
    // More refusals than a transient outage is allowed: an upload is waited out, as a full queue is.
    for (let i = 0; i < TRANSIENT_RETRIES + 2; i++) submit.mockRejectedValueOnce(pending())
    const { result } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()

    expect(result.current.busy).toEqual({ seconds: 2, reason: 'snapshot-pending' })
    for (let i = 0; i < TRANSIENT_RETRIES + 2; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
    }
    expect(submit).toHaveBeenCalledTimes(TRANSIENT_RETRIES + 3)
    expect(result.current.error).toBeUndefined()
    expect(result.current.busy).toBeUndefined()
  })

  it('gives up on a snapshot that never finishes uploading, as an error it can try again (#2101)', async () => {
    submit.mockRejectedValue(
      new ApiError({
        title: 'Service Unavailable',
        status: 503,
        detail: "demo@abc: the revision's snapshot is still uploading",
        retry_after: 2,
        code: 'snapshot_pending',
      }),
    )
    const { result } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    const waits = SNAPSHOT_PENDING_MAX_SECONDS / 2
    for (let i = 0; i < waits + 5; i++) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2000)
      })
    }

    expect(submit).toHaveBeenCalledTimes(waits + 1)
    expect(result.current.busy).toBeUndefined()
    expect(result.current.rendering).toBe(false)
    expect(result.current.error?.message).toBe("demo@abc: the revision's snapshot is still uploading")
    expect(canRetry(result.current.error)).toBe(true)

    submit.mockResolvedValueOnce(accepted(JOB_B))
    act(() => result.current.retry())
    await settle()
    expect(submit).toHaveBeenCalledTimes(waits + 2)
    expect(result.current.error).toBeUndefined()
  })

  it('says why it waits: an unreachable render service, or a request still accepting', async () => {
    submit.mockRejectedValueOnce(
      new ApiError({
        type: TEMPORAL_UNAVAILABLE,
        title: 'Service Unavailable',
        status: 503,
        detail: 'Temporal is unavailable',
        retry_after: 5,
      }),
    )
    submit.mockRejectedValueOnce(
      new ApiError({
        type: STILL_ACCEPTING,
        title: 'Service Unavailable',
        status: 503,
        detail: 'still accepting',
        retry_after: 2,
      }),
    )
    const { result } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    expect(result.current.busy).toEqual({ seconds: 5, reason: 'temporal-unavailable' })

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000)
    })
    expect(result.current.busy).toEqual({ seconds: 2, reason: 'still-accepting' })
    expect(result.current.error).toBeUndefined()
  })

  it.each([TEMPORAL_UNAVAILABLE, STILL_ACCEPTING])(
    'gives up on a %s 503 after a bounded number of retries, as an error it can try again (review #1066 (11) 1)',
    async (type) => {
      submit.mockRejectedValue(
        new ApiError({ type, title: 'Service Unavailable', status: 503, detail: 'Temporal is down', retry_after: 5 }),
      )
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      for (let i = 0; i < TRANSIENT_RETRIES + 2; i++) {
        await act(async () => {
          await vi.advanceTimersByTimeAsync(5000)
        })
      }

      expect(submit).toHaveBeenCalledTimes(TRANSIENT_RETRIES + 1)
      expect(result.current.busy).toBeUndefined()
      expect(result.current.error?.message).toBe('Temporal is down')
      expect(result.current.rendering).toBe(false)

      submit.mockResolvedValueOnce(accepted(JOB_B))
      act(() => result.current.retry())
      await settle()
      expect(submit).toHaveBeenCalledTimes(TRANSIENT_RETRIES + 2)
      expect(result.current.error).toBeUndefined()
    },
  )

  it('waits out a refusal in elapsed time, not on a wall clock that steps (#1485)', async () => {
    submit.mockRejectedValueOnce(
      new ApiError({ title: 'Service Unavailable', status: 503, detail: 'full', retry_after: 3 }),
    )
    mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    vi.setSystemTime(Date.now() + 3_600_000)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect(submit).toHaveBeenCalledTimes(1)
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(submit).toHaveBeenCalledTimes(2)
  })

  it('treats any other refusal as an error, not a wait', async () => {
    submit.mockRejectedValueOnce(
      new ApiError({ title: 'Service Unavailable', status: 503, detail: 'openscad is not available' }),
    )
    const { result } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()

    expect(result.current.busy).toBeUndefined()
    expect(result.current.error?.message).toBe('openscad is not available')
    expect(result.current.rendering).toBe(false)
  })

  it('stops waiting out a refusal once a newer render supersedes it', async () => {
    submit.mockRejectedValueOnce(
      new ApiError({ title: 'Service Unavailable', status: 503, detail: 'full', retry_after: 30 }),
    )
    const { rerender } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    rerender({ slug: 'demo', params: { n: 2 } })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500)
    })

    // The newer render went out within a stale-check, not after the 30 s wait,
    // and the refused one was never retried.
    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { params: { n: 2 } }, undefined, undefined, expect.any(AbortSignal), expect.any(String))
  })

  it('reports a settle only for the newest render, never a superseded one (#254)', async () => {
    const first = { n: 1 }
    const second = { n: 2 }
    // The superseded job's status answer is held back until after the next submit.
    const lateA = deferred<Job>()
    const poll = vi.mocked(api.getJob)
    poll.mockImplementationOnce(() => lateA.promise)

    const { result, rerender } = mount({ slug: 'demo', params: first })
    await settle()
    rerender({ slug: 'demo', params: second })
    await settle()
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { params: second }, undefined, JOB_A, expect.any(AbortSignal), expect.any(String))

    // A says "done" now, but it was superseded: neither its job nor its params count.
    lateA.resolve(job(JOB_A, 'done'))
    await settle()
    expect(result.current.settledFor).toBeUndefined()
    expect(result.current.job?.id).not.toBe(JOB_A)
    expect(result.current.rendering).toBe(true)

    poll.mockImplementation(async (id) => job(id, 'done'))
    await realtime.signal(`job:${JOB_B}`)
    expect(result.current.job?.id).toBe(JOB_B)
    expect(result.current.rendering).toBe(false)
    // By identity: the caller compares it to the exact values object it rendered.
    expect(result.current.settledFor).toBe(second)
  })

  it('keeps jobFor the values the job on show ran with when a submit fails before any job (#1685)', async () => {
    const first = { n: 1 }
    const second = { n: 2 }
    vi.mocked(api.getJob).mockImplementation(async (id) => job(id, 'done'))
    const { result, rerender } = mount({ slug: 'demo', params: first })
    await settle()
    await realtime.signal(`job:${JOB_A}`)
    expect(result.current.job?.id).toBe(JOB_A)
    expect(result.current.jobFor).toBe(first)

    // The next submit is refused for good: no job, so the previous one stays on show.
    submit.mockRejectedValueOnce(
      new ApiError({ title: 'Internal Server Error', status: 500, detail: 'the render could not be started' }),
    )
    rerender({ slug: 'demo', params: second })
    await settle()
    expect(result.current.error).toBeDefined()
    expect(result.current.settledFor).toBe(second)
    expect(result.current.job?.id).toBe(JOB_A)
    // What JOB_A's colours belong to: not the values that never rendered.
    expect(result.current.jobFor).toBe(first)
  })

  it('never supersedes a render of another model or revision', async () => {
    const { rerender } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    rerender({ slug: 'other', params: { n: 1 } })
    await settle()
    rerender({ slug: 'other', params: { n: 1 }, version: 'f'.repeat(40) })
    await settle()

    expect(submit).toHaveBeenNthCalledWith(2, 'other', { params: { n: 1 } }, undefined, undefined, expect.any(AbortSignal), expect.any(String))
    expect(submit).toHaveBeenNthCalledWith(3, 'other', { params: { n: 1 } }, 'f'.repeat(40), undefined, expect.any(AbortSignal), expect.any(String))
  })

  describe('following a job (#267)', () => {
    it('reads a running job when the subscription is confirmed and on each event, never on a timer', async () => {
      const read = vi.mocked(api.getJob)
      read.mockImplementation(async (id) => job(id, 'running'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(realtime.following()).toEqual([`job:${JOB_A}`])
      expect(read).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      expect(read).toHaveBeenCalledTimes(1)

      await realtime.signal(`job:${JOB_A}`)
      expect(read).toHaveBeenCalledTimes(2)
      expect(result.current.job?.status).toBe('running')

      read.mockImplementation(async (id) => job(id, 'done'))
      await realtime.signal(`job:${JOB_A}`)
      expect(result.current.rendering).toBe(false)
      expect(result.current.job?.status).toBe('done')
    })

    it('reads a job waiting for a worker again every few seconds, for its queue place (#1638)', async () => {
      const read = vi.mocked(api.getJob)
      read.mockImplementation(async (id) => ({ ...job(id, 'pending'), queue_position: 3 }))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(read).toHaveBeenCalledTimes(1)
      expect(result.current.job?.queue_position).toBe(3)

      // No event says another job started: the place moves only on a read.
      read.mockImplementation(async (id) => ({ ...job(id, 'pending'), queue_position: 1 }))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(QUEUE_READ_MS + 500)
      })
      expect(read).toHaveBeenCalledTimes(2)
      expect(result.current.job?.queue_position).toBe(1)

      // Once a worker has it, its events are enough.
      read.mockImplementation(async (id) => job(id, 'running'))
      await realtime.signal(`job:${JOB_A}`)
      const reads = read.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(QUEUE_READ_MS * 3)
      })
      expect(read).toHaveBeenCalledTimes(reads)
    })

    it('shows the step a job.progress names without reading the job, and clears it on settle', async () => {
      const read = vi.mocked(api.getJob)
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      const reads = read.mock.calls.length
      await realtime.signal(`job:${JOB_A}`, 'job.progress', { stage: 'solids' })
      expect(result.current.stage).toBe('solids')
      await realtime.signal(`job:${JOB_A}`, 'job.progress', { stage: 'not-a-stage' })
      expect(result.current.stage).toBe('solids')
      expect(read.mock.calls.length).toBe(reads)

      read.mockImplementation(async (id) => job(id, 'done'))
      await realtime.signal(`job:${JOB_A}`)
      expect(result.current.stage).toBeUndefined()
    })

    it('drops the step when the params change and the job is no longer followed', async () => {
      const { result, rerender } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      await realtime.signal(`job:${JOB_A}`, 'job.progress', { stage: 'solids' })
      expect(result.current.stage).toBe('solids')
      // Mid-typing: CustomizePage passes no params until the debounce settles.
      rerender({ slug: 'demo', params: undefined })
      expect(result.current.stage).toBeUndefined()
    })

    it('stops following a job once it settles', async () => {
      vi.mocked(api.getJob).mockImplementation(async (id) => job(id, 'failed'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(result.current.rendering).toBe(false)
      expect(realtime.following()).toEqual([])
    })

    it('stops following a job once it is cancelled, same as a failure', async () => {
      vi.mocked(api.getJob).mockImplementation(async (id) => job(id, 'cancelled'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(result.current.rendering).toBe(false)
      expect(result.current.job?.status).toBe('cancelled')
      expect(realtime.following()).toEqual([])
    })

    it('stops following a job when the view goes', async () => {
      const { unmount } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(realtime.following()).toEqual([`job:${JOB_A}`])
      unmount()
      expect(realtime.following()).toEqual([])
    })

    it('reads one at a time, and once more for events that came during a read', async () => {
      const read = vi.mocked(api.getJob)
      mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      const slow = deferred<Job>()
      read.mockImplementationOnce(() => slow.promise)
      await realtime.signal(`job:${JOB_A}`)
      await realtime.signal(`job:${JOB_A}`)
      await realtime.signal(`job:${JOB_A}`)
      expect(read).toHaveBeenCalledTimes(2)
      await act(async () => {
        slow.resolve(job(JOB_A, 'running'))
        await vi.advanceTimersByTimeAsync(0)
      })
      expect(read).toHaveBeenCalledTimes(3)
    })

    it('polls every 400 ms while the socket is unavailable, and stops when it is back', async () => {
      const read = vi.mocked(api.getJob)
      realtime.setStatus('unavailable')
      mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      const before = read.mock.calls.length
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_200)
      })
      expect(read.mock.calls.length - before).toBe(3)

      realtime.setStatus('live')
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_200)
      })
      expect(read.mock.calls.length - before).toBe(3)
    })

    it('keeps following a running job through a failed read, and reads it again (#1040)', async () => {
      const read = vi.mocked(api.getJob)
      read.mockRejectedValueOnce(new ApiError(500, 'injected 500'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(result.current.error).toBeUndefined()
      expect(result.current.rendering).toBe(true)
      expect(realtime.following()).toEqual([`job:${JOB_A}`])

      read.mockImplementation(async (id) => job(id, 'done'))
      await act(async () => {
        await vi.advanceTimersByTimeAsync(1_000)
      })
      expect(result.current.error).toBeUndefined()
      expect(result.current.rendering).toBe(false)
      expect(result.current.job?.status).toBe('done')
    })

    it('reads the job on its job.done after a failed read (#1040)', async () => {
      const read = vi.mocked(api.getJob)
      read.mockRejectedValueOnce(new TypeError('Failed to fetch'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      read.mockImplementation(async (id) => job(id, 'done'))
      await realtime.signal(`job:${JOB_A}`, 'job.done')
      expect(result.current.error).toBeUndefined()
      expect(result.current.job?.status).toBe('done')
      expect(result.current.rendering).toBe(false)
    })

    it('backs off between failed reads, then gives up and offers try again (#1040)', async () => {
      const read = vi.mocked(api.getJob)
      read.mockRejectedValue(new ApiError(502, 'Bad Gateway'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(read).toHaveBeenCalledTimes(1)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400)
      })
      expect(read).toHaveBeenCalledTimes(2)
      // The second wait is longer than the first.
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400)
      })
      expect(read).toHaveBeenCalledTimes(2)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(60_000)
      })
      expect(read).toHaveBeenCalledTimes(READ_RETRIES + 1)
      expect(result.current.rendering).toBe(false)
      expect(result.current.error?.message).toContain('Bad Gateway')
      expect(canRetry(result.current.error)).toBe(true)
      expect(realtime.following()).toEqual([])
    })

    it('does not poll past a pending retry while the socket is unavailable (#1040)', async () => {
      const read = vi.mocked(api.getJob)
      realtime.setStatus('unavailable')
      read.mockRejectedValue(new ApiError(500, 'injected 500'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })
      expect(result.current.error).toBeUndefined()
      // At 0, then the backoff's 400 and 1200 ms: no 400 ms poll in between.
      expect(read).toHaveBeenCalledTimes(3)
    })

    it('does not read past a pending retry for events that came during the failed read (#1040)', async () => {
      const read = vi.mocked(api.getJob)
      mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      let fail!: (cause: Error) => void
      read.mockImplementationOnce(
        () =>
          new Promise<Job>((_, reject) => {
            fail = reject
          }),
      )
      await realtime.signal(`job:${JOB_A}`)
      await realtime.signal(`job:${JOB_A}`)
      const before = read.mock.calls.length
      await act(async () => {
        fail(new ApiError(502, 'Bad Gateway'))
        await vi.advanceTimersByTimeAsync(0)
      })
      // The event that came during the failed read waits for its backoff.
      expect(read.mock.calls.length - before).toBe(0)
      await act(async () => {
        await vi.advanceTimersByTimeAsync(400)
      })
      expect(read.mock.calls.length - before).toBe(1)
    })

    it('gives up at once on a job that is gone, and offers try again (#1040)', async () => {
      vi.mocked(api.getJob).mockRejectedValue(new ApiError(404, 'no such job'))
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(api.getJob).toHaveBeenCalledTimes(1)
      expect(result.current.rendering).toBe(false)
      expect(result.current.error?.message).toContain('no such job')
      expect(canRetry(result.current.error)).toBe(true)
      expect(realtime.following()).toEqual([])
    })
  })
})


describe('canRetry (review #1066 (13) 2)', () => {
  it.each([
    [new ApiError({ type: TEMPORAL_UNAVAILABLE, title: 'Service Unavailable', status: 503, detail: 'down' }), true],
    [new ApiError({ type: STILL_ACCEPTING, title: 'Service Unavailable', status: 503, detail: 'checking' }), true],
    [new ApiError({ type: UNANSWERED, title: 'Bad Gateway', status: 502, detail: 'no answer' }), true],
    [new ApiError({ type: OFFLINE, title: 'Offline', status: 0, detail: 'offline' }), true],
    [new TypeError('Failed to fetch'), true],
    [new ApiError({ title: 'Unprocessable Content', status: 422, detail: 'width must be at most 100' }), false],
    [new ApiError({ title: 'Internal Server Error', status: 500, detail: 'the render could not be started' }), false],
    [new Error('openscad exited with 1'), false],
  ])('%#: %s is %s', (error, expected) => {
    expect(canRetry(error)).toBe(expected)
  })
})

// #2464 — a submit a retry can fix is sent again on its own, with backoff and a cap.
describe('useRenderAutoRetry', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  /** A failed submit, as a new error object each time, as each failed try gives. */
  const unanswered = () => new ApiError({ type: UNANSWERED, title: 'No answer', status: 503 })

  function setup(values: unknown = { a: 1 }, enabled = true) {
    const retry = vi.fn()
    type Props = { error: unknown; values: unknown; rendering?: boolean; enabled?: boolean }
    const view = renderHook(
      ({ error, values, rendering = false, enabled = true }: Props) =>
        useRenderAutoRetry(error, rendering, retry, values, enabled),
      { initialProps: { error: unanswered(), values, enabled } as Props },
    )
    return { retry, view }
  }

  it(`tries ${RENDER_AUTO_RETRIES} times, doubling the wait to at most a minute, then gives up`, () => {
    // The same values throughout, as `debounced` keeps its identity until they change.
    const values = { a: 1 }
    const { retry, view } = setup(values)
    for (let n = 0; n < RENDER_AUTO_RETRIES; n++) {
      const wait = Math.min(RENDER_AUTO_RETRY_MS * 2 ** n, RENDER_AUTO_RETRY_MAX_MS)
      act(() => vi.advanceTimersByTime(wait - 1))
      expect(retry).toHaveBeenCalledTimes(n)
      act(() => vi.advanceTimersByTime(1))
      expect(retry).toHaveBeenCalledTimes(n + 1)
      view.rerender({ error: unanswered(), values })
    }
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MAX_MS * 10))
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES)
    // The timed tries are spent; coming back online still tries once (#3552).
    act(() => window.dispatchEvent(new Event('online')))
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES + 1)
    // That try fails too: it arms no timed one past the cap (#3793 review).
    view.rerender({ error: unanswered(), values })
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MAX_MS * 10))
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES + 1)
  })

  it('tries again at once when the browser comes back online', () => {
    const { retry } = setup()
    act(() => window.dispatchEvent(new Event('online')))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('starts the count over for new values that fail too', () => {
    const values = { a: 1 }
    const { retry, view } = setup(values)
    for (let n = 0; n < RENDER_AUTO_RETRIES; n++) {
      act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MAX_MS))
      view.rerender({ error: unanswered(), values })
    }
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES)
    view.rerender({ error: unanswered(), values: { a: 2 } })
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MS))
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES + 1)
  })

  it('keeps the count while a retry is in flight, and starts it over once a render goes through', () => {
    const values = { a: 1 }
    const { retry, view } = setup(values)
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MS))
    expect(retry).toHaveBeenCalledTimes(1)
    // The retry is in flight (no error yet), then fails again: the second wait, doubled.
    view.rerender({ error: undefined, values, rendering: true })
    view.rerender({ error: unanswered(), values })
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MS * 2 - 1))
    expect(retry).toHaveBeenCalledTimes(1)
    act(() => vi.advanceTimersByTime(1))
    expect(retry).toHaveBeenCalledTimes(2)
    // That retry goes through (no error, not rendering): the next failure waits the first wait.
    view.rerender({ error: undefined, values })
    view.rerender({ error: unanswered(), values })
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MS))
    expect(retry).toHaveBeenCalledTimes(3)
  })

  it('does not count a try the browser coming back online made (#3341)', () => {
    const values = { a: 1 }
    const { retry, view } = setup(values)
    // A connection that keeps dropping: each return is tried at once, none counted.
    for (let n = 0; n < RENDER_AUTO_RETRIES + 2; n++) {
      act(() => window.dispatchEvent(new Event('online')))
      view.rerender({ error: unanswered(), values })
    }
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES + 2)
    // The server's tries are all still there, from the first wait.
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MS))
    expect(retry).toHaveBeenCalledTimes(RENDER_AUTO_RETRIES + 3)
  })

  it('is not armed while the values cannot be rendered, and keeps every try for when they can (#3341)', () => {
    const values = { a: 1 }
    const { retry, view } = setup(values, false)
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MAX_MS * 10))
    act(() => window.dispatchEvent(new Event('online')))
    expect(retry).not.toHaveBeenCalled()
    // Valid again with the error still on screen: the first wait, as if nothing was tried.
    view.rerender({ error: unanswered(), values, enabled: true })
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MS))
    expect(retry).toHaveBeenCalledTimes(1)
  })

  it('leaves an error a retry cannot fix alone', () => {
    const retry = vi.fn()
    renderHook(() => useRenderAutoRetry(new Error('openscad exited with 1'), false, retry, {}))
    act(() => vi.advanceTimersByTime(RENDER_AUTO_RETRY_MAX_MS * 10))
    expect(retry).not.toHaveBeenCalled()
  })
})
