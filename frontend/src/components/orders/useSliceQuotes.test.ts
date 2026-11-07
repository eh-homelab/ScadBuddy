import { act, renderHook, waitFor } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError, OPERATION_UNFINISHED, UNANSWERED } from '../../api/client'
import type { Activity, OperationAccepted, OrderItemDetail, Quote, SliceQuoteRequest } from '../../api/types'
import { seedActivity } from '../../mocks/features/activity'
import { server } from '../../mocks/server'
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

/** A `followQuote` whose first read finds the operation running, then ends as `end` does. */
function stillRunning(end: () => Promise<Quote>): typeof api.followQuote {
  return (_id, _signal, onRunning) => {
    onRunning?.()
    return end()
  }
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
    const followed = vi.spyOn(api, 'followQuote').mockImplementation(stillRunning(() => pending.promise))
    const onQuoted = vi.fn()
    const items = [{ id: 'item-1', order_id: 'order-1', quotes: [{ id: 'q-old', source: 'slice' }] }] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, onQuoted))
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ error: null, known: ['q-old'], startedAt: Date.parse('2026-10-26T10:00:00Z') }))
    expect(followed).toHaveBeenCalledTimes(1)
    expect(followed).toHaveBeenCalledWith('op-1', expect.any(AbortSignal), expect.any(Function))
    expect(result.current.runs.has('item-9')).toBe(false)
    await act(async () => pending.resolve({} as Quote))
    await waitFor(() => expect(result.current.runs.has('item-1')).toBe(false))
    expect(onQuoted).toHaveBeenCalledTimes(1)
  })

  it('looks again on each reload of the items, so a quote started in another tab shows (#4736 review)', async () => {
    const running = vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    vi.spyOn(api, 'followQuote').mockImplementation(stillRunning(() => new Promise<Quote>(() => undefined)))
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
    const followed = vi.spyOn(api, 'followQuote').mockImplementation(stillRunning(async () => ({}) as Quote))
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

  it('shows nothing for a stale row with no start whose operation already failed (#4736 review)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-old', subject: 'order_item:item-1' }], next_cursor: null })
    // The first read finds it ended: never running, so `onRunning` is not told.
    const followed = vi.spyOn(api, 'followQuote').mockRejectedValue(new ApiError(422, 'An old failure.'))
    const items = [{ id: 'item-1', order_id: 'order-1', quotes: [] }] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(followed).toHaveBeenCalledTimes(1))
    await act(async () => {})
    expect(result.current.runs.has('item-1')).toBe(false)
  })

  it('shows a picked-up quote that failed as failed (#4736)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-1', subject: 'order_item:item-1' }], next_cursor: null })
    vi.spyOn(api, 'followQuote').mockImplementation(stillRunning(() => Promise.reject(new ApiError(422, 'No printer takes this plate.'))))
    const items = [{ id: 'item-1', order_id: 'order-1', quotes: [] }] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toContain('No printer takes this plate.'))
  })

  // #5554 — a failure the page did not start (over MCP, in another tab) shows with its times.
  const FAILED: Activity = { ...ROW, status: 'failed', created_at: '2026-11-06T14:04:00Z', finished_at: '2026-11-06T14:06:00Z' }
  const ITEM = { id: 'item-1', order_id: 'order-1', created_at: '2026-11-06T09:00:00Z', quotes: [] }

  it('shows the newest failed quote of an item from the activity index, with its start, end and error (#5554)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    const failed = vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map([['item-1', { ...FAILED, id: 'operation:op-new', subject: 'order_item:item-1' }]]))
    const followed = vi.spyOn(api, 'followQuote').mockRejectedValue(new ApiError(422, 'No printer takes this plate.'))
    const items = [ITEM] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toContain('No printer takes this plate.'))
    expect(failed).toHaveBeenCalledTimes(1)
    expect(failed).toHaveBeenCalledWith(items)
    expect(followed).toHaveBeenCalledTimes(1)
    expect(followed).toHaveBeenCalledWith('op-new', expect.any(AbortSignal))
    expect(result.current.runs.get('item-1')).toMatchObject({ startedAt: Date.parse('2026-11-06T14:04:00Z'), finishedAt: Date.parse('2026-11-06T14:06:00Z') })
  })

  it('shows no failure that a quote made since answers, nor one of an item still quoting (#5554)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-running', subject: 'order_item:item-2' }], next_cursor: null })
    vi.spyOn(api, 'failedQuotes').mockImplementation(
      async (items) => new Map(items.map((i) => [i.id, { ...FAILED, id: i.id === 'item-1' ? 'operation:op-1' : 'operation:op-2', subject: `order_item:${i.id}` }])),
    )
    const followed = vi.spyOn(api, 'followQuote').mockImplementation(stillRunning(() => new Promise<Quote>(() => undefined)))
    const items = [
      { ...ITEM, quotes: [{ id: 'q-after', source: 'slice', created_at: '2026-11-06T14:30:00Z' }] },
      { ...ITEM, id: 'item-2' },
    ] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-2')).toMatchObject({ error: null }))
    await act(async () => {})
    // Only the running quote is followed; no failure is read.
    expect(followed).toHaveBeenCalledTimes(1)
    expect(followed).toHaveBeenCalledWith('op-running', expect.any(AbortSignal), expect.any(Function))
    expect(result.current.runs.has('item-1')).toBe(false)
  })

  // #5585 — the review follow-ups of #5580.
  it('reads an item’s failures by its subject, so other orders’ failures never push them off (#5585)', async () => {
    const other = (n: number): Activity => ({
      ...FAILED,
      id: `operation:op-other-${n}`,
      subject: `order_item:other-${n}`,
      created_at: new Date(Date.parse('2026-11-06T15:00:00Z') + n * 1000).toISOString(),
    })
    seedActivity([...Array.from({ length: 201 }, (_, n) => other(n)), { ...FAILED, id: 'operation:op-mine', subject: 'order_item:item-1' }])
    const page = await api.failedQuotes([{ id: 'item-1', created_at: '2026-11-06T09:00:00Z' }])
    expect([...page.values()].map((r) => r.id)).toEqual(['operation:op-mine'])
  })

  /** A stale `running` row of item-1 that started at `created`, and a failed row at 14:04. */
  function staleAndFailed(created: string) {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-stale', subject: 'order_item:item-1', created_at: created, updated_at: created }], next_cursor: null })
    vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map([['item-1', { ...FAILED, id: 'operation:op-failed', subject: 'order_item:item-1' }]]))
    // The stale row's first read finds its operation ended: `onRunning` is never told.
    return vi.spyOn(api, 'followQuote').mockImplementation((id) =>
      Promise.reject(new ApiError(422, id === 'op-stale' ? 'The stale one failed.' : 'No printer takes this plate.')),
    )
  }

  it('shows the failure of a stale running row that is newer than the item’s failed row (#5585)', async () => {
    const followed = staleAndFailed('2026-11-06T15:00:00Z')
    const items = [ITEM] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toContain('The stale one failed.'))
    expect(result.current.runs.get('item-1')).toMatchObject({ startedAt: Date.parse('2026-11-06T15:00:00Z') })
    expect(followed).not.toHaveBeenCalledWith('op-failed', expect.anything())
  })

  it('shows the item’s failed row behind an older stale running row whose operation ended (#5585)', async () => {
    staleAndFailed('2026-11-06T13:00:00Z')
    const items = [ITEM] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toContain('No printer takes this plate.'))
    expect(result.current.runs.get('item-1')).toMatchObject({ startedAt: Date.parse('2026-11-06T14:04:00Z') })
  })

  it('shows no old failure while a running row’s first read was lost, and shows it once the row is gone (#5585)', async () => {
    const running = vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-1', subject: 'order_item:item-1', created_at: '2026-11-06T15:00:00Z' }], next_cursor: null })
    const failed = vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map([['item-1', { ...FAILED, id: 'operation:op-old', subject: 'order_item:item-1' }]]))
    const lost = new ApiError({ type: UNANSWERED, title: 'Bad Gateway', status: 502 })
    const followed = vi.spyOn(api, 'followQuote').mockImplementation((id) =>
      Promise.reject(id === 'op-1' ? lost : new ApiError(422, 'An old failure.')),
    )
    const first = [ITEM] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, vi.fn()), { initialProps: { items: first } })
    await waitFor(() => expect(followed).toHaveBeenCalledWith('op-1', expect.any(AbortSignal), expect.any(Function)))
    await act(async () => {})
    expect(result.current.runs.has('item-1')).toBe(false)
    expect(followed).not.toHaveBeenCalledWith('op-old', expect.anything())
    expect(failed).not.toHaveBeenCalled()
    // A reload with the row still running follows it again, and its first read is lost again.
    rerender({ items: [...first] })
    await waitFor(() => expect(followed).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(result.current.runs.has('item-1')).toBe(false)
    // Once the row is gone, the old failure is the item's newest again.
    running.mockResolvedValue({ items: [], next_cursor: null })
    rerender({ items: [...first] })
    await waitFor(() => expect(result.current.runs.get('item-1')?.error).toContain('An old failure.'))
  })

  it('shows no old failure on a reload while an earlier follow’s first read is still out (#5585)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-1', subject: 'order_item:item-1', created_at: '2026-11-06T15:00:00Z' }], next_cursor: null })
    const failed = vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map([['item-1', { ...FAILED, id: 'operation:op-old', subject: 'order_item:item-1' }]]))
    const firstRead = deferred<void>()
    let tell: (() => void) | undefined
    const followed = vi.spyOn(api, 'followQuote').mockImplementation(async (id, _signal, onRunning) => {
      if (id !== 'op-1') throw new ApiError(422, 'An old failure.')
      await firstRead.promise
      onRunning?.()
      tell = onRunning
      return new Promise<Quote>(() => undefined)
    })
    const first = [ITEM] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, vi.fn()), { initialProps: { items: first } })
    await waitFor(() => expect(followed).toHaveBeenCalledTimes(1))
    // The reload, with the follow's first read still out.
    rerender({ items: [...first] })
    await act(async () => {})
    await act(async () => firstRead.resolve())
    await waitFor(() => expect(tell).toBeDefined())
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ error: null }))
    await act(async () => {})
    expect(followed).not.toHaveBeenCalledWith('op-old', expect.anything())
    expect(failed).not.toHaveBeenCalled()
  })

  it('shows no error for a failed row whose operation was pruned (#5585)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map([['item-1', { ...FAILED, id: 'operation:op-pruned', subject: 'order_item:item-1' }]]))
    const followed = vi.spyOn(api, 'followQuote').mockRejectedValue(new ApiError(404, "no operation 'op-pruned'"))
    const items = [ITEM] as unknown as OrderItemDetail[]
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(followed).toHaveBeenCalledWith('op-pruned', expect.any(AbortSignal)))
    await act(async () => {})
    expect(result.current.runs.has('item-1')).toBe(false)
  })

  // #5596 — the review follow-ups of #5587.
  it('reads every item’s newest failure in one request, by its subject (#5596)', async () => {
    const at = (n: number) => new Date(Date.parse('2026-11-06T15:00:00Z') + n * 1000).toISOString()
    seedActivity([
      { ...FAILED, id: 'operation:op-1-old', subject: 'order_item:item-1', created_at: at(0) },
      { ...FAILED, id: 'operation:op-1-new', subject: 'order_item:item-1', created_at: at(1) },
      { ...FAILED, id: 'operation:op-2', subject: 'order_item:item-2', created_at: at(2) },
      { ...FAILED, id: 'operation:op-other', subject: 'order_item:other', created_at: at(3) },
    ])
    const fetched = vi.spyOn(globalThis, 'fetch')
    const page = await api.failedQuotes([
      { id: 'item-1', created_at: '2026-11-06T09:00:00Z' },
      { id: 'item-2', created_at: '2026-11-06T08:00:00Z' },
      { id: 'item-3', created_at: '2026-11-06T10:00:00Z' },
    ])
    expect(Object.fromEntries([...page].map(([item, row]) => [item, row.id]))).toEqual({ 'item-1': 'operation:op-1-new', 'item-2': 'operation:op-2' })
    expect(fetched).toHaveBeenCalledTimes(1)
    const url = new URL(String(fetched.mock.calls[0]![0]), 'http://localhost')
    expect(url.searchParams.getAll('subject')).toEqual(['order_item:item-1', 'order_item:item-2', 'order_item:item-3'])
    expect(url.searchParams.get('since')).toBe('2026-11-06T08:00:00Z')
  })

  // #5599 — the review follow-ups of #5597.
  it('keeps the other chunks’ failures when one chunk’s read fails (#5599)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    const followed = vi.spyOn(api, 'followQuote').mockRejectedValue(new ApiError(422, 'No printer takes this plate.'))
    vi.spyOn(console, 'debug').mockImplementation(() => undefined)
    seedActivity([
      { ...FAILED, id: 'operation:op-first', subject: 'order_item:item-0' },
      { ...FAILED, id: 'operation:op-second', subject: 'order_item:item-200' },
    ])
    // The second chunk (items 200 and on) answers 500; the first falls through to the mock.
    server.use(
      http.get('/api/v1/activity', ({ request }) => {
        if (new URL(request.url).searchParams.getAll('subject').includes('order_item:item-200')) {
          return HttpResponse.json({ detail: 'down' }, { status: 500 })
        }
      }),
    )
    const items = Array.from({ length: 201 }, (_, n) => ({ ...ITEM, id: `item-${n}` })) as unknown as OrderItemDetail[]
    const page = await api.failedQuotes(items)
    expect([...page.keys()]).toEqual(['item-0'])
    const { result } = renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(result.current.runs.get('item-0')?.error).toContain('No printer takes this plate.'))
    expect(followed).toHaveBeenCalledWith('op-first', expect.any(AbortSignal))
    expect(result.current.runs.has('item-200')).toBe(false)
  })

  it('makes one failure read per reload of an order, however many items it has (#5596)', async () => {
    vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [], next_cursor: null })
    const failed = vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map())
    const items = [ITEM, { ...ITEM, id: 'item-2' }, { ...ITEM, id: 'item-3' }] as unknown as OrderItemDetail[]
    renderHook(() => useSliceQuotes('order-1', items, vi.fn()))
    await waitFor(() => expect(failed).toHaveBeenCalledTimes(1))
    await act(async () => {})
    expect(failed).toHaveBeenCalledTimes(1)
  })

  it('shows no old failure after a shown run lost its answer and was dismissed (#5596)', async () => {
    const running = vi.spyOn(api, 'runningQuotes').mockResolvedValue({ items: [{ ...ROW, id: 'operation:op-1', subject: 'order_item:item-1', created_at: '2026-11-06T15:00:00Z' }], next_cursor: null })
    const failed = vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map([['item-1', { ...FAILED, id: 'operation:op-old', subject: 'order_item:item-1' }]]))
    const lost = new ApiError({ type: UNANSWERED, title: 'Bad Gateway', status: 502 })
    // Its first read finds it running; the follow's answer is then lost.
    const followed = vi.spyOn(api, 'followQuote').mockImplementation((id, _signal, onRunning) => {
      if (id !== 'op-1') return Promise.reject(new ApiError(422, 'An old failure.'))
      onRunning?.()
      return Promise.reject(lost)
    })
    const first = [ITEM] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ items }) => useSliceQuotes('order-1', items, vi.fn()), { initialProps: { items: first } })
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ detached: true, error: null }))
    act(() => result.current.dismiss('item-1'))
    // The reload with the row still running: its operation may still run, so it is
    // followed again, and the item's old failure does not show.
    rerender({ items: [...first] })
    await waitFor(() => expect(followed).toHaveBeenCalledTimes(2))
    await act(async () => {})
    expect(followed).not.toHaveBeenCalledWith('op-old', expect.anything())
    expect(failed).not.toHaveBeenCalled()
    expect(result.current.runs.get('item-1')?.error ?? null).toBeNull()
    expect(running).toHaveBeenCalledTimes(2)
  })

  it('keeps an aborted follow of one order out of the next order’s collections (#5596)', async () => {
    // A stale running row whose follow the order change aborts, then answers as a failure.
    const aborted = deferred<Quote>()
    const list = { items: [{ ...ROW, id: 'operation:op-shared', subject: 'order_item:item-1', created_at: '2026-11-06T15:00:00Z' }], next_cursor: null }
    const secondList = deferred<typeof list>()
    vi.spyOn(api, 'runningQuotes').mockResolvedValueOnce(list).mockReturnValueOnce(secondList.promise)
    vi.spyOn(api, 'failedQuotes').mockResolvedValue(new Map())
    const followed = vi.spyOn(api, 'followQuote').mockImplementationOnce(() => aborted.promise)
    const one = [ITEM] as unknown as OrderItemDetail[]
    const two = [{ ...ITEM, order_id: 'order-2' }] as unknown as OrderItemDetail[]
    const { result, rerender } = renderHook(({ id, items }) => useSliceQuotes(id, items, vi.fn()), { initialProps: { id: 'order-1', items: one } })
    await waitFor(() => expect(followed).toHaveBeenCalledTimes(1))
    // The next order lists the same row; its follow finds the operation running. The first
    // order's follow settles before that list arrives.
    followed.mockImplementation(stillRunning(() => new Promise<Quote>(() => undefined)))
    rerender({ id: 'order-2', items: two })
    await act(async () => aborted.reject(new ApiError(422, 'The first order’s follow failed.')))
    await act(async () => secondList.resolve(list))
    await waitFor(() => expect(followed).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(result.current.runs.get('item-1')).toMatchObject({ error: null }))
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
