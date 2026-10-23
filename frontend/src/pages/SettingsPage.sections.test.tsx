import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { UserEvent } from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { BrowserRouter, MemoryRouter, Link, Route, Routes, useLocation, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { api } from '../api/client'
import type { Settings } from '../api/types'
import { restartMockBackend } from '../mocks/features/settings'
import { mockSettings, setMockRemembered, setMockSettings } from '../mocks/handlers'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'
import { SETTLE_MS } from './settings/sectionNav'
import { choose, optionDisabled } from '../test/select'

// #322 — every runtime setting in the UI, with where its value came from; a save per
// section; the remembered choices; About.

async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
}

/**
 * Moves the fake clock 20 ms at a time until `check` passes, at most half of SETTLE_MS
 * per call, so a timer due at SETTLE_MS cannot fire before the test means it to. The
 * budget is not shared between calls: only the steps taken after that timer is scheduled
 * count towards it.
 */
async function stepUntil(check: () => void): Promise<void> {
  for (let moved = 0; ; moved += 20) {
    try {
      check()
      return
    } catch (error) {
      if (moved >= SETTLE_MS / 2) throw error
    }
    await act(() => vi.advanceTimersByTimeAsync(20))
  }
}

/** `text` in `field` as one paste: each keystroke re-renders all of Settings (#1485). */
async function enter(user: UserEvent, field: HTMLElement, text: string) {
  await user.click(field)
  await user.paste(text)
}

/**
 * A section, by its heading. A role query over all of Settings (~1000 elements) runs
 * jsdom's getComputedStyle on each of them, ~0.5 s after every change, and these tests
 * ran into their 5 s under load (#1485); so buttons are looked for in their section.
 */
function region(name: string): HTMLElement {
  const section = screen.getByText(name, { selector: 'h2 button' }).closest('section')
  if (!section) throw new Error(`no section headed ${name}`)
  return section
}

