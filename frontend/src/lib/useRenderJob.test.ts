import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { ApiError, api } from '../api/client'
import type { Job, RenderAccepted } from '../api/types'
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

type Props = { slug: string; params: Record<string, number>; version?: string }

function mount(initialProps: Props) {
  return renderHook(({ slug, params, version }: Props) => useRenderJob(slug, params, version), {
    initialProps,
  })
}

describe('useRenderJob', () => {
  let submit: MockInstance<typeof api.render>

  beforeEach(() => {
    vi.useFakeTimers()
    let next = 0
    const ids = [JOB_A, JOB_B, JOB_C]
    submit = vi.spyOn(api, 'render').mockImplementation(async () => accepted(ids[next++]!))
    vi.spyOn(api, 'getJob').mockImplementation(async (id) => job(id, 'pending'))
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('tells the server which render a new one replaces', async () => {
    const { rerender } = mount({ slug: 'demo', params: { n: 1 } })
    await settle()
    rerender({ slug: 'demo', params: { n: 2 } })
    await settle()

    expect(submit).toHaveBeenNthCalledWith(1, 'demo', { n: 1 }, undefined, undefined)
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { n: 2 }, undefined, JOB_A)
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

    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { n: 2 }, undefined, JOB_A)
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

    expect(result.current.busy).toBe(3)
    expect(result.current.error).toBeUndefined()
    expect(result.current.rendering).toBe(true)
    expect(submit).toHaveBeenCalledTimes(1)

    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000)
    })

    expect(submit).toHaveBeenCalledTimes(2)
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { n: 1 }, undefined, undefined)
    expect(result.current.busy).toBeUndefined()
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
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', { n: 2 }, undefined, undefined)
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
    expect(submit).toHaveBeenNthCalledWith(2, 'demo', second, undefined, JOB_A)

    // A says "done" now, but it was superseded: neither its job nor its params count.
    lateA.resolve(job(JOB_A, 'done'))
    await settle()
    expect(result.current.settledFor).toBeUndefined()
    expect(result.current.job?.id).not.toBe(JOB_A)
    expect(result.current.rendering).toBe(true)

    poll.mockImplementation(async (id) => job(id, 'done'))
    await act(async () => {
      await vi.advanceTimersByTimeAsync(400)
    })
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

    expect(submit).toHaveBeenNthCalledWith(2, 'other', { n: 1 }, undefined, undefined)
    expect(submit).toHaveBeenNthCalledWith(3, 'other', { n: 1 }, 'f'.repeat(40), undefined)
  })
})
