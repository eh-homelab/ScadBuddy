import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, OPERATION_UNFINISHED } from '../../api/client'
import type { Activity, OperationAccepted, OrderItemDetail, Quote, SliceQuoteRequest } from '../../api/types'
import { useSliceQuotes } from './useSliceQuotes'

const BODY: SliceQuoteRequest = {
  printer_id: 1,
  filament_plan: { slots: [], force_colour_match: false },
  choices: { bed_type: 'Textured PEI Plate', nozzles: [], tier: null },
  filaments: {},
}

/** The 202 a slice quote's start answers. */
const ACCEPTED: OperationAccepted = { id: 'op-started', kind: 'order_item_quote', subject: 'order_item:item-1', status: 'running', created_at: '2026-10-26T10:00:00Z', repeated: false }

/** An activity row of a running `order_item_quote` operation (`operations/activity.py` `row_of`). */
const ROW: Activity = {
  id: 'operation:op-0',
  kind: 'operation.order_item_quote',
  principal: 'browser',
  session: null,
  via_tool: null,
  subject: 'order_item:item-0',
  status: 'running',
  title: 'Order item quote order_item:item-0',
  parent_id: null,
  detail: {},
  created_at: null,
  updated_at: null,
  finished_at: null,
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
      onAccepted?.(ACCEPTED)
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
      onAccepted?.(ACCEPTED)
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

  it('picks up a quote still running when the page loads, and follows it to its end (#4736)', async () => {
    const pending = deferred<Quote>()
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({
      items: [
        { ...ROW, id: 'operation:op-1', subject: 'order_item:item-1', created_at: '2026-10-26T10:00:00Z' },
        // Another order's item is not this page's.
        { ...ROW, id: 'operation:op-2', subject: 'order_item:item-9', created_at: '2026-10-26T10:00:00Z' },
      ],
      next_cursor: null,
    })
    const followed = vi.spyOn(api, 'followQuote').mockReturnValue(pending.promise)
    const onQuoted = vi.fn()
    const items = [{ id: 'item-1', order_id: 'order-1', quotes: [{ id: 'q-old', source: 'slice' }] }] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, onQuoted))
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ error: null, known: ['q-old'], startedAt: Date.parse('2026-10-26T10:00:00Z') }))
    expect(followed).toHaveBeenCalledTimes(1)
    expect(followed).toHaveBeenCalledWith('op-1', expect.any(AbortSignal))
    expect(result.current.runs.has('item-9')).toBe(false)
    await act(async () => pending.resolve({} as Quote))
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
    expect(onQuoted).toHaveBeenCalledTimes(1)
  })

  it('looks again on each reload of the items, so a quote started in another tab shows (#4736 review)', async () => {
    const running = vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    vi.spyOn(api, 'followQuote').mockReturnValue(new Promise<Quote>(() => undefined))
    const first = [{ id: 'item-1', order_id: 'order-1', quotes: [] }] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, vi.fn()), { initialProps: { items: first } })
    await waitFor(() => expect(running).toHaveBeenCalledTimes(1))
    running.mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-tab', subject: 'order_item:item-1' }], next_cursor: null })
    rerender({ items: [...first] })
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ error: null }))
    expect(running).toHaveBeenCalledTimes(2)
  })

  it('follows an operation once, though its activity row stays running after it ended (#4736 review)', async () => {
    const running = vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-stale', subject: 'order_item:item-1' }], next_cursor: null })
    const followed = vi.spyOn(api, 'followQuote').mockResolvedValue({} as Quote)
    const onQuoted = vi.fn()
    const first = [{ id: 'item-1', order_id: 'order-1', quotes: [] }] as unknown as OrderItemDetail[]
    const { rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, onQuoted), { initialProps: { items: first } })
    await waitFor(() => expect(onQuoted).toHaveBeenCalledTimes(1))
    // The reload that quote brought: the stale row is listed again, and not followed again.
    rerender({ items: [...first] })
    await waitFor(() => expect(running).toHaveBeenCalledTimes(2))
    expect(followed).toHaveBeenCalledTimes(1)
    expect(onQuoted).toHaveBeenCalledTimes(1)
  })

  it('never picks up a quote it started itself, though its activity row still says running (#4736 review)', async () => {
    const running = vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    const followed = vi.spyOn(api, 'followQuote').mockResolvedValue({} as Quote)
    const pending = deferred<Quote>()
    vi.spyOn(api, 'quoteItem').mockImplementation((_order, _item, _body, onAccepted) => {
      onAccepted?.(ACCEPTED)
      return pending.promise
    })
    const onQuoted = vi.fn()
    const first = [{ id: 'item-1', order_id: 'order-1', quotes: [] }] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, onQuoted), { initialProps: { items: first } })
    await waitFor(() => expect(running).toHaveBeenCalledTimes(1))
    await act(async () => result.current.start('item-1', BODY, []))
    await act(async () => pending.resolve({} as Quote))
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
    // The reload its quote brought, with the projection not yet caught up.
    running.mockResolvedValue({ items: [{ ...ROW, id: `operation:${ACCEPTED.id}`, subject: 'order_item:item-1' }], next_cursor: null })
    rerender({ items: [...first] })
    await waitFor(() => expect(running).toHaveBeenCalledTimes(2))
    expect(followed).not.toHaveBeenCalled()
    expect(onQuoted).toHaveBeenCalledTimes(1)
  })

  it('shows a picked-up quote that failed as failed (#4736)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-1', subject: 'order_item:item-1' }], next_cursor: null })
    vi.spyOn(api, 'followQuote').mockRejectedValue(new ApiError(422, 'No printer takes this plate.'))
    const items = [{ id: 'item-1', order_id: 'order-1', quotes: [] }] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toContain('No printer takes this plate.'))
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
