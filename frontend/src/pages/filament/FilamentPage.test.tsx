import { screen, within } from '@testing-library/react'
import { Navigate, Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { renderPage } from '../../test/utils'
import { FilamentPage } from './FilamentPage'
import { FILAMENT_TABS } from './tabs'

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/filament" element={<Navigate to="/filament/spools" replace />} />
      {FILAMENT_TABS.filter((tab) => tab.available).map((tab) => (
        <Route key={tab.id} path={tab.path} element={<FilamentPage tab={tab.id} />} />
      ))}
    </Routes>,
    { route },
  )
}

describe('the Filament section', () => {
  it('lists every tab of spec §10 in order, with only the built ones available', () => {
    expect(FILAMENT_TABS.map((tab) => tab.id)).toEqual([
      'spools',
      'types',
      'tags',
      'suppliers',
      'invoices',
      'locations',
      'catalog',
      'metrics',
      'shopping-list',
      'ledger',
    ])
    expect(FILAMENT_TABS.filter((tab) => !tab.available).map((tab) => tab.id)).toEqual([
      'suppliers',
      'invoices',
      'metrics',
      'shopping-list',
    ])
    for (const tab of FILAMENT_TABS) expect(tab.path).toBe(`/filament/${tab.id}`)
  })

  it('shows six tabs in spec order and no "Invoices"', () => {
    render('/filament/spools')
    const tabs = screen.getByRole('navigation', { name: 'Filament' })
    const links = within(tabs).getAllByRole('link')
    expect(links.map((link) => link.textContent)).toEqual(['Spools', 'Types', 'Tags', 'Locations', 'Catalog', 'Ledger'])
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      '/filament/spools',
      '/filament/types',
      '/filament/tags',
      '/filament/locations',
      '/filament/catalog',
      '/filament/ledger',
    ])
    expect(within(tabs).queryByRole('link', { name: 'Invoices' })).toBeNull()
  })

  it('opens /filament on Spools, and marks the open tab', () => {
    render('/filament')
    expect(screen.getByRole('heading', { level: 1, name: 'Filament' })).toBeInTheDocument()
    const tabs = screen.getByRole('navigation', { name: 'Filament' })
    expect(within(tabs).getByRole('link', { name: 'Spools' })).toHaveAttribute('aria-current', 'page')
    expect(within(tabs).getByRole('link', { name: 'Ledger' })).not.toHaveAttribute('aria-current')
  })

  it('marks Ledger on its own page', () => {
    render('/filament/ledger')
    const tabs = screen.getByRole('navigation', { name: 'Filament' })
    expect(within(tabs).getByRole('link', { name: 'Ledger' })).toHaveAttribute('aria-current', 'page')
  })

  it('mounts the Tags, Locations, Catalog and Ledger tabs', async () => {
    for (const [route, find] of [
      ['/filament/tags', () => screen.findByRole('table', { name: 'Tags' })],
      ['/filament/locations', () => screen.findByRole('list', { name: 'Locations' })],
      ['/filament/catalog', () => screen.findByRole('table', { name: 'Materials' })],
      ['/filament/ledger', () => screen.findByRole('table', { name: 'Ledger' })],
    ] as const) {
      const view = render(route)
      await find()
      view.unmount()
    }
  })
})
