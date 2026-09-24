import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest'
import { api } from '../api/client'
import type { PrintProgress } from '../api/types'
import * as fixtures from '../mocks/fixtures'
import { usePrintProgress } from './usePrintProgress'

const OUTPUT_A = 'a'.repeat(32)
const OUTPUT_B = 'c'.repeat(32)

/** A read this test resolves by hand, so two of them can land out of order. */
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((settle) => {
    resolve = settle
  })
  return { promise, resolve }
}

/** Lets the mocked read settle and React commit without running any poll timer. */
async function settle() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(0)
  })
}

async function tick(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms)
  })
}

describe('usePrintProgress', () => {
  let read: MockInstance<typeof api.getPrintProgress>

  beforeEach(() => {
    vi.useFakeTimers()
    read = vi.spyOn(api, 'getPrintProgress')
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('stops as soon as the backend says the print has settled', async () => {
    // The recorded failed run: Bambuddy still calls it `in_progress` with a copy in
    // flight, so only `settled` can end this.
    read.mockResolvedValue(fixtures.failedRunProgress)
    const { result } = renderHook(() => usePrintProgress(OUTPUT_A, true))
    await settle()

    expect(result.current.progress).toEqual(fixtures.failedRunProgress)
    expect(result.current.polling).toBe(false)
    expect(read).toHaveBeenCalledTimes(1)

    await tick(10_000)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('stops on a null answer, because the output has never been printed', async () => {
    read.mockResolvedValue(null)
    const { result } = renderHook(() => usePrintProgress(OUTPUT_A, true))
    await settle()

    expect(result.current.progress).toBeNull()
    expect(result.current.polling).toBe(false)
    expect(read).toHaveBeenCalledTimes(1)

    await tick(10_000)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('leaves no timer behind when it unmounts mid-print', async () => {
    read.mockResolvedValue(fixtures.pipelineProgress)
    const { result, unmount } = renderHook(() => usePrintProgress(OUTPUT_A, true))
    await settle()

    expect(result.current.polling).toBe(true)
    expect(vi.getTimerCount()).toBe(1)

    unmount()
    expect(vi.getTimerCount()).toBe(0)

    await tick(10_000)
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('discards an answer a newer output has superseded', async () => {
    const first = deferred<PrintProgress | null>()
    const second = deferred<PrintProgress | null>()
    read.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)

    const { result, rerender } = renderHook(
      ({ id }: { id: string }) => usePrintProgress(id, true),
      { initialProps: { id: OUTPUT_A } },
    )
    rerender({ id: OUTPUT_B })

    second.resolve(fixtures.queuedSliceProgress)
    await settle()
    // The older output's read finishes last, which is the ordering that would otherwise
    // overwrite the newer answer.
    first.resolve(fixtures.failedRunProgress)
    await settle()

    expect(result.current.progress).toEqual(fixtures.queuedSliceProgress)
  })

  it('reads nothing while it is disabled', async () => {
    renderHook(() => usePrintProgress(OUTPUT_A, false))
    await settle()

    expect(read).not.toHaveBeenCalled()
  })
})
