import { act, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getRealtime, resetRealtime, type RealtimeSignal } from './realtime'
import { useAsync } from './useAsync'

let signals: ((signal: RealtimeSignal) => void)[] = []

beforeEach(() => {
  resetRealtime()
  signals = []
  vi.spyOn(getRealtime(), 'subscribe').mockImplementation((_topic, listener) => {
    signals.push(listener)
    return () => {}
  })
})

afterEach(() => {
  vi.restoreAllMocks()
  resetRealtime()
})

function deferred<T>() {
  const resolvers: ((value: T) => void)[] = []
  const load = () => new Promise<T>((resolve) => resolvers.push(resolve))
  return { load, resolvers }
}

it('reads once on mount and again on each signal, coalescing a burst', async () => {
  let reads = 0
  const { result } = renderHook(() => useAsync(() => Promise.resolve(++reads), [], ['models']))
  await waitFor(() => expect(result.current.data).toBe(1))
  act(() => {
    signals[0]?.('resync')
    signals[0]?.('resync')
    signals[0]?.('resync')
  })
  await waitFor(() => expect(result.current.data).toBe(2))
  expect(reads).toBe(2)
})

it('keeps the data on screen, not loading, while a refresh is in flight', async () => {
  const { load, resolvers } = deferred<string>()
  const { result } = renderHook(() => useAsync(load, [], ['m']))
  await act(async () => resolvers[0]?.('first'))
  act(() => signals[0]?.('resync'))
  await waitFor(() => expect(resolvers).toHaveLength(2))
  expect(result.current).toMatchObject({ data: 'first', loading: false })
  await act(async () => resolvers[1]?.('second'))
  expect(result.current.data).toBe('second')
})

it('never lets an older read overwrite a newer one', async () => {
  const { load, resolvers } = deferred<string>()
  const { result } = renderHook(() => useAsync(load, [], ['m']))
  await waitFor(() => expect(resolvers).toHaveLength(1))
  act(() => signals[0]?.('resync'))
  await waitFor(() => expect(resolvers).toHaveLength(2))
  await act(async () => {
    resolvers[1]?.('new')
    await Promise.resolve()
    resolvers[0]?.('old')
  })
  expect(result.current.data).toBe('new')
})

it('drops a refresh that was in flight when the deps changed', async () => {
  const resolvers: Record<string, ((value: string) => void)[]> = {}
  const { result, rerender } = renderHook(
    ({ slug }) =>
      useAsync(
        () => new Promise<string>((resolve) => (resolvers[slug] ??= []).push(resolve)),
        [slug],
        ['m'],
      ),
    { initialProps: { slug: 'a' } },
  )
  await act(async () => resolvers.a?.[0]?.('a1'))
  act(() => signals[0]?.('resync'))
  await waitFor(() => expect(resolvers.a).toHaveLength(2))
  rerender({ slug: 'b' })
  await act(async () => {
    resolvers.b?.[0]?.('b1')
    await Promise.resolve()
    resolvers.a?.[1]?.('a2')
  })
  expect(result.current.data).toBe('b1')
})

it('keeps the data on screen when a background read fails', async () => {
  let fail = false
  const { result } = renderHook(() =>
    useAsync(() => (fail ? Promise.reject(new Error('down')) : Promise.resolve('shown')), [], ['m']),
  )
  await waitFor(() => expect(result.current.data).toBe('shown'))
  fail = true
  act(() => signals[0]?.('resync'))
  await new Promise((resolve) => setTimeout(resolve, 20))
  expect(result.current).toMatchObject({ data: 'shown', error: undefined })
})

it('shows the error of a failed read when there is nothing on screen yet', async () => {
  const { result } = renderHook(() => useAsync(() => Promise.reject(new Error('down')), [], ['m']))
  act(() => signals[0]?.('resync'))
  await waitFor(() => expect(result.current.error?.message).toBe('down'))
})

