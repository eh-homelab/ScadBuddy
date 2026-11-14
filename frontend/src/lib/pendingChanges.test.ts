import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakePendingChanges } from './pendingChanges.fake'
import { PENDING_CHANGES_PATH, REOPEN_MS, subscribePendingChanges, useReadOnPendingChanges } from './pendingChanges'

// #265 — a flow run's queue follows the agent's pending-input stream, not a timer.

const MS = 15_000

describe('useReadOnPendingChanges', () => {
  let stream: ReturnType<typeof fakePendingChanges>
  beforeEach(() => {
    vi.useFakeTimers()
    stream = fakePendingChanges()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('reads on each change the stream announces, and never on a timer while it is up', async () => {
    const read = vi.fn()
    renderHook(() => useReadOnPendingChanges(true, read, MS))
    await act(() => vi.advanceTimersByTimeAsync(MS * 4))
    expect(read).not.toHaveBeenCalled()

    await stream.changed()
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('reads on a timer only once the stream closed for good', async () => {
    const read = vi.fn()
    renderHook(() => useReadOnPendingChanges(true, read, MS))
    await stream.close()
    await act(() => vi.advanceTimersByTimeAsync(MS))
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('opens a stream that closed for good again after a back-off, and leaves the timer once it answers (#265 review)', async () => {
    const read = vi.fn()
    renderHook(() => useReadOnPendingChanges(true, read, MS))
    await stream.close()
    expect(stream.open()).toHaveLength(0)

    await act(() => vi.advanceTimersByTimeAsync(REOPEN_MS))
    expect(stream.sources).toHaveLength(2)
    expect(stream.open()).toHaveLength(1)
    await stream.opened()
    read.mockClear()
    await act(() => vi.advanceTimersByTimeAsync(MS * 3))
    expect(read).not.toHaveBeenCalled()
    await stream.changed()
    expect(read).toHaveBeenCalledTimes(1)
  })

  it('waits twice as long after each try that closes again', async () => {
    renderHook(() => useReadOnPendingChanges(true, vi.fn(), MS))
    await stream.close()
    await act(() => vi.advanceTimersByTimeAsync(REOPEN_MS))
    expect(stream.sources).toHaveLength(2)
    await stream.close()
    await act(() => vi.advanceTimersByTimeAsync(REOPEN_MS))
    expect(stream.sources).toHaveLength(2)
    await act(() => vi.advanceTimersByTimeAsync(REOPEN_MS))
    expect(stream.sources).toHaveLength(3)
  })

  it('follows nothing while inactive', async () => {
    const read = vi.fn()
    renderHook(() => useReadOnPendingChanges(false, read, MS))
    expect(stream.sources).toEqual([])
    await act(() => vi.advanceTimersByTimeAsync(MS * 2))
    expect(read).not.toHaveBeenCalled()
  })

  it('shares one stream per tab, closed once nothing follows it', () => {
    const first = renderHook(() => useReadOnPendingChanges(true, vi.fn(), MS))
    const off = subscribePendingChanges(vi.fn())
    expect(stream.sources.map((s) => s.url)).toEqual([PENDING_CHANGES_PATH])
    first.unmount()
    expect(stream.open()).toHaveLength(1)
    off()
    expect(stream.open()).toHaveLength(0)
  })
})
