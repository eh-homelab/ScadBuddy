import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { insertWeightOffer, type InsertWeightCheck } from '../../api/filament'
import { CHECK_WAIT_MS, useInsertWeightCheck } from './useInsertWeightCheck'

vi.mock('../../api/filament', () => ({ insertWeightOffer: vi.fn() }))

const NO_OFFER: InsertWeightCheck = { offer: null, reason: 'used', insert_type_id: 'i', empty_weight_g: 180, filament_g: 1020 }

describe('useInsertWeightCheck (#7452)', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => {
    vi.useRealTimers()
    vi.mocked(insertWeightOffer).mockReset()
  })

  it('stops waiting for a check that does not answer', () => {
    vi.mocked(insertWeightOffer).mockReturnValue(new Promise(() => {}))
    const { result } = renderHook(() => useInsertWeightCheck('spool', 1200))
    act(() => vi.advanceTimersByTime(300))
    expect(result.current).toMatchObject({ check: null, pending: true, unanswered: false })
    act(() => vi.advanceTimersByTime(CHECK_WAIT_MS - 1))
    expect(result.current.pending).toBe(true)
    act(() => vi.advanceTimersByTime(1))
    expect(result.current).toMatchObject({ check: null, pending: false, unanswered: true })
  })

  it('keeps an answer that comes in time', async () => {
    vi.mocked(insertWeightOffer).mockResolvedValue(NO_OFFER)
    const { result } = renderHook(() => useInsertWeightCheck('spool', 1200))
    await act(async () => vi.advanceTimersByTimeAsync(300))
    expect(result.current).toMatchObject({ check: NO_OFFER, pending: false, unanswered: false })
    await act(async () => vi.advanceTimersByTimeAsync(CHECK_WAIT_MS))
    expect(result.current).toMatchObject({ check: NO_OFFER, pending: false, unanswered: false })
  })

  it('reads the check again on recheck', async () => {
    vi.mocked(insertWeightOffer).mockResolvedValue(NO_OFFER)
    const { result } = renderHook(() => useInsertWeightCheck('spool', 1200))
    await act(async () => vi.advanceTimersByTimeAsync(300))
    expect(insertWeightOffer).toHaveBeenCalledTimes(1)
    act(() => result.current.recheck())
    expect(result.current.pending).toBe(true)
    await act(async () => vi.advanceTimersByTimeAsync(300))
    expect(insertWeightOffer).toHaveBeenCalledTimes(2)
    expect(result.current.pending).toBe(false)
  })
})
