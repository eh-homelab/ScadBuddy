import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import { Section } from '../controls'
import { SECTION_REGISTRY, type SettingsSection } from '../registry'
import { renderSettingsSection, renderSettingsShell } from '../testing'
import { Component as Printers } from './printers'

/** The current address, path and hash, as the router has it. */
function Where() {
  const { pathname, hash } = useLocation()
  return (
    <Section id="where" title="Where">
      <span data-testid="where">{`${pathname}${hash}`}</span>
    </Section>
  )
}

const withWhere: readonly SettingsSection[] = [
  ...SECTION_REGISTRY,
  { id: 'where', area: 'printing', title: 'Where', order: 999, Component: Where },
]

const where = () => screen.getByTestId('where').textContent

describe('Printers', () => {
  it('has a Printers section with the registry (#3823)', async () => {
    renderSettingsSection(<Printers />, { route: '/settings/printing' })
    const section = await screen.findByRole('region', { name: 'Printers' })
    expect(await within(section).findByRole('listitem', { name: 'Printer Garage' })).toBeInTheDocument()
  })

  it('opens the Printers and Orders sections from their old anchors', async () => {
    const first = renderSettingsShell(withWhere, { route: '/settings#printers' })
    await waitFor(() => expect(where()).toBe('/settings/printing#printers'))
    expect(await screen.findByRole('region', { name: 'Printers' })).toBeInTheDocument()
    first.unmount()

    renderSettingsShell(withWhere, { route: '/settings#orders' })
    await waitFor(() => expect(where()).toBe('/settings/printing#orders'))
    expect(await screen.findByRole('region', { name: 'Orders' })).toBeInTheDocument()
  })
})
