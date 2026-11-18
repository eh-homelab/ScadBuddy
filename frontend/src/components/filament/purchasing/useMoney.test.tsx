import { renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from '../../../api/client'
import { useMoney } from './useMoney'

afterEach(() => vi.restoreAllMocks())

describe('useMoney', () => {
  it('shows the bare decimal, with no symbol, while the currency is unread', () => {
    vi.spyOn(api, 'priceBook').mockReturnValue(new Promise(() => undefined))
    const { result } = renderHook(() => useMoney())
    expect(result.current.currency).toBeNull()
    expect(result.current.format('1234.5')).not.toMatch(/[$€£]|USD/)
    expect(result.current.format('1234.5')).toMatch(/1.?234\.50/)
  })

  it('keeps the bare decimal when the price book fails', async () => {
    const book = vi.spyOn(api, 'priceBook').mockRejectedValue(new Error('down'))
    const { result } = renderHook(() => useMoney())
    await waitFor(() => expect(book).toHaveBeenCalled())
    expect(result.current.format('5')).not.toMatch(/[$€£]|USD/)
  })

  it('uses the shop currency once it is read', async () => {
    vi.spyOn(api, 'priceBook').mockResolvedValue({ currency: 'EUR', minor_exponent: 2 } as never)
    const { result } = renderHook(() => useMoney())
    await waitFor(() => expect(result.current.currency).toBe('EUR'))
    expect(result.current.format('5')).toContain('€')
  })
})
