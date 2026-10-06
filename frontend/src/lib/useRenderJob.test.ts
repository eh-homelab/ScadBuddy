import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { ApiError, STILL_ACCEPTING, TEMPORAL_UNAVAILABLE, api } from '../api/client'
import type { Job, RenderAccepted } from '../api/types'
import { fakeRealtime } from './realtime.fake'
import { useRenderJob } from './useRenderJob'

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
    it('reads the job when the subscription is confirmed and on each event, never on a timer', async () => {
      const read = vi.mocked(api.getJob)
      const { result } = mount({ slug: 'demo', params: { n: 1 } })
      await settle()
      expect(realtime.following()).toEqual([`job:${JOB_A}`])
      expect(read).toHaveBeenCalledTimes(1)

      await act(async () => {
        await vi.advanceTimersByTimeAsync(5_000)
      })
      expect(read).toHaveBeenCalledTimes(1)

      read.mockImplementation(async (id) => job(id, 'running'))
      await realtime.signal(`job:${JOB_A}`)
      expect(read).toHaveBeenCalledTimes(2)
      expect(result.current.job?.status).toBe('running')

      read.mockImplementation(async (id) => job(id, 'done'))
      await realtime.signal(`job:${JOB_A}`)
      expect(result.current.rendering).toBe(false)
      expect(result.current.job?.status).toBe('done')
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
  })
})
