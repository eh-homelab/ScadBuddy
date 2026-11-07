import { act, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useNow } from './useNow'

describe('useNow (#5585)', () => {
  afterEach(() => vi.useRealTimers())

  it('reads the clock at once when it turns active, not only at the first tick', () => {
    vi.useFakeTimers({ now: Date.parse('2026-11-07T10:00:00Z') })
    const { result, rerender } = renderHook(({ active }) => useNow(active), { initialProps: { active: false } })
    vi.setSystemTime(Date.parse('2026-11-07T10:05:00Z'))
    rerender({ active: true })
    expect(result.current).toBe(Date.parse('2026-11-07T10:05:00Z'))
  })

  it('reads the clock again each tick while active, and not once inactive (#5596)', () => {
    vi.useFakeTimers({ now: Date.parse('2026-11-07T10:00:00Z') })
    const { result, rerender } = renderHook(({ active }) => useNow(active), { initialProps: { active: true } })
    act(() => vi.advanceTimersByTime(3000))
    expect(result.current).toBe(Date.parse('2026-11-07T10:00:03Z'))
    rerender({ active: false })
    act(() => vi.advanceTimersByTime(3000))
    expect(result.current).toBe(Date.parse('2026-11-07T10:00:03Z'))
  })
})
