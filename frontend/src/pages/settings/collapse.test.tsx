import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, useLocation } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'
import { COLLAPSED_KEY } from './collapseState'
import { SectionContents, SettingsCollapseProvider, SubSection } from './collapse'

// #2769 — a long section's groups, each its own anchor and fold, with a contents list.

const GROUPS = [
  { id: 'g-model', title: 'Model & limits' },
  { id: 'g-mcp', title: 'MCP access' },
  { id: 'g-audit', title: 'Audit log' },
]

function Nothing() {
  return null
}

/** The address, as the section nav reads it: a contents link puts its group there. */
function Hash() {
  return <output aria-label="address">{useLocation().hash}</output>
}

function Harness({ known }: { known?: readonly string[] }) {
  return (
    <MemoryRouter>
      <SettingsCollapseProvider known={known}>
        <Hash />
        <SectionContents label="Assistant settings" items={GROUPS} />
        <SubSection id="g-model" title="Model & limits">
          <label>
            Model <input />
          </label>
        </SubSection>
        {/* Every setting in it hides itself, as without the agent's database. */}
        <SubSection id="g-mcp" title="MCP access">
          <Nothing />
        </SubSection>
        <SubSection id="g-audit" title="Audit log">
          <p>audit rows</p>
        </SubSection>
      </SettingsCollapseProvider>
    </MemoryRouter>
  )
}

describe('SubSection and SectionContents (#2769)', () => {
  afterEach(() => localStorage.clear())

  it('lists each group that shows anything, and hides the empty one with its line', async () => {
    render(<Harness />)
    const contents = screen.getByRole('navigation', { name: 'Assistant settings' })
    await waitFor(() =>
      expect(within(contents).getAllByRole('link').map((link) => link.textContent)).toEqual([
        'Model & limits',
        'Audit log',
      ]),
    )
    expect(screen.getByRole('group', { name: 'Model & limits' })).toBeVisible()
    expect(screen.queryByRole('group', { name: 'MCP access' })).not.toBeInTheDocument()
  })

  it('folds a group from its heading, and its contents link unfolds it again', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    const toggle = screen.getByRole('button', { name: 'Audit log' })
    await user.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.getByText('audit rows')).not.toBeVisible()
    expect(JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '[]')).toEqual(['g-audit'])

    const contents = screen.getByRole('navigation', { name: 'Assistant settings' })
    await user.click(within(contents).getByRole('link', { name: 'Audit log' }))
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    expect(screen.getByText('audit rows')).toBeVisible()
    // Scrolling to it is the section nav's, which follows the address (#2345).
    expect(screen.getByRole('status', { name: 'address' })).toHaveTextContent('#g-audit')
  })

  it('drops a fold the page no longer has from the stored list, keeping unrendered ones (#3415)', async () => {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(['g-renamed', 'g-hidden-today']))
    const user = userEvent.setup()
    render(<Harness known={[...GROUPS.map((group) => group.id), 'g-hidden-today']} />)
    await user.click(screen.getByRole('button', { name: 'Audit log' }))
    expect(JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '[]')).toEqual(['g-hidden-today', 'g-audit'])
  })

  it('opens everything when the browser keeps nothing readable', () => {
    localStorage.setItem(COLLAPSED_KEY, 'not json')
    render(<Harness />)
    expect(screen.getByRole('button', { name: 'Model & limits' })).toHaveAttribute('aria-expanded', 'true')
  })
})
