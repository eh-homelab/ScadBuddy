import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { BrowserRouter, Link, Route, Routes, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AiAvailability } from '../../agent/chat/availability'
import { emitRealtime } from '../../mocks/realtime'
import { Section } from './controls'
import { useDirtySections, useHoldLeave, useReportDirty } from './dirty'
import type { SettingsSection } from './registry'
import { SettingsProviders, SettingsShell } from './SettingsShell'
import { enter, renderSettingsShell } from './testing'

// #2171 — Settings by area: one address per area, each area mounted once opened so drafts
// survive a switch, and the leave guard asking only when the page itself is left.

const availability = vi.hoisted((): AiAvailability => ({ available: true, state: 'configured' }))
vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => availability }))

/** The current address, path and hash, as the router has it. */
function Where() {
  const { pathname, hash } = useLocation()
  return <span data-testid="where">{`${pathname}${hash}`}</span>
}

/** Each mounted fake section has a `Where`; an area mounts only once opened. */
const where = () => screen.getAllByTestId('where')[0]?.textContent

function Alpha() {
  const [value, setValue] = useState('')
  useReportDirty('alpha', value !== '')
  return (
    // Its heading is not "Alpha": the section is labelled by it, and the field is "Alpha".
    <Section id="alpha" title="First section">
      <label htmlFor="alpha-input">Alpha</label>
      <input id="alpha-input" value={value} onChange={(event) => setValue(event.target.value)} />
      <Where />
      <Link to="/">Leave</Link>
      <Link to="/settings#beta">To Beta</Link>
      <Link to="/settings#assistant-model">Old link</Link>
    </Section>
  )
}

function Beta() {
  const [value, setValue] = useState('')
  useReportDirty('beta', value !== '')
  return (
    <Section id="beta" title="Beta">
      <label htmlFor="beta-input">Beta</label>
      <input id="beta-input" value={value} onChange={(event) => setValue(event.target.value)} />
      <Where />
    </Section>
  )
}

function Packages() {
  return (
    <Section id="packages" title="Packages">
      <p>Plugin packages</p>
      <Where />
    </Section>
  )
}

function Ghost() {
  return (
    <Section id="ghost" title="Ghost">
      {null}
    </Section>
  )
}

function GhostMcp() {
  return (
    <Section id="ghost-mcp" title="Ghost MCP">
      {null}
    </Section>
  )
}

/** #7192 — unsaved work that is no settings form: it holds a leave, never a dirty section. */
function Holder() {
  const [held, setHeld] = useState(false)
  useHoldLeave('holder', held ? "shop-notes's skills/a.md" : null)
  const dirty = useDirtySections()
  return (
    <Section id="holder" title="Holder">
      <button type="button" onClick={() => setHeld(!held)}>
        {held ? 'Let go' : 'Hold'}
      </button>
      <span data-testid="dirty">{dirty.join(',')}</span>
      <Link to="/">Leave from holder</Link>
    </Section>
  )
}

const fakes: readonly SettingsSection[] = [
  { id: 'alpha', area: 'general', title: 'Alpha', order: 10, Component: Alpha },
  { id: 'beta', area: 'library', title: 'Beta', order: 10, Component: Beta },
  { id: 'packages', area: 'tools', title: 'Packages', order: 10, Component: Packages },
  { id: 'ghost', area: 'tools', title: 'Ghost', order: 20, Component: Ghost, hideWhenEmpty: true },
]

/** A section holding an element whose id is an old anchor's: a stale hash must not reach it. */
function Decoy() {
  return (
    <Section id="decoy" title="Decoy">
      <label htmlFor="assistant-model">Decoy field</label>
      <input id="assistant-model" />
    </Section>
  )
}

function Model() {
  return (
    <Section id="model" title="Model">
      <p>The model</p>
    </Section>
  )
}

const withDecoy: readonly SettingsSection[] = [
  ...fakes,
  { id: 'decoy', area: 'general', title: 'Decoy', order: 20, Component: Decoy },
  { id: 'model', area: 'assistant', title: 'Model', order: 10, Component: Model },
]

const areas = () => screen.getByRole('navigation', { name: 'Settings areas' })
const sectionLinks = () =>
  within(screen.getByRole('navigation', { name: 'Settings sections' }))
    .queryAllByRole('link')
    .map((link) => link.textContent)

