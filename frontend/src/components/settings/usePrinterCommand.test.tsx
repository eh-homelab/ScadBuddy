import { act, renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { usePrinterCommand } from './usePrinterCommand'

describe('usePrinterCommand', () => {
  it('forgets a request once it succeeds or fails, so stop has nothing to abort', async () => {
    const { result } = renderHook(() => usePrinterCommand('p1'))
    const signals: AbortSignal[] = []
    await act(() => result.current.track(async (signal) => void signals.push(signal)))
    await act(() =>
      result.current
        .track(async (signal) => {
          signals.push(signal)
          throw new Error('boom')
        })
        .catch(() => undefined),
    )
    act(() => result.current.stop())
    expect(signals.map((signal) => signal.aborted)).toEqual([false, false])
  })

  it('aborts a request that is still running when stop is called', async () => {
    const { result } = renderHook(() => usePrinterCommand('p1'))
    let seen!: AbortSignal
    const running = result.current.track((signal) => {
      seen = signal
      return new Promise<void>((_, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))))
    })
    act(() => result.current.stop())
    await expect(running).rejects.toThrow('aborted')
    expect(seen.aborted).toBe(true)
  })
})