// #1307 — a link to a section (`/settings#libraries`, the assistant's Touched list) opens
// Settings scrolled to it, once the page has loaded. #2345 — only the pane scrolls, never
// the window, and the section stays in view while the sections above it load; #2346 — a
// section link puts its section in the address, focuses it, and marks it current.
describe('SettingsPage section links (#1307, #2345, #2346)', () => {
  /**
   * jsdom lays nothing out: each section is given a top (its id's entry in `tops`, read at
   * each call), and the pane's scrolls are recorded. `scrollIntoView` must not be called:
   * it scrolls the window too.
   */
  function stubLayout(tops: Record<string, number>, onScroll?: (top: number) => void) {
    const scrolls: number[] = []
    const intoView = vi.fn()
    const proto = Element.prototype as unknown as Record<string, unknown>
    const saved = { rect: proto.getBoundingClientRect, scrollTo: proto.scrollTo, intoView: proto.scrollIntoView }
    proto.getBoundingClientRect = function (this: Element) {
      const top = tops[this.id] ?? 0
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) }
    }
    proto.scrollTo = function (this: Element, options: ScrollToOptions) {
      if (!this.classList.contains('sb-settings')) return
      scrolls.push(options.top ?? 0)
      onScroll?.(options.top ?? 0)
    }
    proto.scrollIntoView = intoView
    const restore = () => {
      proto.getBoundingClientRect = saved.rect
      if (saved.scrollTo === undefined) delete proto.scrollTo
      else proto.scrollTo = saved.scrollTo
      if (saved.intoView === undefined) delete proto.scrollIntoView
      else proto.scrollIntoView = saved.intoView
    }
    return { scrolls, intoView, restore }
  }

  /**
   * A ResizeObserver jsdom lacks: each one's callback, and whether it was disconnected.
   * Only those on the pane's content are listed: Radix measures its own controls too.
   */
  function stubResizeObserver() {
    const observers: { fire: () => void; disconnected: boolean }[] = []
    class Fake {
      entry: { fire: () => void; disconnected: boolean }
      constructor(callback: () => void) {
        this.entry = { fire: callback, disconnected: false }
      }
      observe(target: Element) {
        if (target.parentElement?.classList.contains('sb-settings')) observers.push(this.entry)
      }
      unobserve() {}
      disconnect() {
        this.entry.disconnected = true
      }
    }
    vi.stubGlobal('ResizeObserver', Fake)
    return observers
  }

  /** Where the router is, rendered beside the page. */
  function Where() {
    const location = useLocation()
    return <p data-testid="where">{location.hash}</p>
  }

  it('scrolls only the pane to the section the URL names, under the nav', async () => {
    const { scrolls, intoView, restore } = stubLayout({ libraries: 5000 })
    try {
      renderPage(<SettingsPage />, { route: '/settings#libraries' })
      await seeded()
      // 8 px under the nav (no height in jsdom).
      await waitFor(() => expect(scrolls).toContain(4992))
      expect(intoView).not.toHaveBeenCalled()
      // The sr-only boxes are placed in the pane, so the document is no taller than the window.
      expect(document.querySelector('.sb-settings')).toHaveClass('relative')
    } finally {
      restore()
    }
  })

  it.each(['wheel', 'touchstart', 'keydown', 'pointerdown'])(
    'keeps a deep-linked section in view while the sections above it load, until a %s',
    async (stop) => {
      const tops = { libraries: 5000 }
      const { scrolls, restore } = stubLayout(tops)
      const observers = stubResizeObserver()
      try {
        renderPage(<SettingsPage />, { route: '/settings#libraries' })
        await seeded()
        await waitFor(() => expect(scrolls).toContain(4992))
        // The first is the deep link's follow; the section-in-view one comes after it.
        const observer = observers[0]!
        // Remembered choices finish loading above it and push it down.
        tops.libraries = 6900
        act(() => observer.fire())
        expect(scrolls.at(-1)).toBe(6892)
        document.querySelector('.sb-settings')!.dispatchEvent(new Event(stop))
        expect(observer.disconnected).toBe(true)
      } finally {
        restore()
        vi.unstubAllGlobals()
      }
    },
  )

  it('stops keeping a deep-linked section in view after SETTLE_MS (#2947 review)', async () => {
    const observers = stubResizeObserver()
    // The settle timer is scheduled right after the deep link's scroll, at the same
    // instant, which `stepUntil` may step past. So the boundary is pinned from there
    // (#3904 review): two timers set at the scroll look at that scroll's observer (made
    // next) just before and just after SETTLE_MS.
    const seen: (boolean | 'no observer')[] = []
    const { scrolls, restore } = stubLayout({ libraries: 5000 }, (top) => {
      if (top !== 4992) return
      const at = observers.length
      for (const delay of [SETTLE_MS - 1, SETTLE_MS + 1]) {
        setTimeout(() => seen.push(observers[at]?.disconnected ?? 'no observer'), delay)
      }
    })
    // Time itself, not one timer picked out by its closure's text (#3247 review), and
    // only the time the test moves (#3804 review): a clock that also followed the wall
    // could pass SETTLE_MS on a loaded machine before the first check.
    vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
    try {
      renderPage(<SettingsPage />, { route: '/settings#libraries' })
      // The page's loads wait on timers too (the mock API's delays), so move the clock
      // in small steps, never near SETTLE_MS, until each step shows.
      await stepUntil(() =>
        expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
      )
      await stepUntil(() => expect(scrolls).toContain(4992))
      expect(scrolls.filter((top) => top === 4992)).toHaveLength(1)
      expect(observers[0]!.disconnected).toBe(false)
      act(() => vi.advanceTimersByTime(SETTLE_MS + 1))
      expect(seen).toEqual([false, true])
      expect(observers[0]!.disconnected).toBe(true)
    } finally {
      vi.useRealTimers()
      restore()
      vi.unstubAllGlobals()
    }
  })

  it('marks the section that a load above moves into view current, without a scroll (#2947 review)', async () => {
    const tops: Record<string, number> = {}
    const { restore } = stubLayout(tops)
    const observers = stubResizeObserver()
    try {
      renderPage(<SettingsPage />, { route: '/settings' })
      await seeded()
      const nav = screen.getByRole('navigation', { name: 'Settings sections' })
      const ids = within(nav)
        .getAllByRole('link')
        .map((link) => link.getAttribute('href')!.slice(1))
      const at = ids.indexOf('libraries')
      // Each section 1000 px below the last, the first at the top.
      ids.forEach((id, index) => (tops[id] = index * 1000))
      act(() => {
        for (const observer of observers) observer.fire()
      })
      const libraries = within(nav).getByRole('link', { name: 'Libraries' })
      expect(libraries).not.toHaveAttribute('aria-current')
      // Everything above Libraries shrinks as it loads: Libraries is now at the top.
      ids.forEach((id, index) => (tops[id] = (index - at) * 1000))
      act(() => {
        for (const observer of observers) observer.fire()
      })
      expect(libraries).toHaveAttribute('aria-current', 'location')
    } finally {
      restore()
      vi.unstubAllGlobals()
    }
  })

  it('a section link on a page with unsaved changes scrolls there without a hash, so one Back is still held (#2947 review)', async () => {
    const { scrolls, restore } = stubLayout({ libraries: 5000 })
    window.history.replaceState(null, '', '/')
    window.history.pushState(null, '', '/settings')
    try {
      const user = userEvent.setup()
      render(
        <BrowserRouter>
          <Routes>
            <Route path="/settings" element={<SettingsPage />} />
            <Route path="/" element={<p>The catalogue</p>} />
          </Routes>
        </BrowserRouter>,
      )
      await seeded()
      await user.clear(screen.getByLabelText('Render timeout'))
      await user.type(screen.getByLabelText('Render timeout'), '45')

      const nav = screen.getByRole('navigation', { name: 'Settings sections' })
      await user.click(within(nav).getByRole('link', { name: 'Libraries' }))
      expect(scrolls).toContain(4992)
      await waitFor(() => expect(document.activeElement).toBe(document.getElementById('libraries-heading')))
      expect(window.location.hash).toBe('')

      act(() => window.history.back())
      expect(await screen.findByRole('alertdialog', { name: 'Leave without saving?' })).toBeInTheDocument()
      expect(window.location.pathname).toBe('/settings')
    } finally {
      restore()
    }
  })

  it('unfolds a deep-linked group inside a folded section, and scrolls to it once laid out (#3414)', async () => {
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['assistant', 'assistant-mcp']))
    const tops: Record<string, number> = {}
    const { scrolls, restore } = stubLayout(tops)
    const observers = stubResizeObserver()
    try {
      renderPage(<SettingsPage />, { route: '/settings#assistant-mcp' })
      await seeded()
      const group = await screen.findByRole('group', { name: 'MCP access' })
      await waitFor(() => expect(screen.getByRole('button', { name: 'MCP access' })).toHaveAttribute('aria-expanded', 'true'))
      expect(group.closest('section')!.querySelector('h2 button')).toHaveAttribute('aria-expanded', 'true')
      // Measured while its section was folded, the group had no box; laid out, it has one,
      // and the section nav's resize follow-up scrolls the pane there.
      tops['assistant-mcp'] = 7000
      act(() => {
        for (const observer of observers) if (!observer.disconnected) observer.fire()
      })
      expect(scrolls.at(-1)).toBe(6992)
    } finally {
      restore()
      vi.unstubAllGlobals()
      localStorage.clear()
    }
  })

  it('still opens, unscrolled, at a hash with a malformed escape (#2113)', async () => {
    const { scrolls, restore } = stubLayout({})
    try {
      renderPage(<SettingsPage />, { route: '/settings#%E0%A4%A' })
      await seeded()
      expect(scrolls).toEqual([])
    } finally {
      restore()
    }
  })

  it('leaves the page at the top without a hash', async () => {
    const { scrolls, restore } = stubLayout({})
    try {
      renderPage(<SettingsPage />, { route: '/settings' })
      await seeded()
      expect(scrolls).toEqual([])
    } finally {
      restore()
    }
  })

  it('a section link puts it in the address, focuses its heading and marks it current', async () => {
    const { scrolls, restore } = stubLayout({ libraries: 5000 })
    try {
      const { user } = renderPage(
        <>
          <SettingsPage />
          <Where />
        </>,
        { route: '/settings' },
      )
      await seeded()
      const nav = screen.getByRole('navigation', { name: 'Settings sections' })
      const link = within(nav).getByRole('link', { name: 'Libraries' })
      await user.click(link)
      expect(screen.getByTestId('where')).toHaveTextContent('#libraries')
      await waitFor(() => expect(document.activeElement).toBe(document.getElementById('libraries-heading')))
      expect(scrolls).toContain(4992)
      expect(link).toHaveAttribute('aria-current', 'location')
      expect(within(nav).getByRole('link', { name: 'Connection' })).not.toHaveAttribute('aria-current')
    } finally {
      restore()
    }
  })
})

