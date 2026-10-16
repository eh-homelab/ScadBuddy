import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { lazyRetry } from '../lib/lazyRetry'
import { renderPage } from '../test/utils'
import { AppShell } from './AppShell'

vi.mock('../agent/chat/availability', () => ({
  useAiAvailability: () => ({ available: true, state: 'configured' }),
}))
// A chunk that failed to load: the lazy import rejects, as it does once the stale-chunk
// reload has given up.
vi.mock('./AgentLink', () => {
  throw new Error('Failed to fetch dynamically imported module: AgentLink-abc.js')
})
vi.mock('./assistant/AssistantPanel', () => {
  throw new Error('Failed to fetch dynamically imported module: AssistantPanel-abc.js')
})

describe('the shell’s own lazy chunks (#1002)', () => {
  it('keeps the page when the agent link or the assistant panel fails to load', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="*" element={<p>page</p>} />
        </Route>
      </Routes>,
    )
    await user.click(await screen.findByRole('button', { name: 'Assistant' }))

    const panel = screen.getByRole('complementary', { name: 'Assistant' })
    expect(await within(panel).findByRole('alert')).toHaveTextContent('The assistant failed to load')
    expect(within(panel).getByRole('button', { name: 'Reload page' })).toBeInTheDocument()
    expect(screen.getByText('page')).toBeInTheDocument()
    expect(screen.getByRole('navigation', { name: 'Main' })).toBeInTheDocument()
  })
})

function Broken(): never {
  throw new Error('page boom')
}

describe('the shell’s page boundary (#361, #1446)', () => {
  it('keeps the header and nav when a page throws, and navigating clears the fallback', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="/" element={<Broken />} />
          <Route path="/prints" element={<p>prints page</p>} />
        </Route>
      </Routes>,
    )
    const main = screen.getByRole('main')
    expect(within(main).getByRole('alert')).toHaveTextContent('This page stopped working')
    expect(within(main).getByRole('button', { name: 'Reload page' })).toBeInTheDocument()
    const nav = screen.getByRole('navigation', { name: 'Main' })

    await user.click(within(nav).getByRole('link', { name: 'Prints' }))
    expect(await within(main).findByText('prints page')).toBeInTheDocument()
    expect(within(main).queryByRole('alert')).not.toBeInTheDocument()
  })
})

describe('the shell’s page boundary retries a page chunk (#3241, #3754)', () => {
  it('shows a page whose chunk failed once when it is visited again', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const load = vi
      .fn<() => Promise<{ default: () => React.JSX.Element }>>()
      .mockRejectedValueOnce(new TypeError('Failed to fetch dynamically imported module: Prints-abc.js'))
      .mockResolvedValue({ default: () => <p>prints page</p> })
    const Prints = lazyRetry(load)
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="/prints" element={<Prints />} />
          <Route path="/library" element={<p>library page</p>} />
        </Route>
      </Routes>,
      { route: '/prints' },
    )
    const main = screen.getByRole('main')
    expect(await within(main).findByRole('alert')).toHaveTextContent('This page stopped working')
    const nav = screen.getByRole('navigation', { name: 'Main' })

    await user.click(within(nav).getByRole('link', { name: 'Library' }))
    expect(await within(main).findByText('library page')).toBeInTheDocument()
    await user.click(within(nav).getByRole('link', { name: 'Prints' }))

    // The page boundary's onError dropped the rejected import, so this visit loads it.
    expect(await within(main).findByText('prints page')).toBeInTheDocument()
    expect(load).toHaveBeenCalledTimes(2)
  })
})