describe('SettingsShell (#2171)', () => {
  afterEach(() => {
    Object.assign(availability, { available: true, state: 'configured' })
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('mounts an area when it is first opened, and keeps it mounted after', async () => {
    const user = userEvent.setup()
    renderSettingsShell(fakes, { route: '/settings/general' })
    await screen.findByLabelText('Alpha')
    // Not mounted, hidden or not: Tools & plugins and System are thousands of rows.
    expect(screen.queryByText('Plugin packages')).toBeNull()
    expect(screen.queryByRole('textbox', { name: 'Beta', hidden: true })).toBeNull()
    await user.click(within(areas()).getByRole('link', { name: 'Tools & plugins' }))
    expect(await screen.findByText('Plugin packages')).toBeVisible()
    await user.click(within(areas()).getByRole('link', { name: 'General' }))
    await waitFor(() => expect(where()).toBe('/settings/general'))
    expect(screen.getByText('Plugin packages')).not.toBeVisible()
  })

  it("mounts the agent's field areas at once, so set_field finds them", async () => {
    const withConnection: readonly SettingsSection[] = [
      ...fakes,
      { id: 'projects', area: 'library', title: 'Projects', order: 20, Component: Model },
    ]
    renderSettingsShell(withConnection, { route: '/settings/general' })
    await screen.findByLabelText('Alpha')
    expect(screen.getByText('The model')).not.toBeVisible()
  })

  it('redirects /settings to /settings/general', async () => {
    renderSettingsShell(fakes, { route: '/settings' })
    await waitFor(() => expect(where()).toBe('/settings/general'))
  })

  it('opens the area that holds a /settings#<section> link, unfolded', async () => {
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['beta']))
    renderSettingsShell(fakes, { route: '/settings#beta' })
    await waitFor(() => expect(where()).toBe('/settings/library#beta'))
    expect(screen.getByRole('button', { name: 'Beta' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('sends an old anchor to its new section', async () => {
    renderSettingsShell(fakes, { route: '/settings#assistant-plugins' })
    await waitFor(() => expect(where()).toBe('/settings/tools#packages'))
  })

  it('lands on General for a hash no area holds, and for a malformed escape', async () => {
    const first = renderSettingsShell(fakes, { route: '/settings#nope' })
    await waitFor(() => expect(where()).toBe('/settings/general#nope'))
    first.unmount()
    renderSettingsShell(fakes, { route: '/settings#%E0%A4%A' })
    await waitFor(() => expect(where()).toBe('/settings/general#%E0%A4%A'))
  })

  it("lists every area, and only the current area's sections", async () => {
    renderSettingsShell(fakes)
    await waitFor(() => expect(where()).toBe('/settings/general'))
    expect(within(areas()).getAllByRole('link').map((link) => link.textContent)).toEqual([
      'General',
      'Printing',
      'Library',
      'Assistant',
      'Tools & plugins',
      'MCP access',
      'Audits',
      'System',
    ])
    expect(within(areas()).getByRole('link', { name: 'General' })).toHaveAttribute('aria-current', 'page')
    expect(sectionLinks()).toEqual(['Alpha'])
  })

  it('keeps a draft across area switches, dots the area, and asks only when leaving Settings', async () => {
    const { user } = renderSettingsShell(fakes)
    await enter(user, await screen.findByLabelText('Alpha'), 'draft')
    await user.click(within(areas()).getByRole('link', { name: 'Library' }))
    expect(where()).toBe('/settings/library')
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(sectionLinks()).toEqual(['Beta'])
    expect(within(areas()).getByRole('link', { name: /General/ })).toContainElement(screen.getByLabelText('unsaved'))
    await user.click(within(areas()).getByRole('link', { name: /General/ }))
    expect(screen.getByLabelText('Alpha')).toHaveValue('draft')
    await user.click(screen.getByRole('link', { name: 'Leave' }))
    expect(await screen.findByRole('alertdialog', { name: 'Leave without saving?' })).toHaveTextContent('Alpha')
  })

  it('asks before leaving while a hold is set, naming it, though no section is dirty (#7192)', async () => {
    const { user } = renderSettingsShell([...fakes, { id: 'holder', area: 'general', title: 'Holder', order: 30, Component: Holder }])
    await user.click(await screen.findByRole('button', { name: 'Hold' }))
    // Not a dirty section: the reload, the area dots and the agent's flag read those only.
    expect(screen.getByTestId('dirty')).toHaveTextContent('')
    expect(within(areas()).getByRole('link', { name: 'General' })).not.toContainElement(screen.queryByLabelText('unsaved'))
    await user.click(screen.getByRole('link', { name: 'Leave from holder' }))
    const dialog = await screen.findByRole('alertdialog', { name: 'Leave without saving?' })
    expect(dialog).toHaveTextContent("Unsaved changes in shop-notes's skills/a.md will be lost. Save each section and file first")
    await user.click(within(dialog).getByRole('button', { name: 'Stay' }))
    expect(where()).toBe('/settings/general')
    // Let go: leaving no longer asks.
    await user.click(screen.getByRole('button', { name: 'Let go' }))
    await user.click(screen.getByRole('link', { name: 'Leave from holder' }))
    await waitFor(() => expect(screen.queryByTestId('where')).toBeNull())
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('names every section with a draft, in any area, in the changed-elsewhere notice', async () => {
    const { user } = renderSettingsShell(fakes)
    await enter(user, await screen.findByLabelText('Alpha'), 'draft')
    await user.click(within(areas()).getByRole('link', { name: 'Library' }))
    await enter(user, screen.getByRole('textbox', { name: 'Beta' }), 'draft')
    // The first event may land before the subscription is live.
    await waitFor(() => {
      emitRealtime('settings.changed', ['settings'], { section: 'connection' })
      expect(screen.getByText(/Settings were changed elsewhere/)).toHaveTextContent(
        'Settings were changed elsewhere. Your unsaved changes in Alpha, Beta are kept; Load the latest discards them.',
      )
    })
  })

  it('keeps drafts through a redirect', async () => {
    const { user } = renderSettingsShell(fakes)
    await enter(user, await screen.findByLabelText('Alpha'), 'draft')
    await user.click(screen.getByRole('link', { name: 'To Beta' }))
    await waitFor(() => expect(where()).toBe('/settings/library#beta'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    expect(screen.getByLabelText('Alpha')).toHaveValue('draft')
  })

  it('hides an empty hideWhenEmpty section and its sub-nav link', async () => {
    renderSettingsShell(fakes, { route: '/settings/tools' })
    await waitFor(() => expect(sectionLinks()).toEqual(['Packages']))
    expect(document.getElementById('ghost')).not.toBeVisible()
    expect(document.getElementById('packages')).toBeVisible()
  })

  it.each(['unavailable', 'unreachable'] as const)(
    'shows one notice on an agent-served area while the assistant is %s',
    async (state) => {
      Object.assign(availability, { available: false, state })
      const notice = 'The assistant service is not answering: the settings it serves are hidden until it does.'
      const { user } = renderSettingsShell(fakes, { route: '/settings/tools' })
      await waitFor(() => expect(screen.getAllByRole('status')).toHaveLength(1))
      expect(screen.getByRole('status')).toHaveTextContent(notice)
      await user.click(within(areas()).getByRole('link', { name: 'General' }))
      expect(screen.queryByRole('status')).toBeNull()
    },
  )

  it.each(['checking', 'not_configured'] as const)(
    'shows no notice on an agent-served area while the assistant is %s (#2171)',
    async (state) => {
      Object.assign(availability, { available: false, state })
      renderSettingsShell(fakes, { route: '/settings/tools' })
      await waitFor(() => expect(sectionLinks()).toEqual(['Packages']))
      expect(screen.queryByText(/The assistant service is not answering/)).toBeNull()
    },
  )

  it('says where to look when every section of an agent-served area hid itself while the assistant is not_configured', async () => {
    Object.assign(availability, { available: false, state: 'not_configured' })
    const withGhostMcp: readonly SettingsSection[] = [
      ...fakes,
      { id: 'ghost-mcp', area: 'mcp', title: 'Ghost MCP', order: 10, Component: GhostMcp, hideWhenEmpty: true },
    ]
    const first = renderSettingsShell(withGhostMcp, { route: '/settings/mcp' })
    const notice = await screen.findByRole('status')
    expect(notice).toHaveTextContent(
      'Nothing here until the assistant service is set up. Status & credentials says what it needs.',
    )
    expect(within(notice).getByRole('link', { name: 'Status & credentials' })).toHaveAttribute(
      'href',
      '/settings/assistant#assistant-credentials',
    )
    first.unmount()
    renderSettingsShell(fakes, { route: '/settings/tools' })
    await waitFor(() => expect(sectionLinks()).toEqual(['Packages']))
    expect(screen.queryByText(/Nothing here until/)).toBeNull()
  })

  it.each(['unavailable', 'unreachable'] as const)(
    'shows no "nothing here" notice on an emptied agent-served area while the assistant is %s',
    async (state) => {
      Object.assign(availability, { available: false, state })
      const withGhostMcp: readonly SettingsSection[] = [
        ...fakes,
        { id: 'ghost-mcp', area: 'mcp', title: 'Ghost MCP', order: 10, Component: GhostMcp, hideWhenEmpty: true },
      ]
      renderSettingsShell(withGhostMcp, { route: '/settings/mcp' })
      await screen.findByRole('status')
      expect(screen.queryByText(/Nothing here until/)).toBeNull()
    },
  )

  it('scrolls the pane to the top on an area switch', async () => {
    const { user, container } = renderSettingsShell(fakes)
    await screen.findByLabelText('Alpha')
    const pane = container.querySelector<HTMLElement>('.sb-settings')!
    const tops: number[] = []
    pane.scrollTo = ((options: ScrollToOptions) => tops.push(options.top ?? -1)) as typeof pane.scrollTo
    await user.click(within(areas()).getByRole('link', { name: 'Library' }))
    expect(tops).toEqual([0])
  })

  it('Back across areas while dirty asks nothing and keeps the draft', async () => {
    window.history.replaceState(null, '', '/settings/general')
    const user = userEvent.setup()
    render(
      <BrowserRouter>
        <Routes>
          <Route
            path="/settings/*"
            element={
              <SettingsProviders sections={fakes}>
                <SettingsShell sections={fakes} />
              </SettingsProviders>
            }
          />
        </Routes>
      </BrowserRouter>,
    )
    await screen.findByLabelText('Alpha')
    await user.click(within(areas()).getByRole('link', { name: 'Library' }))
    await enter(user, screen.getByRole('textbox', { name: 'Beta' }), 'draft')
    window.history.back()
    await waitFor(() => expect(where()).toBe('/settings/general'))
    expect(screen.queryByRole('alertdialog')).toBeNull()
    // Its area is hidden now, and still mounted.
    expect(screen.getByRole('textbox', { name: 'Beta', hidden: true })).toHaveValue('draft')
    expect(within(areas()).getByRole('link', { name: /Library/ })).toContainElement(screen.getByLabelText('unsaved'))
  })

  it('never scrolls to, or unfolds around, an element named by the hash before its redirect', async () => {
    localStorage.setItem('scadbuddy.settings.collapsed', JSON.stringify(['decoy']))
    const tops: number[] = []
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function (this: Element) {
      const top = this.id === 'assistant-model' ? 500 : this.id === 'model' ? 300 : 0
      return { top, bottom: top, left: 0, right: 0, width: 0, height: 0, x: 0, y: top, toJSON: () => ({}) }
    })
    Object.defineProperty(HTMLElement.prototype, 'scrollTo', {
      configurable: true,
      value: (options: ScrollToOptions) => tops.push(options.top ?? -1),
    })
    try {
      const { user } = renderSettingsShell(withDecoy)
      await screen.findByLabelText('Alpha')
      // Followed once the page is loaded: the stale hash is in the address for one render.
      await user.click(screen.getByRole('link', { name: 'Old link' }))
      await waitFor(() => expect(where()).toBe('/settings/assistant#model'))
      await waitFor(() => expect(tops).toContain(292))
      expect(tops).not.toContain(492)
      expect(screen.getByRole('button', { name: 'Decoy', hidden: true })).toHaveAttribute('aria-expanded', 'false')
    } finally {
      delete (HTMLElement.prototype as { scrollTo?: unknown }).scrollTo
    }
  })
})
