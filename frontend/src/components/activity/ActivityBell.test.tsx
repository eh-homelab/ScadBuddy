import { act, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { Activity } from '../../api/types'
import { api } from '../../api/client'
import { fakeRealtime } from '../../lib/realtime.fake'
import { getRealtime } from '../../lib/realtime'
import { seedActivity, setActivityListCursor } from '../../mocks/features/activity'
import { mockSettings, resetMockState, setMockSettings } from '../../mocks/handlers'
import { clearToasts } from '../../lib/toast'
import { renderPage } from '../../test/utils'
import { Toasts } from '../ui/Toasts'
import { ActivityBell } from './ActivityBell'

function finished(principal: string): Activity {
  const now = new Date(Date.now() + 1000).toISOString()
  return {
    id: 'render:x',
    kind: 'render',
    status: 'succeeded',
    title: 'Render box',
    principal,
    session: null,
    via_tool: null,
    subject: 'box',
    parent_id: null,
    detail: {},
    created_at: now,
    updated_at: now,
    finished_at: now,
  }
}

describe('ActivityBell', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true })
    clearToasts()
    resetMockState()
    window.localStorage.clear()
    seedActivity([])
  })
  afterEach(() => {
    vi.restoreAllMocks()
    vi.useRealTimers()
  })

  it('counts finished activity from followed principals and toasts on finish', async () => {
    const live = fakeRealtime()
    const { user } = renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('browser')])
    await live.signal('activity', 'activity.changed', { activity_id: 'render:x', principal: 'browser' })
    expect(await screen.findByText(/Render box: succeeded/)).toBeInTheDocument()
    expect(screen.getByTestId('activity-bell-count')).toHaveTextContent('1')
    await user.click(screen.getByRole('button', { name: /Activity, 1 finished/ }))
    expect(screen.getByTestId('activity-bell-count')).not.toBeVisible()
    expect(screen.getByText(/Render box/, { selector: 'li span' })).toBeInTheDocument()
  })

  describe("and the activity subscription's confirmation (#1039)", () => {
    afterEach(() => setActivityListCursor(undefined))

    /** How many times the bell reads its followed activity. */
    function countReads() {
      const list = vi.spyOn(api, 'listActivity')
      return () => list.mock.calls.length
    }

    it("reads once when its read saw the server's last event", async () => {
      setActivityListCursor('e5')
      const live = fakeRealtime({ confirmation: { last: 'e5' } })
      const check = vi.spyOn(getRealtime(), 'check')
      const reads = countReads()
      renderPage(<ActivityBell />)
      await waitFor(() => expect(live.following()).toContain('activity'))
      await waitFor(() => expect(reads()).toBe(1))
      await act(async () => vi.advanceTimersByTimeAsync(5_000))
      expect(reads()).toBe(1)
      expect(check).not.toHaveBeenCalledWith(expect.anything(), ['activity'])
    })

    it('reads again when the server says activity changed since', async () => {
      setActivityListCursor('e5')
      const live = fakeRealtime({ confirmation: { last: 'e9' } })
      vi.spyOn(getRealtime(), 'check').mockImplementation(async (_since, topics) => topics)
      const reads = countReads()
      renderPage(<ActivityBell />)
      await waitFor(() => expect(live.following()).toContain('activity'))
      await act(async () => vi.advanceTimersByTimeAsync(5_000))
      expect(reads()).toBe(2)
    })
  })

  // #3706: a read that settles after the bell is gone, as a test's teardown leaves one, must
  // set no state. React's dispatchSetState reads `window` first, so with the environment torn
  // down (no `window`) any update threw "window is not defined" after every test had passed.
  describe('after it is unmounted (#3706)', () => {
    function deferred<T>() {
      let resolve!: (value: T) => void
      const promise = new Promise<T>((r) => (resolve = r))
      return { promise, resolve }
    }
    /** Settles `settle` with `window` gone, and returns what went unhandled meanwhile. */
    async function withoutWindow(settle: () => void): Promise<unknown[]> {
      const unhandled: unknown[] = []
      const onRejection = (reason: unknown) => void unhandled.push(reason)
      process.on('unhandledRejection', onRejection)
      vi.stubGlobal('window', undefined)
      try {
        settle()
        for (let i = 0; i < 10; i++) await Promise.resolve()
      } finally {
        vi.unstubAllGlobals()
      }
      await new Promise((r) => setTimeout(r, 0))
      process.off('unhandledRejection', onRejection)
      return unhandled
    }

    it('sets nothing when the settings read settles', async () => {
      const settings = deferred<Awaited<ReturnType<typeof api.getSettings>>>()
      vi.spyOn(api, 'getSettings').mockReturnValue(settings.promise)
      const { unmount } = renderPage(<ActivityBell />)
      unmount()
      expect(await withoutWindow(() => settings.resolve(mockSettings()))).toEqual([])
    })

    it('sets nothing, and toasts nothing, when an activity read settles', async () => {
      const page = deferred<Awaited<ReturnType<typeof api.listActivity>>>()
      const list = vi.spyOn(api, 'listActivity').mockReturnValue(page.promise)
      const { unmount } = renderPage(<ActivityBell />)
      await waitFor(() => expect(list).toHaveBeenCalled())
      unmount()
      expect(await withoutWindow(() => page.resolve({ items: [finished('browser')], next: null } as never))).toEqual([])
    })
  })

  it('ignores principals not followed', async () => {
    const live = fakeRealtime()
    renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('token:t1')])
    await live.signal('activity', 'activity.changed', { activity_id: 'render:x', principal: 'token:t1' })
    await act(async () => {
      await vi.advanceTimersByTimeAsync(800)
    })
    expect(screen.getByTestId('toasts')).toBeEmptyDOMElement()
    expect(screen.getByTestId('activity-bell-count')).not.toBeVisible()
  })

  it('follows what the setting names', async () => {
    setMockSettings({ ...mockSettings(), activity_follow: ['token:t1'] })
    const live = fakeRealtime()
    renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('token:t1')])
    await live.signal('activity', 'activity.changed', { activity_id: 'render:x', principal: 'token:t1' })
    expect(await screen.findByText(/Render box: succeeded/)).toBeInTheDocument()
  })

  const mount = () =>
    renderPage(
      <>
        <ActivityBell />
        <Toasts />
      </>,
    )
  const advance = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms)
    })

  it('costs one read for a burst of signals', async () => {
    const live = fakeRealtime()
    const list = vi.spyOn(api, 'listActivity')
    mount()
    await waitFor(() => expect(live.following()).toContain('activity'))
    await advance(600)
    list.mockClear()
    for (let i = 0; i < 5; i++) await live.signal('activity', 'activity.changed', { principal: 'browser' })
    await advance(600)
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('still reads at least every two seconds while signals keep coming', async () => {
    const live = fakeRealtime()
    const list = vi.spyOn(api, 'listActivity')
    mount()
    await waitFor(() => expect(live.following()).toContain('activity'))
    await advance(600)
    list.mockClear()
    for (let i = 0; i < 7; i++) {
      await live.signal('activity', 'activity.changed', { principal: 'browser' })
      await advance(400)
    }
    expect(list.mock.calls.length).toBeGreaterThanOrEqual(1)
  })

  it('writes the first seen marker and honours it across mounts', async () => {
    fakeRealtime()
    const first = mount()
    await waitFor(() => expect(Number(window.localStorage.getItem('scadbuddy.activity.seen'))).toBeGreaterThan(0))
    first.unmount()
    // Finished while the page was closed, after the marker: counted, not announced.
    const marker = Date.now() - 60_000
    window.localStorage.setItem('scadbuddy.activity.seen', String(marker))
    seedActivity([{ ...finished('browser'), finished_at: new Date(Date.now() - 30_000).toISOString() }])
    mount()
    await waitFor(() => expect(screen.getByTestId('activity-bell-count')).toHaveTextContent('1'))
    expect(screen.getByTestId('toasts')).toBeEmptyDOMElement()
  })

  it('works with localStorage blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked')
    })
    const live = fakeRealtime()
    mount()
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('browser')])
    await live.signal('activity', 'activity.changed', { principal: 'browser' })
    expect(await screen.findByText(/Render box: succeeded/)).toBeInTheDocument()
    expect(screen.getByTestId('activity-bell-count')).toHaveTextContent('1')
  })

  it('dismisses a toast after six seconds, but not while it is hovered', async () => {
    const live = fakeRealtime()
    const { user } = mount()
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('browser')])
    await live.signal('activity', 'activity.changed', { principal: 'browser' })
    const shown = await screen.findByText(/Render box: succeeded/)
    await user.hover(shown)
    await advance(7000)
    expect(screen.getByText(/Render box: succeeded/)).toBeInTheDocument()
    await user.unhover(shown)
    await advance(6100)
    expect(screen.getByTestId('toasts')).toBeEmptyDOMElement()
  })

  it('keeps a toast while it is focused even after the pointer leaves', async () => {
    const live = fakeRealtime()
    const { user } = mount()
    await waitFor(() => expect(live.following()).toContain('activity'))
    seedActivity([finished('browser')])
    await live.signal('activity', 'activity.changed', { principal: 'browser' })
    const link = await screen.findByText(/Render box: succeeded/)
    await user.hover(link)
    await act(async () => link.focus())
    await user.unhover(link)
    await advance(7000)
    expect(screen.getByText(/Render box: succeeded/)).toBeInTheDocument()
    await act(async () => link.blur())
    await advance(6100)
    expect(screen.getByTestId('toasts')).toBeEmptyDOMElement()
  })

  it('follows a list saved in Settings without a reload', async () => {
    const live = fakeRealtime()
    mount()
    await waitFor(() => expect(live.following()).toContain('settings'))
    setMockSettings({ ...mockSettings(), activity_follow: ['token:t1'] })
    await live.signal('settings', 'settings.changed')
    await advance(50)
    seedActivity([finished('token:t1')])
    await live.signal('activity', 'activity.changed', { principal: 'token:t1' })
    expect(await screen.findByText(/Render box: succeeded/)).toBeInTheDocument()
  })

  it('closes the popover on Escape and outside clicks, and names it with aria-controls', async () => {
    fakeRealtime()
    const { user } = mount()
    const bell = screen.getByRole('button', { name: /Activity, 0 finished/ })
    await user.click(bell)
    expect(bell).toHaveAttribute('aria-controls', screen.getByText('All activity').closest('[data-slot="popover-content"]')!.id)
    await user.keyboard('{Escape}')
    expect(screen.queryByText('All activity')).toBeNull()
    expect(bell).toHaveFocus()
    await user.click(bell)
    await user.click(document.body)
    expect(screen.queryByText('All activity')).toBeNull()
  })
})