describe('SettingsPage field checks and descriptions (#2347, #2348, #2456)', () => {
  it('refuses a negative or fractional number before sending, and ties the error to the field', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    const renders = screen.getByLabelText('Renders at once')
    // The help is read with the field, before any error.
    const helpId = timeout.getAttribute('aria-describedby')
    expect(helpId).toBe('setting-render_timeout-help')
    expect(document.getElementById(helpId!)).toBeInTheDocument()
    expect(timeout).not.toHaveAttribute('aria-invalid')

    await user.clear(timeout)
    await enter(user, timeout, '-5')
    await user.clear(renders)
    await enter(user, renders, '1.5')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))

    expect(await screen.findByText('Enter 0 or more.')).toBeInTheDocument()
    expect(screen.getByText('Enter a whole number.')).toBeInTheDocument()
    expect(put).not.toHaveBeenCalled()
    expect(timeout).toHaveAttribute('aria-invalid', 'true')
    expect(timeout).toHaveAttribute('aria-describedby', 'setting-render_timeout-help setting-render_timeout-error')
    expect(document.getElementById('setting-render_timeout-error')).toHaveTextContent('Enter 0 or more.')
    // The help stays beside the error.
    expect(document.getElementById('setting-render_timeout-help')).toBeInTheDocument()
    put.mockRestore()
  })

  it('names one landmark Assistant: the status inside it is "Assistant status"', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const assistant = document.getElementById('assistant')!
    expect(assistant).toHaveAccessibleName('Assistant')
    const status = document.getElementById('ai-status-heading')!.closest('[role="region"]')!
    expect(status).toHaveAccessibleName('Assistant status')
  })
})

describe('SettingsPage sources (#322)', () => {
  it('badges each field with where its value comes from', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByTestId('source-bambuddy_url')).toHaveTextContent('Set here')
    expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL')
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('From SCADBUDDY_RENDER_TIMEOUT')
    expect(screen.getByTestId('source-default_plate')).toHaveTextContent('Default')
    const cleared = screen.getByTestId('source-google_fonts_api_key')
    expect(cleared).toHaveTextContent('Cleared')
    expect(cleared).toHaveTextContent('SCADBUDDY_GOOGLE_FONTS_API_KEY is ignored')
    // Only a stored or cleared value has something to reset.
    expect(within(cleared).getByRole('button', { name: 'Reset to deployment value: Google Fonts API key' })).toBeInTheDocument()
    expect(within(screen.getByTestId('source-public_url')).queryByRole('button')).toBeNull()
  })

  it('resets a saved value back to the deployment value', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    // This page is ~1000 elements, and it took most of the test's 5 s under load (#1485):
    // each keystroke re-renders all of it, and a role query over all of it runs jsdom's
    // getComputedStyle on every element (~0.5 s). So the value is pasted, and the buttons
    // are looked for in their section and badge. Reset is the point here.
    const field = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(field)
    await user.click(field)
    await user.paste('https://mine.test')
    await user.click(within(field.closest('section')!).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('Set here'))

    const put = vi.spyOn(api, 'putSettings')
    await user.click(
      within(screen.getByTestId('source-public_url')).getByRole('button', { name: 'Reset to deployment value: ScadBuddy’s own URL' }),
    )
    await waitFor(() => expect(screen.getByTestId('source-public_url')).toHaveTextContent('From SCADBUDDY_PUBLIC_URL'))
    expect(put).toHaveBeenCalledWith({ reset: ['public_url'] })
    await waitFor(() => expect(field).toHaveValue('https://scadbuddy.internal.nullreference.io'))
    put.mockRestore()
  })

  it('resets a cleared key so the deployment one applies again', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.click(within(screen.getByTestId('source-google_fonts_api_key')).getByRole('button', { name: 'Reset to deployment value: Google Fonts API key' }))
    await waitFor(() =>
      expect(screen.getByTestId('source-google_fonts_api_key')).toHaveTextContent('From SCADBUDDY_GOOGLE_FONTS_API_KEY'),
    )
    expect(screen.getByLabelText('Google Fonts API key')).toHaveAttribute(
      'placeholder',
      expect.stringContaining('A key is stored'),
    )
  })

  it('says a restart-only field waits for a restart once saved', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByTestId('source-render_concurrency')).toHaveTextContent('Applies on restart')
    const field = screen.getByLabelText('Renders at once')
    await user.clear(field)
    await user.type(field, '4')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() =>
      expect(screen.getByTestId('source-render_concurrency')).toHaveTextContent('Saved; restart ScadBuddy to apply'),
    )
    restartMockBackend()
  })
})

