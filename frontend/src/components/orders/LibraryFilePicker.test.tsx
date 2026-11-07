import { act, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { api, ApiError } from '../../api/client'
import type { LibrarySearch } from '../../api/types'
import { resetMockState } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { LibraryFilePicker } from './LibraryFilePicker'

// #5544 — the Add Item dialog's library search: its states, and a failed search's error
// tied to the query that failed (the PR #5610 review).
describe('LibraryFilePicker (#5544)', () => {
  beforeEach(() => resetMockState())
  afterEach(() => vi.restoreAllMocks())

  const picker = () => <LibraryFilePicker open picked={null} disabled={false} onPick={vi.fn()} />

  it('drops a failed search’s error when the query changes or clears', async () => {
    const real = api.searchLibrary.bind(api)
    let release: () => void = () => {}
    const held = new Promise<void>((resolve) => {
      release = resolve
    })
    vi.spyOn(api, 'searchLibrary').mockImplementation(async (query, signal) => {
      if (query.q === 'drawer') throw new ApiError(502, 'Bambuddy did not answer.')
      await held
      return real(query, signal)
    })
    const { user } = renderPage(picker())
    const box = screen.getByLabelText('Library file')
    await user.type(box, 'drawer')
    expect(await screen.findByRole('alert')).toHaveTextContent('Could not search the library: Bambuddy did not answer.')
    // A new query: its search is in flight, so the old query's error must not show.
    await user.type(box, 'x')
    expect(await screen.findByText('Searching…')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
    release()
    await user.clear(box)
    await user.type(box, 'drawer')
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    // The box cleared: the hint, not the error.
    await user.clear(box)
    expect(await screen.findByText('Type part of a file or folder name.')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says when no library file matches', async () => {
    const { user } = renderPage(picker())
    await user.type(screen.getByLabelText('Library file'), 'zzzz-no-such-file')
    expect(await screen.findByText('No library file matches.')).toBeInTheDocument()
  })

  it('shows the first eight files and how many more there are', async () => {
    const sample = await api.searchLibrary({ q: 'drawer', all: false })
    const hit = sample.files?.[0]
    if (!hit) throw new Error('the mocked library has no drawer file')
    const files = Array.from({ length: 12 }, (_, index) => ({ ...hit, id: 1000 + index, filename: `part-${index}.3mf` }))
    vi.spyOn(api, 'searchLibrary').mockResolvedValue({ ...sample, query: 'part', files, total: 12 } satisfies LibrarySearch)
    const { user } = renderPage(picker())
    await user.type(screen.getByLabelText('Library file'), 'part')
    const list = await screen.findByRole('list', { name: 'Library files' })
    expect(within(list).getAllByRole('button')).toHaveLength(8)
    expect(within(list).getByText('4 more; narrow the search.')).toBeInTheDocument()
  })

  // #5633: a failed search can run again for the same query.
  describe('Retry', () => {
    beforeEach(() => {
      vi.useFakeTimers({ shouldAdvanceTime: true })
    })
    afterEach(() => {
      vi.useRealTimers()
    })

    /** Fails the first `failures` searches, then answers from the mocked library. */
    const failFirst = (failures: number) => {
      const real = api.searchLibrary.bind(api)
      let left = failures
      return vi.spyOn(api, 'searchLibrary').mockImplementation(async (query, signal) => {
        if (left > 0) {
          left -= 1
          throw new ApiError(502, 'Bambuddy did not answer.')
        }
        return real(query, signal)
      })
    }

    it('runs the same query again and shows its files', async () => {
      const search = failFirst(1)
      const { user } = renderPage(picker(), { userEventOptions: { advanceTimers: vi.advanceTimersByTime } })
      await user.type(screen.getByLabelText('Library file'), 'drawer')
      await act(() => vi.advanceTimersByTimeAsync(300))
      expect(await screen.findByRole('alert')).toHaveTextContent('Bambuddy did not answer.')
      expect(search).toHaveBeenCalledTimes(1)
      await user.click(screen.getByRole('button', { name: 'Retry' }))
      expect(await screen.findByRole('list', { name: 'Library files' })).toBeInTheDocument()
      expect(screen.queryByRole('alert')).toBeNull()
      expect(search).toHaveBeenCalledTimes(2)
      expect(search.mock.calls[1]?.[0]).toEqual({ q: 'drawer', all: false })
    })

    it('runs again after the query is edited and put back within the debounce', async () => {
      const search = failFirst(1)
      const { user } = renderPage(picker(), { userEventOptions: { advanceTimers: vi.advanceTimersByTime } })
      const box = screen.getByLabelText('Library file')
      await user.type(box, 'drawer')
      await act(() => vi.advanceTimersByTimeAsync(300))
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      // Edit and restore inside 250 ms: the asked query never changes, so nothing runs.
      await user.type(box, 'x')
      await user.type(box, '{Backspace}')
      await act(() => vi.advanceTimersByTimeAsync(300))
      expect(search).toHaveBeenCalledTimes(1)
      expect(screen.getByRole('alert')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Retry' }))
      expect(await screen.findByRole('list', { name: 'Library files' })).toBeInTheDocument()
      expect(search).toHaveBeenCalledTimes(2)
    })

    it('says Searching…, not an earlier answer for the same query (#5635)', async () => {
      const real = api.searchLibrary.bind(api)
      let drawerCalls = 0
      const never = new Promise<never>(() => {})
      vi.spyOn(api, 'searchLibrary').mockImplementation(async (query, signal) => {
        if (query.q !== 'drawer') return never
        drawerCalls += 1
        if (drawerCalls === 1) return real(query, signal)
        if (drawerCalls === 2) throw new ApiError(502, 'Bambuddy did not answer.')
        return never
      })
      const { user } = renderPage(picker(), { userEventOptions: { advanceTimers: vi.advanceTimersByTime } })
      const box = screen.getByLabelText('Library file')
      await user.type(box, 'drawer')
      await act(() => vi.advanceTimersByTimeAsync(300))
      expect(await screen.findByRole('list', { name: 'Library files' })).toBeInTheDocument()
      // Another query that never answers, then back: the second search for "drawer" fails.
      await user.type(box, 'x')
      await act(() => vi.advanceTimersByTimeAsync(300))
      await user.type(box, '{Backspace}')
      await act(() => vi.advanceTimersByTimeAsync(300))
      expect(await screen.findByRole('alert')).toBeInTheDocument()
      await user.click(screen.getByRole('button', { name: 'Retry' }))
      expect(await screen.findByText('Searching…')).toBeInTheDocument()
      expect(screen.queryByRole('list', { name: 'Library files' })).toBeNull()
      expect(drawerCalls).toBe(3)
    })
  })
})