it('applies a refresh only when accept says so as the answer lands', async () => {
  let n = 0
  const { result } = renderHook(() => useAsync(() => Promise.resolve(++n), [], ['m']))
  await waitFor(() => expect(result.current.data).toBe(1))
  const seen: number[] = []
  act(() =>
    result.current.refresh((data) => {
      seen.push(data)
      return false
    }),
  )
  await waitFor(() => expect(seen).toEqual([2]))
  expect(result.current.data).toBe(1)
  act(() => result.current.refresh(() => true))
  await waitFor(() => expect(result.current.data).toBe(3))
})

it('never asks accept about an answer a newer read has overtaken', async () => {
  const { load, resolvers } = deferred<string>()
  const { result } = renderHook(() => useAsync(load, [], ['m']))
  await act(async () => resolvers[0]?.('first'))
  const asked: string[] = []
  act(() => result.current.refresh((data) => (asked.push(data), true)))
  await waitFor(() => expect(resolvers).toHaveLength(2))
  act(() => result.current.reload())
  await waitFor(() => expect(resolvers).toHaveLength(3))
  await act(async () => {
    resolvers[2]?.('newest')
    await Promise.resolve()
    resolvers[1]?.('stale')
  })
  expect(result.current.data).toBe('newest')
  expect(asked).toEqual([])
})

it('never lets a read in flight overwrite data set since', async () => {
  const { load, resolvers } = deferred<string>()
  const { result } = renderHook(() => useAsync(load, [], ['m']))
  await act(async () => resolvers[0]?.('first'))
  act(() => result.current.refresh())
  await waitFor(() => expect(resolvers).toHaveLength(2))
  act(() => result.current.setData('set', { supersede: true }))
  await act(async () => resolvers[1]?.('stale'))
  expect(result.current.data).toBe('set')
})

it('lets a read in flight replace data set without supersede', async () => {
  const { load, resolvers } = deferred<string>()
  const { result } = renderHook(() => useAsync(load, [], ['m']))
  await act(async () => resolvers[0]?.('first'))
  act(() => result.current.refresh())
  await waitFor(() => expect(resolvers).toHaveLength(2))
  act(() => result.current.setData('set'))
  await act(async () => resolvers[1]?.('newer'))
  expect(result.current.data).toBe('newer')
})

it('never settles a new key, or cancels its load, with data set after the deps changed', async () => {
  const reads: Record<string, ((value: string) => void)[]> = { a: [], b: [] }
  const { result, rerender } = renderHook(
    ({ slug }) => useAsync(() => new Promise<string>((resolve) => reads[slug]!.push(resolve)), [slug]),
    { initialProps: { slug: 'a' } },
  )
  await act(async () => reads.a![0]?.('a data'))
  const fromA = result.current.setData
  rerender({ slug: 'b' })
  await waitFor(() => expect(reads.b).toHaveLength(1))
  act(() => fromA('a edited', { supersede: true }))
  expect(result.current).toMatchObject({ data: undefined, loading: true })
  await act(async () => reads.b![0]?.('b data'))
  expect(result.current).toMatchObject({ data: 'b data', loading: false })
})

it('tells refresh when its read fails', async () => {
  let fail = false
  const { result } = renderHook(() => useAsync(() => (fail ? Promise.reject(new Error('no')) : Promise.resolve(1)), []))
  await waitFor(() => expect(result.current.data).toBe(1))
  fail = true
  const failed = vi.fn()
  act(() => result.current.refresh(undefined, failed))
  await waitFor(() => expect(failed).toHaveBeenCalledOnce())
  expect(result.current.data).toBe(1)
})

it('tells every caller merged into one read that it failed', async () => {
  let fail = false
  const { result } = renderHook(() => useAsync(() => (fail ? Promise.reject(new Error('no')) : Promise.resolve(1)), []))
  await waitFor(() => expect(result.current.data).toBe(1))
  fail = true
  const first = vi.fn()
  const second = vi.fn()
  act(() => {
    result.current.refresh(undefined, first)
    result.current.refresh()
    result.current.refresh(undefined, second)
  })
  await waitFor(() => expect(second).toHaveBeenCalledOnce())
  expect(first).toHaveBeenCalledOnce()
})