describe('SettingsPage sections (#322)', () => {
  it('saves one section and leaves another one’s edits unsaved', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await choose(user, screen.getByLabelText('Show dimensions in'), 'Inches (in)')
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Preview')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Connection')).queryByText('Unsaved')).toBeNull()

    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    // Only what changed in that section: the rest of the page is not posted back.
    expect(put.mock.calls[0]?.[0]).toEqual({ render_timeout: 45 })
    await waitFor(() => expect(within(region('Rendering')).queryByText('Unsaved')).toBeNull())
    expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here')
    // The other section kept its edit.
    expect(screen.getByLabelText('Show dimensions in')).toHaveTextContent('Inches (in)')
    expect(within(region('Preview')).getByText('Unsaved')).toBeInTheDocument()
    put.mockRestore()
  })

  it('keeps an edit typed while its section is saving (#767)', async () => {
    // Held open so the edit lands mid-save; the save's seed must not take it back.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      await held
      return saved
    })
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await user.clear(timeout)
    await user.type(timeout, '50')

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(timeout).toHaveValue(50)
    expect(within(region('Rendering')).getByText('Unsaved')).toBeInTheDocument()
    put.mockRestore()
  })

  it('keeps that edit when another section saves while the first is in flight (#767)', async () => {
    // Rendering's save is held; Preview's goes straight through. Preview's request must
    // not erase the edit Rendering's answer has to leave alone.
    const save = api.putSettings.bind(api)
    let release: () => void = () => undefined
    const held = new Promise<void>((resolve) => (release = resolve))
    const put = vi.spyOn(api, 'putSettings').mockImplementation(async (body) => {
      const saved = await save(body)
      if ('render_timeout' in body) await held
      return saved
    })
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' }))
    await waitFor(() => expect(put).toHaveBeenCalledTimes(1))
    await user.clear(timeout)
    await user.type(timeout, '50')
    await choose(user, screen.getByLabelText('Show dimensions in'), 'Inches (in)')
    await user.click(within(region('Preview')).getByRole('button', { name: 'Save Preview' }))
    await waitFor(() => expect(within(region('Preview')).queryByText('Unsaved')).toBeNull())

    await act(async () => release())
    await waitFor(() => expect(screen.getByTestId('source-render_timeout')).toHaveTextContent('Set here'))
    expect(timeout).toHaveValue(50)
    put.mockRestore()
  })

  it('discards one section’s edits', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()

    const ttl = screen.getByLabelText('Refresh the font catalogue after')
    await user.clear(ttl)
    await user.type(ttl, '10')
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')

    await user.click(within(region('Fonts')).getByRole('button', { name: 'Discard Fonts changes' }))
    expect(ttl).toHaveValue(86400)
    expect(within(region('Fonts')).queryByText('Unsaved')).toBeNull()
    expect(within(region('Fonts')).getByRole('button', { name: 'Save Fonts' })).toBeDisabled()
    expect(timeout).toHaveValue(45)
    expect(put).not.toHaveBeenCalled()
    put.mockRestore()
  })

  it('shows a refused value beside its field', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    // Queried inside the section, not across the whole page, which is slow (#906).
    const rendering = within(region('Rendering'))
    const field = rendering.getByLabelText('Editor checks at once')
    await user.clear(field)
    await user.type(field, '0')
    await user.click(rendering.getByRole('button', { name: 'Save Rendering' }))
    expect(await rendering.findByRole('alert', {}, { timeout: 5000 })).toHaveTextContent(
      'SCADBUDDY_CHECK_CONCURRENCY must be at least 1',
    )
    expect(rendering.getByText('Unsaved')).toBeInTheDocument()
    // The alert's own wait is 5 s, so the test needs more than vitest's default 5 s (#906).
  }, 15_000)

  it('edits the upload limit in MB or GB and stores bytes', async () => {
    // The default is 1 GiB, shown as exactly that.
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const limit = screen.getByLabelText('Largest media upload')
    expect(limit).toHaveValue(1)
    expect(screen.getByLabelText('Unit for setting-media_upload_max_bytes')).toHaveTextContent('GiB')

    // Picking a unit converts the number; typing one reinterprets it in that unit.
    await choose(user, screen.getByLabelText('Unit for setting-media_upload_max_bytes'), 'MB')
    expect(limit).toHaveValue(1073.742)
    await user.clear(limit)
    await user.type(limit, '500')
    await user.click(within(region('Uploads')).getByRole('button', { name: 'Save Uploads' }))
    await waitFor(() => expect(put).toHaveBeenCalled())
    expect(put.mock.calls[0]?.[0]).toEqual({ media_upload_max_bytes: 500_000_000 })
    put.mockRestore()
  })

  it('holds an in-app link while anything is unsaved', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route
            path="/settings"
            element={
              <>
                <Link to="/">Catalogue</Link>
                <SettingsPage />
              </>
            }
          />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </MemoryRouter>,
    )
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')

    await user.click(screen.getByRole('link', { name: 'Catalogue' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(dialog).toHaveTextContent('Rendering')
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(screen.queryByText('The catalogue')).toBeNull()
    expect(timeout).toHaveValue(45)

    await user.click(screen.getByRole('link', { name: 'Catalogue' }))
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('The catalogue')).toBeInTheDocument()
  })

  it('holds a programmatic navigation while anything is unsaved', async () => {
    function GoHome() {
      const navigate = useNavigate()
      return (
        <button type="button" onClick={() => void navigate('/')}>
          Go home
        </button>
      )
    }
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route
            path="/settings"
            element={
              <>
                <GoHome />
                <SettingsPage />
              </>
            }
          />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </MemoryRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    await user.click(screen.getByRole('button', { name: 'Go home' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(screen.queryByText('The catalogue')).toBeNull()
    await user.click(within(dialog).getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('The catalogue')).toBeInTheDocument()
  })

  it('holds the browser Back button while anything is unsaved', async () => {
    window.history.replaceState(null, '', '/')
    window.history.pushState(null, '', '/settings')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </BrowserRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    act(() => window.history.back())
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(window.location.pathname).toBe('/settings')
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(screen.getByLabelText('Render timeout')).toHaveValue(45)

    // Held again after staying, and this time let through.
    act(() => window.history.back())
    await user.click(await screen.findByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('The catalogue')).toBeInTheDocument()
    expect(window.location.pathname).toBe('/')
  })

  it('holds a second Back pressed before the dialog is answered', async () => {
    window.history.replaceState(null, '', '/')
    window.history.pushState(null, '', '/elsewhere')
    window.history.pushState(null, '', '/settings')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/elsewhere" element={<p>Elsewhere</p>} />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </BrowserRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    act(() => window.history.back())
    await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    act(() => window.history.back())
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(window.location.pathname).toBe('/settings')
    expect(screen.getByLabelText('Render timeout')).toHaveValue(45)

    await user.click(screen.getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })
})

describe('SettingsPage secrets (#322)', () => {
  it('typing a key after Remove key replaces it instead of clearing it', async () => {
    const put = vi.spyOn(api, 'putSettings')
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const key = screen.getByLabelText('API key')
    const remove = screen.getByRole('button', { name: 'Remove key' })
    await user.click(remove)
    expect(key).toHaveAttribute('placeholder', 'Cleared when you save.')
    expect(remove).toBeDisabled()

    await user.type(key, 'n')
    expect(remove).toBeEnabled()
    // Emptied again, the field is back to leaving the stored key alone.
    await user.clear(key)
    expect(key).toHaveAttribute('placeholder', 'A key is stored. Paste a new one to replace it.')
    expect(within(region('Connection')).getByRole('button', { name: 'Save Connection' })).toBeDisabled()
    expect(put).not.toHaveBeenCalled()
    put.mockRestore()
  })
})

describe('SettingsPage remembered choices (#322)', () => {
  it('forgets one remembered choice and leaves the rest', async () => {
    setMockRemembered({
      modelChoices: { 'name-keychain': { tier: 'fine' }, gear: { tier: 'draft' } },
      printerBedTypes: { '1': 'Textured PEI Plate' },
    })
    const { user } = renderPage(<SettingsPage />)
    const table = await screen.findByRole('table', { name: 'Remembered choices' })
    expect(within(table).getAllByRole('row')).toHaveLength(4)

    await user.click(within(table).getByRole('button', { name: 'Forget printer and spools for gear' }))
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(3))
    expect(within(table).queryByText('draft quality')).toBeNull()
    expect(within(table).getByText('fine quality')).toBeInTheDocument()
    expect(within(table).getByText('Textured PEI Plate')).toBeInTheDocument()

    await user.click(within(table).getByRole('button', { name: 'Forget printer and spools for name-keychain' }))
    await waitFor(() => expect(within(table).getAllByRole('row')).toHaveLength(2))
    expect((await api.getRemembered()).model_print_choices).toEqual({})
    expect((await api.getRemembered()).printer_bed_types).toEqual({ '1': 'Textured PEI Plate' })
  })

  it('shows each project\'s printer and nozzle and forgets one (#599)', async () => {
    setMockRemembered({
      projectTargets: { '1': { printer_id: 1, nozzle_diameter: '0.4' }, '99': { printer_id: 42 } },
    })
    const { user } = renderPage(<SettingsPage />)
    const table = await screen.findByRole('table', { name: 'Remembered choices' })
    const projectRow = within(table).getByText('Reagan Keychain').closest('tr') as HTMLElement
    expect(within(projectRow).getByText(/0\.4 mm/)).toBeInTheDocument()
    const unknownRow = within(table).getByText('Project 99').closest('tr') as HTMLElement
    expect(within(unknownRow).getByText('Printer 42')).toBeInTheDocument()

    await user.click(within(table).getByRole('button', { name: 'Forget project printer and nozzle for Reagan Keychain' }))
    await waitFor(() => expect(within(table).queryByText('Reagan Keychain')).toBeNull())
    expect(within(table).getByText('Project 99')).toBeInTheDocument()
    expect(Object.keys((await api.getRemembered()).project_print_targets ?? {})).toEqual(['99'])
  })

  it('forgets everything after a confirmation', async () => {
    setMockRemembered({ printerBedTypes: { '1': 'Textured PEI Plate' }, modelChoices: { gear: { tier: 'draft' } } })
    const { user } = renderPage(<SettingsPage />)
    await screen.findByRole('table', { name: 'Remembered choices' })
    await user.click(screen.getByRole('button', { name: 'Forget all' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Yes, forget all' }))
    expect(await screen.findByText('Nothing is remembered yet.')).toBeInTheDocument()
  })
})

describe('SettingsPage connection and About (#322)', () => {
  it('lists every scope, the write scopes as not checked', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.click(screen.getByRole('button', { name: 'Test connection' }))
    const scopes = await screen.findByRole('list', { name: 'Scopes' })
    expect(within(scopes).getAllByRole('listitem')).toHaveLength(6)
    expect(scopes).toHaveTextContent('Read Status: granted.')
    expect(scopes).toHaveTextContent('Manage Queue: not checked.')
    expect(scopes).toHaveTextContent('Manage Archives (optional): not checked.')
    expect(scopes).toHaveTextContent('Manage Inventory (optional): not checked.')
  })

  it('says Bambuddy takes no finish photo, with a link to turn it on', async () => {
    renderPage(<SettingsPage />)
    const status = await screen.findByTestId('bambuddy-status')
    await waitFor(() => expect(status).toHaveTextContent('Finish photo in Bambuddy: off'))
    expect(within(status).getByRole('link', { name: 'Turn it on in Bambuddy' })).toHaveAttribute(
      'href',
      'https://bambuddy.internal.nullreference.io/settings',
    )
  })

  it('shows the build and the values only the deployment sets', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const about = screen.getByTestId('about')
    expect(about).toHaveTextContent('v0.42.0 (abc1234)')
    expect(about).toHaveTextContent('OpenSCAD version 2026.09.28')
    await waitFor(() => expect(about).toHaveTextContent('1.2.5.6'))
    const table = screen.getByRole('table', { name: 'Deployment values' })
    expect(table).toHaveTextContent('SCADBUDDY_DATABASE_URL')
    expect(table).toHaveTextContent('postgres.scadbuddy.svc:5432/scadbuddy')
    expect(table).toHaveTextContent('SCADBUDDY_OPENSCAD')
    // Read-only: nothing here is an input.
    expect(within(table).queryByRole('textbox')).toBeNull()
  })
})

// #426 — the render key, the blob store choice and the blob store's usage, in #322's
// sections: the key beside the API key in Connection, the store beside the inbox folder it
// needs in Projects & files.
describe('SettingsPage blob store (#426)', () => {
  const stored = {
    ...mockSettings(),
    bambuddy_url: 'https://bambuddy.internal.nullreference.io',
    has_api_key: true,
    library_folder_id: 2,
    store_backend: 'local' as const,
  }

  function serve(settings: Settings) {
    const bodies: Record<string, unknown>[] = []
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json(settings)),
      http.put('/api/v1/settings', async ({ request }) => {
        const body = (await request.json()) as Record<string, unknown>
        bodies.push(body)
        const { bambuddy_render_api_key: key, ...rest } = body
        return HttpResponse.json({
          ...settings,
          ...rest,
          ...(typeof key === 'string' ? { has_render_api_key: key !== '', render_key_fallback: key === '' } : {}),
        })
      }),
    )
    return bodies
  }

  it('warns while render workers would hold the full key', async () => {
    serve({ ...stored, has_render_api_key: false, render_key_fallback: true })
    renderPage(<SettingsPage />)
    await seeded()
    const warning = screen.getByTestId('render-key-fallback')
    expect(warning).toHaveAttribute('role', 'status')
    expect(warning).toHaveTextContent(
      /^Render workers hold the full Bambuddy key; template code can print\. Create a key with only Manage Library in Bambuddy and paste it above as the render key\.$/,
    )
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Paste the key')
  })

  it('drops the warning once a render key is stored, and never shows the key', async () => {
    serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    renderPage(<SettingsPage />)
    await seeded()
    expect(screen.queryByTestId('render-key-fallback')).toBeNull()
    const key = screen.getByLabelText('Render key')
    expect(key).toHaveValue('')
    expect(key).toHaveAttribute('type', 'password')
    expect(key).toHaveAttribute('placeholder', expect.stringContaining('A key is stored'))
  })

  it('sends a typed render key with Connection and leaves the stored API key alone', async () => {
    const bodies = serve({ ...stored, has_render_api_key: false, render_key_fallback: true })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await enter(user, screen.getByLabelText('Render key'), 'narrow')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_render_api_key: 'narrow' })
    await waitFor(() => expect(screen.queryByTestId('render-key-fallback')).toBeNull())
    expect(screen.getByLabelText('Render key')).toHaveValue('')
  })

  it('seeds the Blob store choice and sends a change with Projects & files', async () => {
    const bodies = serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const store = screen.getByLabelText('Blob store')
    expect(store).toHaveTextContent('This server’s volume')
    expect(await optionDisabled(user, store, /Bambuddy library/)).toBe(false)
    await choose(user, store, /Bambuddy library/)
    expect(within(region('Projects & files')).getByText('Unsaved')).toBeInTheDocument()
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ store_backend: 'bambuddy' })
  })

  it('offers ScadBuddy\u2019s own library only over a content store', async () => {
    serve({ ...stored, store_backend: 'local' })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const library = screen.getByLabelText('Print library')
    expect(library).toHaveTextContent('Bambuddy')
    expect(await optionDisabled(user, library, /ScadBuddy.s own/)).toBe(true)
  })

  it('saves slicer_backend, which applies at once (plan 2026-10-16 Task 14)', async () => {
    const bodies = serve({ ...stored, applies: { ...stored.applies, slicer_backend: 'live' } })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const slicer = screen.getByLabelText('Slicer')
    expect(slicer).toHaveTextContent('Bambuddy\u2019s slicer')
    await choose(user, slicer, /ScadBuddy.s slicer \(Bambu Studio CLI\)/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ slicer_backend: 'native' })
    expect(screen.getByLabelText('Slicer')).toHaveTextContent('ScadBuddy\u2019s slicer (Bambu Studio CLI)')
    expect(within(region('Projects & files')).getByTestId('source-slicer_backend')).not.toHaveTextContent(
      'Applies on restart',
    )
  })

  it('saves library_backend and shows it applies on restart', async () => {
    const bodies = serve({
      ...stored,
      store_backend: 'bambuddy',
      has_render_api_key: true,
      render_key_fallback: false,
    })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    const library = screen.getByLabelText('Print library')
    expect(await optionDisabled(user, library, /ScadBuddy.s own/)).toBe(false)
    await choose(user, library, /ScadBuddy.s own/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(within(region('Projects & files')).getByTestId('source-library_backend')).toHaveTextContent(
      'Applies on restart',
    )
    expect(bodies[0]).toEqual({ library_backend: 'native' })
    expect(screen.getByLabelText('Print library')).toHaveTextContent('ScadBuddy\u2019s own')
    // Only the API reads it at start: the workers ask the API on every call (#3827).
    expect(
      within(region('Projects & files')).getByText(/Takes effect when the ScadBuddy API restarts; its workers need no restart/),
    ).toBeInTheDocument()
  })

  it('does not fall back to local while the library is native', async () => {
    const bodies: Record<string, unknown>[] = []
    const stuck = { ...stored, store_backend: 'bambuddy' as const, library_backend: 'native' as const }
    server.use(
      http.get('/api/v1/settings', () => HttpResponse.json(stuck)),
      http.put('/api/v1/settings', async ({ request }) => {
        bodies.push((await request.json()) as Record<string, unknown>)
        return HttpResponse.json(
          {
            type: 'about:blank',
            title: 'Unprocessable Content',
            status: 422,
            detail:
              'ScadBuddy\'s own library keeps its files in the blob store: set store_backend to s3 or bambuddy first',
          },
          { status: 422, headers: { 'Content-Type': 'application/problem+json' } },
        )
      }),
    )
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Print library')).toHaveTextContent('ScadBuddy\u2019s own'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_url: null })
    expect(await screen.findByText(/set store_backend to s3 or bambuddy/)).toBeVisible()
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('')
  })

  it('keeps a stored Bambuddy store backend on an unrelated save', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    const own = screen.getByLabelText(/ScadBuddy.s own URL/)
    await user.clear(own)
    await enter(user, own, 'https://scadbuddy.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).not.toHaveProperty('store_backend')
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library')
  })

  it('falls back to the local store when the Bambuddy store loses its inbox', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const store = screen.getByLabelText('Blob store')
    await waitFor(() => expect(store).toHaveTextContent('Bambuddy library'))
    await choose(user, screen.getByLabelText(/Inbox folder/), 'Library root')
    expect(await optionDisabled(user, store, /Bambuddy library/)).toBe(true)
    expect(store).toHaveTextContent('This server’s volume')
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ library_folder_id: null, store_backend: 'local' })
  })

  it('falls back to the local store when a Connection save clears the Bambuddy URL', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    // The choice follows the saved URL, so it moves once Connection is saved.
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_url: null, store_backend: 'local' })
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('This server’s volume'))
  })

  it('falls back to the local store when the Bambuddy URL is reset while on the Bambuddy store', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.click(within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'], store_backend: 'local' })
  })

  it('does not fall back to local on a reset while the library is native', async () => {
    const bodies = serve({
      ...stored,
      store_backend: 'bambuddy',
      library_backend: 'native',
      has_render_api_key: true,
      render_key_fallback: false,
    })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Print library')).toHaveTextContent('ScadBuddy’s own'))
    await user.click(within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'] })
  })

  it('resets only the Bambuddy URL while on the local store', async () => {
    const bodies = serve(stored)
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await user.click(within(screen.getByTestId('source-bambuddy_url')).getByRole('button', { name: 'Reset to deployment value: Bambuddy URL' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ reset: ['bambuddy_url'] })
  })

  it('keeps the Bambuddy store on a Connection save while an unsaved Projects edit drops the inbox', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await choose(user, screen.getByLabelText(/Inbox folder/), 'Library root')
    const own = screen.getByLabelText('ScadBuddy’s own URL')
    await user.clear(own)
    await enter(user, own, 'https://mine.test')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ public_url: 'https://mine.test' })
  })

  it('keeps the Bambuddy store on a Projects save while an unsaved Connection edit clears the URL', async () => {
    const bodies = serve({ ...stored, store_backend: 'bambuddy', has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await waitFor(() => expect(screen.getByLabelText('Blob store')).toHaveTextContent('Bambuddy library'))
    await user.clear(screen.getByLabelText('Bambuddy URL'))
    await choose(user, screen.getByLabelText(/Inbox folder/), 'Keychains')
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ library_folder_id: 3 })
  })

  it('removes a stored render key when Connection is saved', async () => {
    const bodies = serve({ ...stored, has_render_api_key: true, render_key_fallback: false })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const row = screen.getByLabelText('Render key').closest('div') as HTMLElement
    await user.click(within(row).getByRole('button', { name: 'Remove key' }))
    expect(screen.getByLabelText('Render key')).toHaveAttribute('placeholder', 'Cleared when you save.')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    expect(bodies[0]).toEqual({ bambuddy_render_api_key: '' })
  })

  it('offers the Bambuddy store only once its URL is saved, then marks choosing it unsaved', async () => {
    serve({ ...stored, bambuddy_url: null })
    const { user } = renderPage(<SettingsPage />)
    // Saved: an inbox folder, no Bambuddy URL.
    const url = await screen.findByLabelText('Bambuddy URL')
    await waitFor(() => expect(screen.getByTestId('source-store_backend')).toBeInTheDocument())
    await enter(user, url, 'https://bambuddy.new.test')
    expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /Bambuddy library/)).toBe(true)
    expect(screen.getByTestId('store-backend-hint')).toHaveTextContent('Bambuddy URL is not saved yet')
    await user.click(within(region('Connection')).getByRole('button', { name: 'Save Connection' }))
    await waitFor(async () =>
      expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /Bambuddy library/)).toBe(false),
    )
    expect(screen.queryByTestId('store-backend-hint')).toBeNull()
    await choose(user, screen.getByLabelText('Blob store'), /Bambuddy library/)
    expect(within(region('Projects & files')).getByText('Unsaved')).toBeInTheDocument()
    expect(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' })).toBeEnabled()
  })

  it('offers the Bambuddy store only once an inbox folder is chosen', async () => {
    serve({ ...stored, library_folder_id: null })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /Bambuddy library/)).toBe(true)
  })

  it('shows what the blob store holds, where, against its caps', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({
          backend: 'bambuddy',
          count: 12,
          bytes: 3_450_000,
          max_count: 10_000,
          max_total_bytes: 1_000_000_000,
          by_kind: { piece: 1024, asset: 1024 },
        }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('Bambuddy library')
    expect(within(usage).getByText('Files').nextElementSibling).toHaveTextContent(/^12 of 10000$/)
    expect(usage).toHaveTextContent('3.5 MB of 1.0 GB')
    expect(
      screen.getByText(
        'The Where row is the store this process uses; it moves to the Blob store choice above at its next restart, so the two can differ until then.',
      ),
    ).toBeInTheDocument()
  })

  it('says a store cap of zero is no limit', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({ backend: 'local', by_kind: {}, count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(usage).toHaveTextContent('This server’s volume')
    expect(usage).toHaveTextContent('2 (no limit)')
    expect(usage).toHaveTextContent('640 B (no limit)')
  })
})

