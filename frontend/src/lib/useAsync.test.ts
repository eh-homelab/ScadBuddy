import { act, renderHook, waitFor } from '@testing-library/react'
import { Activity, createElement, type ReactNode } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getRealtime, resetRealtime, type RealtimeListener } from './realtime'
import { CURSOR_HEADER, trackRequest } from './readCursor'
import { RESYNC_WAIT_MS, SIGNAL_READ_MIN_MS, SIGNAL_READ_STALL_MS, useAsync } from './useAsync'

let signals: RealtimeListener[] = []

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

// #4149 — however often a topic is signalled, one read is in flight and one waits behind
// it, each at least SIGNAL_READ_MIN_MS after the last: a signal can never become a storm.
it('keeps one signalled read in flight and one waiting, spaced by the minimum interval', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: false })
  try {
    const { load, resolvers } = deferred<number>()
    const { result } = renderHook(() => useAsync(load, [], ['models']))
    await act(async () => resolvers[0]?.(0))
    expect(result.current.data).toBe(0)
    // A signal while nothing is in flight reads at once.
    await act(async () => signals[0]?.('resync'))
    expect(resolvers).toHaveLength(2)
    // Signals while it is in flight all wait for one read behind it.
    await act(async () => {
      for (let i = 0; i < 50; i++) signals[0]?.('resync')
      await vi.advanceTimersByTimeAsync(SIGNAL_READ_MIN_MS * 4)
    })
    expect(resolvers).toHaveLength(2)
    await act(async () => resolvers[1]?.(1))
    expect(resolvers).toHaveLength(3)
    // Answered at once, the next waits out the interval since the last started.
    await act(async () => resolvers[2]?.(2))
    await act(async () => signals[0]?.('resync'))
    expect(resolvers).toHaveLength(3)
    await act(() => vi.advanceTimersByTimeAsync(SIGNAL_READ_MIN_MS))
    expect(resolvers).toHaveLength(4)
    await act(async () => resolvers[3]?.(3))
    expect(result.current.data).toBe(3)
    // A storm of signals reads once per interval. It starts right after a read and lasts
    // 400 ms, so exactly floor(400 / SIGNAL_READ_MIN_MS) more reads fit (#4166): a change
    // in how reads are spaced changes this count.
    for (let i = 0; i < 40; i++) {
      await act(async () => {
        signals[0]?.('resync')
        await vi.advanceTimersByTimeAsync(10)
        for (const resolve of resolvers) resolve(i)
      })
    }
    expect(resolvers).toHaveLength(4 + Math.floor((40 * 10) / SIGNAL_READ_MIN_MS))
  } finally {
    vi.useRealTimers()
  }
})

// #4165 — a read that never answers holds later signals back for SIGNAL_READ_STALL_MS
// at most, then the next read starts anyway: the view goes on following its topics.
it('starts a signalled read anyway once the read in flight has stalled', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: false })
  try {
    const { load, resolvers } = deferred<number>()
    renderHook(() => useAsync(load, [], ['models']))
    await act(async () => resolvers[0]?.(0))
    await act(async () => signals[0]?.('resync'))
    expect(resolvers).toHaveLength(2)
    // The read in flight never answers; a later signal waits out the spacing, then waits
    // on that read, but not for ever.
    await act(async () => signals[0]?.('resync'))
    await act(() => vi.advanceTimersByTimeAsync(SIGNAL_READ_MIN_MS + SIGNAL_READ_STALL_MS - 1))
    expect(resolvers).toHaveLength(2)
    await act(() => vi.advanceTimersByTimeAsync(1))
    expect(resolvers).toHaveLength(3)
  } finally {
    vi.useRealTimers()
  }
})

// #4165 — an effect replayed in place (here `<Activity>` hiding and showing the view;
// StrictMode and Fast Refresh do the same) must leave the hook hearing its topics.
it('hears its topics again after its effects are torn down and replayed in place', async () => {
  const { load, resolvers } = deferred<number>()
  let mode: 'visible' | 'hidden' = 'visible'
  const wrapper = ({ children }: { children: ReactNode }) => createElement(Activity, { mode, children })
  const { rerender } = renderHook(() => useAsync(load, [], ['models']), { wrapper })
  await act(async () => resolvers[0]?.(0))
  await act(async () => signals.at(-1)?.('resync'))
  expect(resolvers).toHaveLength(2)
  // A signal while that read is in flight waits behind it; then the view is hidden.
  await act(async () => signals.at(-1)?.('resync'))
  mode = 'hidden'
  rerender()
  await act(async () => resolvers[1]?.(1))
  mode = 'visible'
  rerender()
  const before = resolvers.length
  await act(async () => signals.at(-1)?.('resync'))
  await waitFor(() => expect(resolvers.length).toBeGreaterThan(before))
})

