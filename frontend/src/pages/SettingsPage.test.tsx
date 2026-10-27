import { act, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { HttpResponse, http } from 'msw'
import { BrowserRouter, MemoryRouter, Link, Route, Routes, useLocation, useNavigate } from 'react-router'
import { describe, expect, it, vi } from 'vitest'
import { resetAiAvailability } from '../agent/chat/availability'
import { api } from '../api/client'
import { emitRealtime } from '../mocks/realtime'
import { server } from '../mocks/server'
import { renderPage } from '../test/utils'
import { SettingsPage } from './SettingsPage'
import { SECTION_REGISTRY } from './settings/registry'
import { SETTLE_MS } from './settings/sectionNav'
import { enter, region } from './settings/testing'

/** Bambuddy's "Test connection", as against a plugin endpoint's (RemotePluginsPanel). */
const bambuddyTest = () =>
  within(region('Connection')).getByRole('button', { name: 'Test connection' })

/** The form seeds itself from the server, so wait for the URL to arrive. */
async function seeded() {
  await waitFor(() =>
    expect(screen.getByLabelText('Bambuddy URL')).toHaveValue('https://bambuddy.internal.nullreference.io'),
  )
}

/** The render key's warning (#426) is a status too, so find the one that says `text`. */
function findStatus(text: string) {
  return screen.findByText(
    (_, element) => element?.getAttribute('role') === 'status' && (element.textContent ?? '').includes(text),
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

/** An element's accessible name, from what labels it (enough for these landmarks). */
function nameOf(element: Element): string {
  const labelledBy = element.getAttribute('aria-labelledby')
  if (labelledBy) {
    return labelledBy
      .split(/\s+/)
      .map((id) => document.getElementById(id)?.textContent?.trim() ?? '')
      .join(' ')
  }
  return element.getAttribute('aria-label') ?? ''
}

// #2171 — every area is rendered at once, so ids and landmark names must stay unique
// across all of Settings, not only within one area.
describe('SettingsPage, every area at once (#2171)', () => {
  it('gives every section an id, heading and body no other element on the page has', async () => {
    renderPage(<SettingsPage />, { route: '/settings/general' })
    await seeded()
    await waitFor(() => {
      for (const { id } of SECTION_REGISTRY) {
        expect(document.querySelectorAll(`[id="${id}"]`), id).toHaveLength(1)
        expect(document.querySelectorAll(`[id="${id}-heading"]`), `${id}-heading`).toHaveLength(1)
        expect(document.querySelectorAll(`[id="${id}-body"]`), `${id}-body`).toHaveLength(1)
      }
    })
  })

  it('names no two regions alike', async () => {
    renderPage(<SettingsPage />, { route: '/settings/general' })
    await seeded()
    // Each section's own heading, once the agent's sections have loaded too.
    await waitFor(() =>
      expect(document.querySelectorAll('section[aria-labelledby]').length).toBeGreaterThanOrEqual(
        SECTION_REGISTRY.length,
      ),
    )
    const names = [...document.querySelectorAll('section[aria-labelledby], [role="region"]')].map(nameOf)
    const repeated = names.filter((name, index) => names.indexOf(name) !== index)
    expect(repeated).toEqual([])
  })
})

describe('SettingsPage, live (#269)', () => {
  const OTHER = 'https://scadbuddy.elsewhere.test'

  it('follows settings saved elsewhere while the form is untouched', async () => {
    renderPage(<SettingsPage />, { route: '/settings/general' })
    const field = await screen.findByLabelText(/ScadBuddy.s own URL/)
    await api.putSettings({ public_url: OTHER })
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await waitFor(() => expect(field).toHaveValue(OTHER))
  })

  it('keeps an edited form and says the settings changed elsewhere', async () => {
    const { user } = renderPage(<SettingsPage />, { route: '/settings/general' })
    const field = await screen.findByLabelText(/ScadBuddy.s own URL/)
    // Seeded from the stored settings first; editing before that would be overwritten.
    await waitFor(() => expect(field).not.toHaveValue(''))
    await user.clear(field)
    await enter(user, field, 'https://mine.test')

    await api.putSettings({ public_url: OTHER })
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    expect(await screen.findByText(/Settings were changed elsewhere/)).toBeInTheDocument()
    expect(field).toHaveValue('https://mine.test')

    await user.click(screen.getByRole('button', { name: 'Load the latest' }))
    await waitFor(() => expect(field).toHaveValue(OTHER))
  })

  it("does not call this tab's own Test connection a change made elsewhere", async () => {
    const { user } = renderPage(<SettingsPage />, { route: '/settings/general' })
    const field = await screen.findByLabelText(/ScadBuddy.s own URL/)
    await waitFor(() => expect(field).not.toHaveValue(''))
    await user.clear(field)
    await enter(user, field, 'https://tested.test')
    await user.click(bambuddyTest())
    await findStatus('3DP-31B-598')

    // The test saved the form; this is that save's own event.
    emitRealtime('settings.changed', ['settings'], { section: 'connection' })
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(screen.queryByText(/Settings were changed elsewhere/)).not.toBeInTheDocument()
    expect(field).toHaveValue('https://tested.test')
  })

  it('shows the assistant plugin sections only while the agent says AI is available', async () => {
    // Availability is the agent's own answer at /api/v1/ai/status (useAiAvailability).
    server.use(
      http.get('/api/v1/ai/status', () =>
        HttpResponse.json({
          available: false,
          state: 'disabled',
          ai: 'disabled (no Claude credential)',
          reason: 'No Claude credential is configured yet.',
        }),
      ),
    )
    resetAiAvailability()
    const hidden = renderPage(<SettingsPage />, { route: '/settings/tools' })
    await seeded()
    await waitFor(() => expect(screen.getByTestId('ai-status')).toHaveAttribute('data-state', 'not_configured'))
    expect(screen.queryByRole('region', { name: 'Packages' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Plugin packages' })).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: 'Plugins' })).not.toBeInTheDocument()
    hidden.unmount()

    // The default handler answers as available.
    server.resetHandlers()
    resetAiAvailability()
    renderPage(<SettingsPage />, { route: '/settings/tools' })
    const packages = await screen.findByRole('region', { name: 'Packages' })
    expect(await within(packages).findByRole('heading', { name: 'Plugin packages' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Plugins' })).toBeInTheDocument()
    expect(await screen.findByRole('listitem', { name: 'Plugin endpoint hindsight' })).toBeInTheDocument()
  })

  it('says a failed load failed, and loads again on Try again (#1041)', async () => {
    let fail = true
    server.use(
      http.get('/api/v1/settings', () =>
        fail
          ? HttpResponse.json({ title: 'Internal Server Error', status: 500, detail: 'injected 500' }, { status: 500 })
          : undefined,
      ),
    )
    const { user } = renderPage(<SettingsPage />, { route: '/settings/general' })
    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('Could not load settings: injected 500')
    expect(screen.queryByText('Loading settings')).not.toBeInTheDocument()

    fail = false
    await user.click(within(alert).getByRole('button', { name: 'Try again' }))
    await seeded()
  })
})

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
    return <p data-testid="where">{`${location.pathname}${location.hash}`}</p>
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
      renderPage(<SettingsPage />, { route: '/settings/library' })
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
    window.history.pushState(null, '', '/settings/library')
    try {
      const user = userEvent.setup()
      render(
        <BrowserRouter>
          <Routes>
            <Route path="/settings/*" element={<SettingsPage />} />
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
      expect(window.location.pathname).toBe('/settings/library')
    } finally {
      restore()
    }
  })

  it('unfolds a deep-linked section folded on another area, and scrolls to it once laid out', async () => {
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['mcp-auth-mode']))
    const tops: Record<string, number> = {}
    const { scrolls, restore } = stubLayout(tops)
    const observers = stubResizeObserver()
    try {
      renderPage(
        <>
          <SettingsPage />
          <Where />
        </>,
        { route: '/settings#assistant-mcp' },
      )
      await seeded()
      // The old anchor of the one long page lands on its section in the MCP access area.
      await waitFor(() => expect(screen.getByTestId('where')).toHaveTextContent('/settings/mcp#mcp-auth-mode'))
      await waitFor(() =>
        expect(within(region('Auth mode')).getByRole('button', { name: 'Auth mode' })).toHaveAttribute(
          'aria-expanded',
          'true',
        ),
      )
      // Measured while it was folded, the section had no box; laid out, it has one, and
      // the section nav's resize follow-up scrolls the pane there.
      tops['mcp-auth-mode'] = 7000
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
        { route: '/settings/library' },
      )
      await seeded()
      const nav = screen.getByRole('navigation', { name: 'Settings sections' })
      const link = within(nav).getByRole('link', { name: 'Libraries' })
      await user.click(link)
      expect(screen.getByTestId('where')).toHaveTextContent('#libraries')
      await waitFor(() => expect(document.activeElement).toBe(document.getElementById('libraries-heading')))
      expect(scrolls).toContain(4992)
      expect(link).toHaveAttribute('aria-current', 'location')
      expect(within(nav).getByRole('link', { name: 'Projects & files' })).not.toHaveAttribute('aria-current')
    } finally {
      restore()
    }
  })
})

describe('SettingsPage sections (#322)', () => {
  it('holds an in-app link while anything is unsaved', async () => {
    const user = userEvent.setup()
    render(
      <MemoryRouter initialEntries={['/settings/library']}>
        <Routes>
          <Route
            path="/settings/*"
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
      <MemoryRouter initialEntries={['/settings/library']}>
        <Routes>
          <Route
            path="/settings/*"
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
    window.history.pushState(null, '', '/settings/library')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/settings/*" element={<SettingsPage />} />
          <Route path="/" element={<p>The catalogue</p>} />
        </Routes>
      </BrowserRouter>,
    )
    await seeded()
    await user.clear(screen.getByLabelText('Render timeout'))
    await user.type(screen.getByLabelText('Render timeout'), '45')

    act(() => window.history.back())
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(window.location.pathname).toBe('/settings/library')
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
    window.history.pushState(null, '', '/settings/library')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route path="/settings/*" element={<SettingsPage />} />
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
    expect(window.location.pathname).toBe('/settings/library')
    expect(screen.getByLabelText('Render timeout')).toHaveValue(45)

    await user.click(screen.getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })
})

// #2173 — every section folds from its heading, remembered per browser.
describe('SettingsPage folded sections (#2173)', () => {
  function fold(name: string): HTMLElement {
    return within(region(name)).getByRole('button', { name })
  }

  it('folds a section from its heading, and the next visit keeps it folded', async () => {
    localStorage.clear()
    const { user, unmount } = renderPage(<SettingsPage />, { route: '/settings/library' })
    await seeded()
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'true')
    await user.click(fold('Rendering'))
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByLabelText('Render timeout')).not.toBeVisible()
    unmount()

    renderPage(<SettingsPage />, { route: '/settings/library' })
    await seeded()
    expect(fold('Rendering')).toHaveAttribute('aria-expanded', 'false')
    expect(region('Connection').querySelector('h2 button')).toHaveAttribute('aria-expanded', 'true')
    localStorage.clear()
  })

  it('keeps a folded section’s edits, its Unsaved badge and its Save bar', async () => {
    localStorage.clear()
    const { user } = renderPage(<SettingsPage />, { route: '/settings/library' })
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
    // A Section whose id is missing from the registry's folds would be pruned as stale.
    const first = renderPage(<SettingsPage />, { route: '/settings/general' })
    await seeded()
    const ids = [...document.querySelectorAll('[data-collapsible]')].map((fold) => fold.id)
    expect(ids.length).toBeGreaterThan(0)
    first.unmount()
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(ids))
    try {
      renderPage(<SettingsPage />, { route: '/settings/general' })
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
