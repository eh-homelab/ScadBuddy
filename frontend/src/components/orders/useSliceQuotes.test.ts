import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, OPERATION_UNFINISHED } from '../../api/client'
import type { OrderItemDetail, Quote, SliceQuoteRequest } from '../../api/types'
import { useSliceQuotes } from './useSliceQuotes'

const BODY: SliceQuoteRequest = {
  printer_id: 1,
  filament_plan: { slots: [], force_colour_match: false },
  choices: { bed_type: 'Textured PEI Plate', nozzles: [], tier: null },
  filaments: {},
}

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
    act(() => void result.current.start('item-1', BODY, []))
    expect(quoted).toHaveBeenCalledWith('order-1', 'item-1', BODY, expect.any(Function), expect.any(AbortSignal))
    expect(result.current.runs.get('item-1')).toMatchObject({ error: null })
    await act(async () => pending.resolve({} as Quote))
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
    expect(onQuoted).toHaveBeenCalledTimes(1)
  })

  it('settles start once the server accepts the quote, before it ends', async () => {
    const pending = deferred<Quote>()
    vi.spyOn(api, 'quoteItem').mockImplementation((_order, _item, _body, onAccepted) => {
      onAccepted?.()
      return pending.promise
    })
    const { result } = renderHook(() => useSliceQuotes('order-1', undefined, vi.fn()))
    let started!: Promise<void>
    act(() => {
      started = result.current.start('item-1', BODY, [])
    })
    await expect(started).resolves.toBeUndefined()
    expect(result.current.runs.get('item-1')).toMatchObject({ error: null })
  })

  it('rejects start with a refusal before the quote ran, and keeps no run', async () => {
    vi.spyOn(api, 'quoteItem').mockRejectedValue(new ApiError(409, 'Every slot needs a price-book filament.'))
    const { result } = renderHook(() => useSliceQuotes('order-1', undefined, vi.fn()))
    let started!: Promise<void>
    act(() => {
      started = result.current.start('item-1', BODY, [])
    })
    await act(async () => {
      await expect(started).rejects.toThrow()
    })
    expect(result.current.runs.has('item-1')).toBe(false)
  })

  it('keeps a run that failed after it was accepted, with its refusal', async () => {
    const pending = deferred<Quote>()
    vi.spyOn(api, 'quoteItem').mockImplementation((_order, _item, _body, onAccepted) => {
      onAccepted?.()
      return pending.promise
    })
    const onQuoted = vi.fn()
    const { result } = renderHook(() => useSliceQuotes('order-1', undefined, onQuoted))
    act(() => void result.current.start('item-1', BODY, []))
    await act(async () => pending.reject(new Error('No printer.')))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toMatch(/No printer/))
    expect(onQuoted).not.toHaveBeenCalled()
  })

  it('keeps a quote the page stopped following as running, until its quote shows on the item', async () => {
    const pending = deferred<Quote>()
    vi.spyOn(api, 'quoteItem').mockReturnValue(pending.promise)
    const before = [{ id: 'item-1', quotes: [{ id: 'q-old', source: 'slice' }] }] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, vi.fn()), { initialProps: { items: before } })
    act(() => void result.current.start('item-1', BODY, ['q-old']))
    await act(async () => pending.reject(new ApiError({ type: OPERATION_UNFINISHED, title: 'Still running', status: 504 })))
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ detached: true, error: null }))
    // The order's refresh without the quote yet, or with a price typed by hand: still running.
    rerender({ items: [...before] })
    expect(result.current.runs.has('item-1')).toBe(true)
    rerender({ items: [{ id: 'item-1', quotes: [{ id: 'q-old', source: 'slice' }, { id: 'q-hand', source: 'manual' }] }] as unknown as OrderItemDetail[] })
    expect(result.current.runs.has('item-1')).toBe(true)
    rerender({ items: [{ id: 'item-1', quotes: [{ id: 'q-old', source: 'slice' }, { id: 'q-new', source: 'slice' }] }] as unknown as OrderItemDetail[] })
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
  })

  it("drops another order's runs", async () => {
    vi.spyOn(api, 'quoteItem').mockReturnValue(new Promise<Quote>(() => undefined))
    const { result, rerender } = renderHook(({ id }) => useSliceQuotes(id, undefined, vi.fn()), { initialProps: { id: 'order-1' } })
    act(() => void result.current.start('item-1', BODY, []))
    expect(result.current.runs.size).toBe(1)
    rerender({ id: 'order-2' })
    await waitFor(() => expect(result.current.runs.size).toBe(0))
  })

  it('ends its follows with the page', () => {
    let signal: AbortSignal | undefined
    vi.spyOn(api, 'quoteItem').mockImplementation((_order, _item, _body, _accepted, given) => {
      signal = given
      return new Promise<Quote>(() => undefined)
    })
    const { result, unmount } = renderHook(() => useSliceQuotes('order-1', undefined, vi.fn()))
    act(() => void result.current.start('item-1', BODY, []))
    expect(signal?.aborted).toBe(false)
    unmount()
    expect(signal?.aborted).toBe(true)
  })
})