describe('the shell’s orientation (#2454)', () => {
  it('names the tab after the page’s heading, and follows navigation', async () => {
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="/" element={<h1>Models</h1>} />
          <Route path="/prints" element={<h1>Print history</h1>} />
        </Route>
      </Routes>,
    )
    await waitFor(() => expect(document.title).toBe('Models · ScadBuddy'))
    const nav = screen.getByRole('navigation', { name: 'Main' })
    await user.click(within(nav).getByRole('link', { name: 'Prints' }))
    await waitFor(() => expect(document.title).toBe('Print history · ScadBuddy'))
  })

  it('offers a skip link first, to the page, and names the logo link by its text', async () => {
    const { user } = renderPage(
      <Routes>
        <Route element={<AppShell embedded={false} tabLink={null} />}>
          <Route path="*" element={<h1>Models</h1>} />
        </Route>
      </Routes>,
    )
    // #3393: a click on a bare spot of the page does not focus <main>.
    await user.click(screen.getByRole('heading', { name: 'Models' }))
    expect(screen.getByRole('main')).not.toHaveFocus()
    await user.tab()
    const skip = screen.getByRole('link', { name: 'Skip to main content' })
    expect(skip).toHaveFocus()
    await user.click(skip)
    expect(screen.getByRole('main')).toHaveFocus()
    // Activated again while <main> has focus, it does not stack another blur listener.
    const added = vi.spyOn(screen.getByRole('main'), 'addEventListener')
    fireEvent.click(skip)
    expect(added).not.toHaveBeenCalled()
    added.mockRestore()
    // Focusable only while the skip link has put focus there.
    await user.tab()
    expect(screen.getByRole('main')).not.toHaveFocus()
    expect(screen.getByRole('main')).not.toHaveAttribute('tabindex')
    // Its visible text, not a shorter label (axe label-content-name-mismatch).
    expect(screen.getByRole('link', { name: /^ScadBuddy/ })).toHaveAccessibleName('ScadBuddy OpenSCAD customizer')
  })
})

describe('the shell’s sidebar (#3653)', () => {
  const shell = (embedded: boolean) => (
    <Routes>
      <Route element={<AppShell embedded={embedded} tabLink={null} />}>
        <Route path="*" element={<h1>Page</h1>} />
      </Route>
    </Routes>
  )
  const sidebarState = () => document.querySelector('[data-slot="sidebar"]')?.getAttribute('data-state')
  const forget = () => {
    window.localStorage.removeItem('scadbuddy.sidebar.open')
    window.localStorage.removeItem('scadbuddy.sidebar.open.embedded')
  }

  it('marks the page’s own link, and folds to its icons and back', async () => {
    forget()
    onTestFinished(forget)
    const { user } = renderPage(shell(false), { route: '/prints/7' })
    const nav = screen.getByRole('navigation', { name: 'Main' })
    expect(within(nav).getByRole('link', { name: 'Prints' })).toHaveAttribute('aria-current', 'page')
    expect(within(nav).getByRole('link', { name: 'Prints' })).toHaveAttribute('data-active', 'true')
    expect(within(nav).getByRole('link', { name: 'Models' })).not.toHaveAttribute('aria-current')

    const before = sidebarState()
    await user.click(screen.getByRole('button', { name: 'Toggle navigation' }))
    expect(sidebarState()).not.toBe(before)
    // Folded to icons, the links stay, by name.
    expect(within(nav).getByRole('link', { name: 'Settings' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Toggle navigation' }))
    expect(sidebarState()).toBe(before)
    // Remembered in this browser, not in a cookie that every request would carry.
    expect(window.localStorage.getItem('scadbuddy.sidebar.open')).toBe(String(before === 'expanded'))
    expect(document.cookie).not.toContain('sidebar_state')
  })

  it('leaves Ctrl+B to the browser and to whatever has the focus', async () => {
    forget()
    const { user } = renderPage(shell(false))
    const before = sidebarState()
    await user.keyboard('{Control>}b{/Control}')
    expect(sidebarState()).toBe(before)
  })

  it('starts as icons in Bambuddy’s frame, beside its own navigation, whatever the app outside it does', () => {
    forget()
    // Expanded outside the frame: the same origin's storage, but not the frame's choice.
    window.localStorage.setItem('scadbuddy.sidebar.open', 'true')
    onTestFinished(forget)
    renderPage(shell(true))
    expect(sidebarState()).toBe('collapsed')
    expect(screen.getByRole('banner')).toHaveAttribute('data-embedded', 'true')
  })

  it('starts as the user left it, in the frame or out of it', () => {
    window.localStorage.setItem('scadbuddy.sidebar.open.embedded', 'true')
    window.localStorage.setItem('scadbuddy.sidebar.open', 'false')
    onTestFinished(forget)
    const { unmount } = renderPage(shell(true))
    expect(sidebarState()).toBe('expanded')
    unmount()
    renderPage(shell(false))
    expect(sidebarState()).toBe('collapsed')
  })
})
