import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { BrowserRouter, Link, Route, Routes, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AiAvailability } from '../../agent/chat/availability'
import { Section } from './controls'
import { useReportDirty } from './dirty'
import type { SettingsSection } from './registry'
import { SettingsProviders, SettingsShell } from './SettingsShell'
import { enter, renderSettingsShell } from './testing'

// #2171 — Settings by area: one address per area, every area mounted at once so drafts
// survive a switch, and the leave guard asking only when the page itself is left.

const availability = vi.hoisted((): AiAvailability => ({ available: true, state: 'configured' }))
vi.mock('../../agent/chat/availability', () => ({ useAiAvailability: () => availability }))

/** The current address, path and hash, as the router has it. */
function Where() {
  const { pathname, hash } = useLocation()
  return <span data-testid="where">{`${pathname}${hash}`}</span>
}

const where = () => screen.getByTestId('where').textContent

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
    </Section>
  )
}

function Packages() {
  return (
    <Section id="packages" title="Packages">
      <p>Plugin packages</p>
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