// The S3 store: its fields under the Blob store choice, the choice offered once they are saved.
describe('SettingsPage S3 store', () => {
  const s3 = {
    store_s3_endpoint: 'https://s3.example.test',
    store_s3_bucket: 'renders',
    store_s3_access_key_id: 'AKIA',
  }

  it('offers the S3 store once its endpoint, bucket and keys are saved', async () => {
    setMockSettings({ ...mockSettings(), ...s3 })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const store = screen.getByLabelText('Blob store')
    expect(await optionDisabled(user, store, /S3-compatible object store/)).toBe(true)
    expect(screen.getByTestId('store-s3-hint')).toHaveTextContent(
      'The S3 store needs its endpoint, bucket and keys saved first.',
    )
    const secret = screen.getByLabelText('Secret access key')
    expect(secret).toHaveAttribute('type', 'password')
    expect(secret).toHaveAttribute('placeholder', 'Paste the key')
    await enter(user, secret, 'shh')
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(async () =>
      expect(await optionDisabled(user, screen.getByLabelText('Blob store'), /S3-compatible object store/)).toBe(false),
    )
    expect(mockSettings().has_store_s3_secret_access_key).toBe(true)
    expect(screen.queryByTestId('store-s3-hint')).toBeNull()
    expect(screen.getByLabelText('Secret access key')).toHaveValue('')
    expect(screen.getByLabelText('Secret access key')).toHaveAttribute(
      'placeholder',
      expect.stringContaining('A key is stored'),
    )
    await choose(user, screen.getByLabelText('Blob store'), /S3-compatible object store/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(mockSettings().store_backend).toBe('s3'))
    expect(screen.getByLabelText('Blob store')).toHaveTextContent('S3-compatible object store')
  })

  it('sends the S3 fields and the path-style switch with Projects & files', async () => {
    const bodies: Record<string, unknown>[] = []
    server.events.on('request:start', async ({ request }) => {
      if (request.method === 'PUT' && new URL(request.url).pathname === '/api/v1/settings') {
        bodies.push((await request.clone().json()) as Record<string, unknown>)
      }
    })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    expect(screen.getByLabelText('Region')).toHaveValue('us-east-1')
    expect(screen.getByLabelText('Prefix')).toHaveValue('scadbuddy')
    expect(screen.getByLabelText('Path-style addressing')).toBeChecked()
    await enter(user, screen.getByLabelText('Endpoint'), 'https://s3.example.test')
    await enter(user, screen.getByLabelText('Bucket'), 'renders')
    await user.click(screen.getByLabelText('Path-style addressing'))
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    await waitFor(() => expect(bodies).toHaveLength(1))
    server.events.removeAllListeners()
    expect(bodies[0]).toEqual({
      store_s3_endpoint: 'https://s3.example.test',
      store_s3_bucket: 'renders',
      store_s3_path_style: false,
    })
  })

  it('refuses to switch to S3 without its fields', async () => {
    // Ready as saved, but the same save removes the secret: the server refuses the switch.
    setMockSettings({ ...mockSettings(), ...s3, has_store_s3_secret_access_key: true })
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    await choose(user, screen.getByLabelText('Blob store'), /S3-compatible object store/)
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Remove key' }))
    await user.click(within(region('Projects & files')).getByRole('button', { name: 'Save Projects & files' }))
    expect(
      await within(region('Projects & files')).findByText(
        'the S3 store needs its endpoint, bucket, access key id and secret access key saved first',
      ),
    ).toBeInTheDocument()
    expect(mockSettings().has_store_s3_secret_access_key).toBe(true)
    expect(mockSettings().store_backend).toBe('local')
  })

  it('says where an S3 store is', async () => {
    server.use(
      http.get('/api/v1/store/usage', () =>
        HttpResponse.json({ backend: 's3', by_kind: {}, count: 2, bytes: 640, max_count: 0, max_total_bytes: 0 }),
      ),
    )
    renderPage(<SettingsPage />)
    const usage = await screen.findByTestId('store-usage')
    expect(within(usage).getByText('Where').nextElementSibling).toHaveTextContent(/^S3 object store$/)
  })
})