// #4291 review — the same replay, with the read in flight answering only once the view
// is shown again: the wait from before the teardown starts nothing beside the replay's.
it('starts no read for a wait left from before its effects were replayed', async () => {
  const { load, resolvers } = deferred<number>()
  let mode: 'visible' | 'hidden' = 'visible'
  const wrapper = ({ children }: { children: ReactNode }) => createElement(Activity, { mode, children })
  const { rerender } = renderHook(() => useAsync(load, [], ['models']), { wrapper })
  await act(async () => resolvers[0]?.(0))
  await act(async () => signals.at(-1)?.('resync'))
  expect(resolvers).toHaveLength(2)
  // Past the spacing, so this signal's wait is on the read in flight, not on a timer
  // the teardown clears.
  await act(() => new Promise((r) => setTimeout(r, SIGNAL_READ_MIN_MS + 50)))
  await act(async () => signals.at(-1)?.('resync'))
  mode = 'hidden'
  rerender()
  mode = 'visible'
  rerender()
  const before = resolvers.length
  await act(async () => resolvers[1]?.(1))
  expect(resolvers).toHaveLength(before)
  // The replay's own signal reads once.
  await act(async () => signals.at(-1)?.('resync'))
  await waitFor(() => expect(resolvers).toHaveLength(before + 1))
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

// A background read that lands after its component is gone must touch nothing: no
// state, no caller's `accept` or `failed`. A test file's environment may be torn down by
// then, and React's setState reads `window` (the unhandled "window is not defined" that
// failed whole CI runs after every test had passed).
it('does nothing with a background read that lands after unmount', async () => {
  let fail: (cause: Error) => void = () => {}
  let pass: (value: string) => void = () => {}
  let reads = 0
  const load = () => {
    reads += 1
    if (reads === 1) return Promise.resolve('first')
    return new Promise<string>((resolve, reject) => {
      if (reads === 2) fail = reject
      else pass = resolve
    })
  }
  // No state update either: React would warn (an act() or unmounted-update warning).
  const errors = vi.spyOn(console, 'error')
  // Restored even when an assertion fails, so the spy never leaks into later tests (#3111).
  try {
    const { result, unmount } = renderHook(() => useAsync(load, [], ['m']))
    await waitFor(() => expect(result.current.data).toBe('first'))
    const failed = vi.fn()
    const accept = vi.fn(() => true)
    act(() => result.current.refresh(undefined, failed))
    await waitFor(() => expect(reads).toBe(2))
    unmount()
    await act(async () => fail(new Error('the agent went away')))
    expect(failed).not.toHaveBeenCalled()

    const second = renderHook(() => useAsync(load, [], ['m']))
    await waitFor(() => expect(reads).toBe(3))
    await act(async () => pass('second'))
    act(() => second.result.current.refresh(accept))
    await waitFor(() => expect(reads).toBe(4))
    second.unmount()
    await act(async () => pass('late'))
    expect(accept).not.toHaveBeenCalled()
    expect(errors).not.toHaveBeenCalled()
  } finally {
    errors.mockRestore()
  }
})

describe('with the socket already open (#1039)', () => {
  beforeEach(() => {
    vi.spyOn(getRealtime(), 'willResync', 'get').mockReturnValue(true)
  })

  it("leaves the first read to the subscription's resync, reading once", async () => {
    let reads = 0
    const { result } = renderHook(() => useAsync(() => Promise.resolve(++reads), [], ['models']))
    expect(result.current.loading).toBe(true)
    await act(async () => {})
    expect(reads).toBe(0)

    act(() => signals[0]?.('resync'))

    await waitFor(() => expect(result.current).toMatchObject({ data: 1, loading: false }))
    expect(reads).toBe(1)
  })

  it('reads anyway when the resync does not come', async () => {
    vi.useFakeTimers()
    try {
      let reads = 0
      const { result } = renderHook(() => useAsync(() => Promise.resolve(++reads), [], ['models']))
      await act(async () => vi.advanceTimersByTimeAsync(RESYNC_WAIT_MS - 1))
      expect(reads).toBe(0)
      await act(async () => vi.advanceTimersByTimeAsync(1))
      expect(reads).toBe(1)
      expect(result.current.data).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('never reads again at the fallback once the resync has read', async () => {
    vi.useFakeTimers()
    try {
      let reads = 0
      renderHook(() => useAsync(() => Promise.resolve(++reads), [], ['models']))
      await act(async () => {
        signals[0]?.('resync')
        await vi.advanceTimersByTimeAsync(RESYNC_WAIT_MS * 2)
      })
      expect(reads).toBe(1)
    } finally {
      vi.useRealTimers()
    }
  })

  it('reads at once when only the deps change, since nothing is subscribed anew', async () => {
    let reads = 0
    const { result, rerender } = renderHook(
      ({ id }) => useAsync(() => Promise.resolve(`${id}:${++reads}`), [id], ['models']),
      { initialProps: { id: 'a' } },
    )
    act(() => signals[0]?.('resync'))
    await waitFor(() => expect(result.current.data).toBe('a:1'))

    rerender({ id: 'b' })

    await waitFor(() => expect(result.current.data).toBe('b:2'))
    expect(signals).toHaveLength(1)
  })

  it('reads once when the deps and the topic change together', async () => {
    let reads = 0
    const { result, rerender } = renderHook(
      ({ id }) => useAsync(() => Promise.resolve(`${id}:${++reads}`), [id], [`model:${id}`]),
      { initialProps: { id: 'a' } },
    )
    act(() => signals[0]?.('resync'))
    await waitFor(() => expect(result.current.data).toBe('a:1'))

    rerender({ id: 'b' })
    await act(async () => {})
    expect(reads).toBe(1)
    act(() => signals[1]?.('resync'))

    await waitFor(() => expect(result.current.data).toBe('b:2'))
    expect(reads).toBe(2)
  })

  it('reads at once with no topics', async () => {
    let reads = 0
    const { result } = renderHook(() => useAsync(() => Promise.resolve(++reads), []))
    await waitFor(() => expect(result.current.data).toBe(1))
  })
})

describe('on a cold load, the confirmation is checked against the first read (#1039)', () => {
  /** A load whose one request answers with `cursor` (none when null). */
  function reading(cursor: string | null) {
    let reads = 0
    const load = () => {
      const answered = trackRequest()
      return Promise.resolve().then(() => {
        answered(new Headers(cursor === null ? {} : { [CURSOR_HEADER]: cursor }))
        return ++reads
      })
    }
    return { load, reads: () => reads }
  }

  it("reads once when the first read saw the server's last event", async () => {
    const check = vi.spyOn(getRealtime(), 'check')
    const { load, reads } = reading('e5')
    const { result } = renderHook(() => useAsync(load, [], ['models']))
    await waitFor(() => expect(result.current.data).toBe(1))
    await act(async () => signals[0]?.('resync', { last: 'e5' }))
    expect(reads()).toBe(1)
    expect(check).not.toHaveBeenCalled()
  })

  it('asks the server, and reads again only for a topic it names', async () => {
    const check = vi.spyOn(getRealtime(), 'check').mockResolvedValueOnce([]).mockResolvedValueOnce(['fonts'])
    const { load, reads } = reading('e5')
    renderHook(() => useAsync(load, [], ['models', 'fonts']))
    await waitFor(() => expect(reads()).toBe(1))
    await act(async () => signals[0]?.('resync', { last: 'e7' }))
    expect(check).toHaveBeenLastCalledWith(['e5'], ['models'])
    expect(reads()).toBe(1)
    await act(async () => signals[1]?.('resync', { last: 'e7' }))
    expect(check).toHaveBeenLastCalledWith(['e5'], ['fonts'])
    await waitFor(() => expect(reads()).toBe(2))
  })

  it('checks a later confirmation against the newest read shown, not the first', async () => {
    const check = vi.spyOn(getRealtime(), 'check')
    let cursor = 'e5'
    let reads = 0
    const load = () => {
      const answered = trackRequest()
      const mine = cursor
      return Promise.resolve().then(() => {
        answered(new Headers({ [CURSOR_HEADER]: mine }))
        return ++reads
      })
    }
    const { result } = renderHook(() => useAsync(load, [], ['models']))
    await waitFor(() => expect(result.current.data).toBe(1))
    cursor = 'e8'
    await act(async () => signals[0]?.({ id: 'e8', kind: 'model.updated', topics: ['models'], data: {} }))
    await waitFor(() => expect(result.current.data).toBe(2))
    // A reconnect: the server's last is what the refresh saw.
    await act(async () => signals[0]?.('resync', { last: 'e8' }))
    expect(reads).toBe(2)
    expect(check).not.toHaveBeenCalled()
  })

  it('waits for a first read still in flight before it decides', async () => {
    const check = vi.spyOn(getRealtime(), 'check')
    let answer: (value: number) => void = () => {}
    const load = () => {
      const answered = trackRequest()
      return new Promise<number>((resolve) => {
        answer = (value) => {
          answered(new Headers({ [CURSOR_HEADER]: 'e5' }))
          resolve(value)
        }
      })
    }
    const { result } = renderHook(() => useAsync(load, [], ['models']))
    await act(async () => signals[0]?.('resync', { last: 'e5' }))
    await act(async () => answer(1))
    expect(result.current.data).toBe(1)
    expect(check).not.toHaveBeenCalled()
  })

  it('reads again when the first read saw no cursor, or failed', async () => {
    const check = vi.spyOn(getRealtime(), 'check')
    const { load, reads } = reading(null)
    renderHook(() => useAsync(load, [], ['models']))
    await waitFor(() => expect(reads()).toBe(1))
    await act(async () => signals[0]?.('resync', { last: 'e5' }))
    await waitFor(() => expect(reads()).toBe(2))

    let tries = 0
    signals = []
    renderHook(() =>
      useAsync(() => (++tries === 1 ? Promise.reject(new Error('down')) : Promise.resolve(tries)), [], ['fonts']),
    )
    await waitFor(() => expect(tries).toBe(1))
    await act(async () => signals[0]?.('resync', { last: 'e5' }))
    await waitFor(() => expect(tries).toBe(2))
    expect(check).not.toHaveBeenCalled()
  })
})
