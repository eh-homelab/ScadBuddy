import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, OPERATION_UNFINISHED } from '../../api/client'
import type { OrderItemDetail, Quote, SliceQuoteRequest } from '../../api/types'
import { useSliceQuotes } from './useSliceQuotes'

const BODY = { printer_id: 1, filament_plan: { slots: [], force_colour_match: false }, choices: {}, filaments: {} } as unknown as SliceQuoteRequest

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (cause: unknown) => void
  const promise = new Promise<T>((res, rej) => {
    resolve = res
    reject = rej
  })
  return { promise, resolve, reject }
}

// #4693 — the page follows a slice quote, so the dialog that chose it can close at once.
describe('useSliceQuotes (#4693)', () => {
  afterEach(() => vi.restoreAllMocks())

  it('keeps a run while the operation is followed, then reloads the order', async () => {
    const pending = deferred<Quote>()
    const quoted = vi.spyOn(api, 'quoteItem').mockReturnValue(pending.promise)
    const onQuoted = vi.fn()
    const { result } = renderHook(() => useSliceQuotes('order-1', undefined, onQuoted))
    act(() => result.current.start('item-1', BODY, []))
    expect(quoted).toHaveBeenCalledWith('order-1', 'item-1', BODY)
    expect(result.current.runs.get('item-1')).toMatchObject({ error: null })
    await act(async () => pending.resolve({} as Quote))
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
    expect(onQuoted).toHaveBeenCalledTimes(1)
  })

  it('keeps a failed run with its refusal until another starts', async () => {
    const pending = deferred<Quote>()
    vi.spyOn(api, 'quoteItem').mockReturnValue(pending.promise)
    const onQuoted = vi.fn()
    const { result } = renderHook(() => useSliceQuotes('order-1', undefined, onQuoted))
    act(() => result.current.start('item-1', BODY, []))
    await act(async () => pending.reject(new Error('No printer.')))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toMatch(/No printer/))
    expect(onQuoted).not.toHaveBeenCalled()
  })

  it('keeps a quote the page stopped following as running, until its quote shows on the item', async () => {
    const pending = deferred<Quote>()
    vi.spyOn(api, 'quoteItem').mockReturnValue(pending.promise)
    const before = [{ id: 'item-1', quotes: [{ id: 'q-old' }] }] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, vi.fn()), { initialProps: { items: before } })
    act(() => result.current.start('item-1', BODY, ['q-old']))
    await act(async () => pending.reject(new ApiError({ type: OPERATION_UNFINISHED, title: 'Still running', status: 504 })))
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ detached: true, error: null }))
    // The order's refresh without the quote yet: still running.
    rerender({ items: [...before] })
    expect(result.current.runs.has('item-1')).toBe(true)
    rerender({ items: [{ id: 'item-1', quotes: [{ id: 'q-old' }, { id: 'q-new' }] }] as unknown as OrderItemDetail[] })
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
  })

  it("drops another order's runs", async () => {
    vi.spyOn(api, 'quoteItem').mockReturnValue(new Promise<Quote>(() => undefined))
    const { result, rerender } = renderHook(({ id }) => useSliceQuotes(id, undefined, vi.fn()), { initialProps: { id: 'order-1' } })
    act(() => result.current.start('item-1', BODY, []))
    expect(result.current.runs.size).toBe(1)
    rerender({ id: 'order-2' })
    await waitFor(() => expect(result.current.runs.size).toBe(0))
  })
})
