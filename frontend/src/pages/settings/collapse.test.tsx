import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it } from 'vitest'
import { COLLAPSED_KEY } from './collapseState'
import { SettingsCollapseProvider } from './collapse'
import { Section } from './controls'

// #2173 — each Settings section folds from its heading, remembered per browser.

const FOLDS = [
  { id: 'g-model', title: 'Model & limits' },
  { id: 'g-audit', title: 'Audit log' },
]

function Harness({ known }: { known?: readonly string[] }) {
  return (
    <SettingsCollapseProvider known={known}>
      <Section id="g-model" title="Model & limits">
        <label>
          Model <input />
        </label>
      </Section>
      <Section id="g-audit" title="Audit log">
        <p>audit rows</p>
      </Section>
    </SettingsCollapseProvider>
  )
}

describe('SettingsCollapseProvider', () => {
  afterEach(() => localStorage.clear())

  it('drops a fold the page no longer has from the stored list, keeping unrendered ones (#3415)', async () => {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(['g-renamed', 'g-hidden-today']))
    const user = userEvent.setup()
    render(<Harness known={[...FOLDS.map((fold) => fold.id), 'g-hidden-today']} />)
    await user.click(screen.getByRole('button', { name: 'Audit log' }))
    expect(JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '[]')).toEqual(['g-hidden-today', 'g-audit'])
  })

  it('drops a stale fold on mount, without waiting for a toggle (#3415 review)', () => {
    localStorage.setItem(COLLAPSED_KEY, JSON.stringify(['g-renamed', 'g-audit']))
    render(<Harness known={FOLDS.map((fold) => fold.id)} />)
    expect(JSON.parse(localStorage.getItem(COLLAPSED_KEY) ?? '[]')).toEqual(['g-audit'])
  })

  it('opens everything when the browser keeps nothing readable', () => {
    localStorage.setItem(COLLAPSED_KEY, 'not json')
    render(<Harness />)
    expect(screen.getByRole('button', { name: 'Model & limits' })).toHaveAttribute('aria-expanded', 'true')
  })
})