describe('SettingsPage Orders (#4249)', () => {
  it('shows the orders currency, USD by default, in its own section', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const orders = region('Orders')
    expect(within(orders).getByLabelText('Orders currency')).toHaveValue('USD')
    // Applies to the next order, never a restart: no restart badge.
    expect(within(orders).queryByText(/restart/i)).toBeNull()
  })

  it('saves a three-letter code and shows the server refusing a lowercase one', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const field = () => within(region('Orders')).getByLabelText('Orders currency')
    await user.clear(field())
    await enter(user, field(), 'usd')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await within(region('Orders')).findByRole('alert')
    expect(mockSettings().orders_currency).toBe('USD')

    await user.clear(field())
    await enter(user, field(), 'EUR')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().orders_currency).toBe('EUR'))
    expect(mockSettings().sources?.['orders_currency']).toBe('stored')
  })
})

describe('SettingsPage Orders, the price book (#4250)', () => {
  it('shows the pricing policy and the filaments in the Orders section', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const orders = region('Orders')
    expect(within(orders).getByLabelText('Machine rate per hour')).toHaveValue('0')
    expect(within(orders).getByLabelText('Setup fee per plate')).toHaveValue('0')
    expect(within(orders).getByLabelText('Labour per item')).toHaveValue('0')
    expect(within(orders).getByLabelText('Markup, percent')).toHaveValue('0')
    expect(within(orders).getByRole('combobox', { name: 'Round the unit price' })).toHaveTextContent('Each line to the cent')
    const filaments = await within(orders).findByRole('table', { name: 'Filaments' })
    expect(await within(filaments).findByText(/Sunflower Yellow/)).toBeInTheDocument()
  })

  it('saves the policy, and shows the server refusing a negative rate', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const field = () => within(region('Orders')).getByLabelText('Markup, percent')
    await user.clear(field())
    await enter(user, field(), '-1')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await within(region('Orders')).findByRole('alert')
    expect(mockSettings().pricing_markup_percent).toBe('0')

    await user.clear(field())
    await enter(user, field(), '15')
    await choose(user, within(region('Orders')).getByRole('combobox', { name: 'Round the unit price' }), 'Up to a whole unit')
    await user.click(within(region('Orders')).getByRole('button', { name: 'Save Orders' }))
    await waitFor(() => expect(mockSettings().pricing_markup_percent).toBe('15'))
    expect(mockSettings().pricing_rounding).toBe('to_major')
    expect((await api.priceBook()).policy.markup_percent).toBe('15')
  })
})

