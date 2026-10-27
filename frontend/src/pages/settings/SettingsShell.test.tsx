import { screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'
import { Link, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Section } from './controls'
import { useReportDirty } from './dirty'
import type { SettingsSection } from './registry'
import { enter, renderSettingsShell } from './testing'

// #2171 — Settings by area: one address per area, every area mounted at once so drafts
// survive a switch, and the leave guard asking only when the page itself is left.

const availability = vi.hoisted(() => ({ available: true }))
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

const areas = () => screen.getByRole('navigation', { name: 'Settings areas' })
const sectionLinks = () =>
  within(screen.getByRole('navigation', { name: 'Settings sections' }))
    .queryAllByRole('link')
    .map((link) => link.textContent)

describe('SettingsShell (#2171)', () => {
  afterEach(() => {
    availability.available = true
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

  it('shows one notice on an agent-served area while the assistant is unavailable', async () => {
    availability.available = false
    const notice = 'The assistant service is not answering: the settings it serves are hidden until it does.'
    const { user } = renderSettingsShell(fakes, { route: '/settings/tools' })
    await waitFor(() => expect(screen.getAllByRole('status')).toHaveLength(1))
    expect(screen.getByRole('status')).toHaveTextContent(notice)
    await user.click(within(areas()).getByRole('link', { name: 'General' }))
    expect(screen.queryByRole('status')).toBeNull()
  })

  it('scrolls the pane to the top on an area switch', async () => {
    const { user, container } = renderSettingsShell(fakes)
    await screen.findByLabelText('Alpha')
    const pane = container.querySelector<HTMLElement>('.sb-settings')!
    const tops: number[] = []
    pane.scrollTo = ((options: ScrollToOptions) => tops.push(options.top ?? -1)) as typeof pane.scrollTo
    await user.click(within(areas()).getByRole('link', { name: 'Library' }))
    expect(tops).toEqual([0])
  })
})