describe('SettingsPage Administration (#668)', () => {
  it('shows no Temporal UI link while its URL is empty', async () => {
    renderPage(<SettingsPage />)
    await seeded()
    const administration = region('Administration')
    expect(within(administration).getByLabelText('Temporal UI URL')).toHaveValue('')
    expect(within(administration).queryByRole('link', { name: /Temporal UI/ })).toBeNull()
  })

  it('links to the Temporal UI in a new tab once its URL is saved', async () => {
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const administration = region('Administration')
    await enter(user, within(administration).getByLabelText('Temporal UI URL'), 'https://temporal.lan')
    await user.click(within(region('Administration')).getByRole('button', { name: 'Save Administration' }))
    const link = await within(administration).findByRole('link', { name: /Temporal UI/ })
    expect(link).toHaveAttribute('href', 'https://temporal.lan')
    // A page that is not ScadBuddy's: a new tab, which also escapes Bambuddy's sandbox.
    expect(link).toHaveAttribute('target', '_blank')
    expect(link.getAttribute('rel')).toContain('noopener')
    expect(mockSettings().temporal_ui_url).toBe('https://temporal.lan')
    expect(mockSettings().sources?.['temporal_ui_url']).toBe('stored')
  })

  it('links to a Temporal UI the deployment set', async () => {
    setMockSettings({
      ...mockSettings(),
      temporal_ui_url: 'https://temporal.env',
      sources: { ...mockSettings().sources, temporal_ui_url: 'env' },
    })
    renderPage(<SettingsPage />)
    await seeded()
    const link = await within(region('Administration')).findByRole('link', { name: /Temporal UI/ })
    expect(link).toHaveAttribute('href', 'https://temporal.env')
    expect(screen.getByTestId('source-temporal_ui_url')).toHaveTextContent('From SCADBUDDY_TEMPORAL_UI_URL')
  })
})

// #2173 — every section folds from its heading, remembered per browser.
describe('SettingsPage folded sections (#2173)', () => {
  function fold(name: string): HTMLElement {
    return within(region(name)).getByRole('button', { name })
  }

  it('folds a section from its heading, and the next visit keeps it folded', async () => {
    localStorage.clear()
    const { user, unmount } = renderPage(<SettingsPage />)
    await seeded()
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'true')
    await user.click(fold('Rendering'))
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByLabelText('Render timeout')).not.toBeVisible()
    unmount()

    renderPage(<SettingsPage />)
    await seeded()
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'false')
    expect(fold('Connection')).toHaveAttribute('aria-expanded', 'true')
    localStorage.clear()
  })

  it('keeps a folded section’s edits, its Unsaved badge and its Save bar', async () => {
    localStorage.clear()
    const { user } = renderPage(<SettingsPage />)
    await seeded()
    const timeout = screen.getByLabelText('Render timeout')
    await user.clear(timeout)
    await user.type(timeout, '45')
    await user.click(fold('Rendering'))

    expect(within(region('Rendering')).getByText('Unsaved')).toBeVisible()
    expect(within(region('Rendering')).getByRole('button', { name: 'Save Rendering' })).toBeEnabled()
    await user.click(fold('Rendering'))
    expect(screen.getByLabelText('Render timeout')).toHaveValue(45)
    localStorage.clear()
  })

  it('keeps every fold the page renders in the stored list (#3415 review)', async () => {
    // A Section or SubSection whose id is missing from FOLD_IDS would be pruned as stale.
    const first = renderPage(<SettingsPage />)
    await seeded()
    const ids = [...document.querySelectorAll('[data-collapsible]')].map((fold) => fold.id)
    expect(ids.length).toBeGreaterThan(0)
    first.unmount()
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(ids))
    try {
      renderPage(<SettingsPage />)
      await seeded()
      expect(JSON.parse(localStorage.getItem('scadbuddy.settings.collapsed') ?? '[]')).toEqual(ids)
    } finally {
      localStorage.clear()
    }
  })

  it('unfolds the section a link or the nav goes to', async () => {
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['libraries', 'rendering']))
    const { user } = renderPage(<SettingsPage />, { route: '/settings#libraries' })
    await seeded()
    await waitFor(() => expect(fold('Libraries')).toHaveAttribute('aria-expanded', 'true'))
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'false')

    const nav = screen.getByRole('navigation', { name: 'Settings sections' })
    await user.click(within(nav).getByRole('link', { name: 'Rendering' }))
    await waitFor(() => expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'true'))
    localStorage.clear()
  })
})
