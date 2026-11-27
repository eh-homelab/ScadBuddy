import { screen, waitFor, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { listLocations } from '../../api/filament'
import { L, spoolId } from '../../mocks/features/filament/seed'
import { seedUnknownTray } from '../../mocks/features/filament/unknownTrays'
import { NeedsYouHarness } from '../../test/needsYou'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { LocationsTab } from './LocationsTab'

const tree = () => screen.findByRole('list', { name: 'Locations' })
const place = async (id: string) => {
  const found = within(await tree()).getAllByRole('listitem').find((item) => item.dataset.place === id)
  if (!found) throw new Error(`no place ${id}`)
  return found
}

describe('the Locations tab', () => {
  it('links to the printer labels', async () => {
    renderPage(<LocationsTab />)
    expect(await screen.findByRole('link', { name: 'Print printer labels' })).toHaveAttribute('href', '/filament/labels?printers=all')
  })

  it('shows the tree with each place spool count and its spools', async () => {
    renderPage(<LocationsTab />)
    await tree()
    const shelf = await place(L.SHELF_A)
    await waitFor(() => expect(within(shelf).getByRole('link', { name: /#1/ })).toBeInTheDocument())
    expect(within(shelf).getByLabelText('2 spools in Shelf A')).toBeInTheDocument()
    expect(within(shelf).getByRole('link', { name: /#1/ })).toHaveAttribute('href', `/filament/spools/${spoolId(1)}`)
    expect(within(shelf).getByRole('link', { name: /#3/ })).toBeInTheDocument()
    expect(within(await place(L.WORKSHOP)).getAllByText('Shelf A').length).toBeGreaterThan(0)
  })

  it('leaves an AMS tray read only', async () => {
    renderPage(<LocationsTab />)
    const tray = await place(L.AMS_A1)
    expect(within(tray).getByText('AMS tray, read only')).toBeInTheDocument()
    expect(within(tray).queryByRole('button', { name: /Edit|Archive/ })).toBeNull()
  })

  it('shows the 409 text inline when a move would make a cycle', async () => {
    const { user } = renderPage(<LocationsTab />)
    const workshop = await place(L.WORKSHOP)
    await user.click(within(workshop).getByRole('button', { name: 'Edit Workshop' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Inside' }), 'Workshop / Shelf A')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('a place cannot be its own parent or sit under one of its own')
    expect((await listLocations()).find((node) => node.id === L.WORKSHOP)?.parent_id).toBeNull()
  })

  it('links a place name to its page and its spool count to the spools filter', async () => {
    renderPage(<LocationsTab />)
    const shelf = await place(L.SHELF_A)
    expect(within(shelf).getByRole('link', { name: 'Shelf A' })).toHaveAttribute('href', `/filament/locations/${L.SHELF_A}`)
    expect(within(shelf).getByRole('link', { name: /spools? in Shelf A/ })).toHaveAttribute('href', `/filament/spools?place=${L.SHELF_A}`)
  })

  it('renames a place', async () => {
    const { user } = renderPage(<LocationsTab />)
    await user.click(within(await place(L.SHELF_A)).getByRole('button', { name: 'Edit Shelf A' }))
    const name = await screen.findByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Shelf B')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    await waitFor(async () => expect(within(await place(L.SHELF_A)).getAllByText('Shelf B').length).toBeGreaterThan(0))
  })

  it('renames an AMS unit place (#7153), while its trays stay read only', async () => {
    const { user } = renderPage(<LocationsTab />)
    await user.click(within(await place(L.AMS_A)).getByRole('button', { name: 'Edit H2C AMS-A' }))
    const name = await screen.findByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'Left rack')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    await waitFor(async () => expect((await listLocations()).flatMap(function all(n): typeof n[] {
      return [n, ...(n.children ?? []).flatMap(all)]
    }).find((n) => n.id === L.AMS_A)?.name).toBe('Left rack'))
    expect(within(await place(L.AMS_A1)).queryByRole('button', { name: /Edit/ })).toBeNull()
  })

  it('adds a place under another', async () => {
    const { user } = renderPage(<LocationsTab />)
    await tree()
    await user.click(screen.getByRole('button', { name: 'Add a place' }))
    await user.type(await screen.findByLabelText('Name'), 'Drawer 2')
    await choose(user, screen.getByRole('combobox', { name: 'Kind' }), 'Shelf')
    await choose(user, screen.getByRole('combobox', { name: 'Inside' }), 'Workshop')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect(((await listLocations())[0]!.children ?? []).map((child) => child.name)).toContain('Drawer 2'))
  })

  it('does not add a copy when the place being changed has left the list', async () => {
    const { user } = renderPage(<LocationsTab />)
    await user.click(within(await place(L.DRY_BOX)).getByRole('button', { name: 'Archive Dry box 1' }))
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Locations' })).queryByText('Dry box 1')).toBeNull())
    const toggle = screen.getByRole('checkbox', { name: 'Show archived places' })
    await user.click(toggle)
    await user.click(within(await place(L.DRY_BOX)).getByRole('button', { name: 'Edit Dry box 1' }))
    await screen.findByLabelText('Name')
    await user.click(toggle)
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Locations' })).queryByText('Dry box 1')).toBeNull())
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('That place is no longer in the list')
    expect(JSON.stringify(await listLocations({ includeArchived: true })).split('"name":"Dry box 1"')).toHaveLength(2)
  })

  it('archives a place, which leaves the tree', async () => {
    const { user } = renderPage(<LocationsTab />)
    await user.click(within(await place(L.DRY_BOX)).getByRole('button', { name: 'Archive Dry box 1' }))
    await waitFor(() => expect(within(screen.getByRole('list', { name: 'Locations' })).queryByText('Dry box 1')).toBeNull())
  })
})

describe('the Locations tab · unknown trays (#7143)', () => {
  it('lists the unknown trays above the tree', async () => {
    seedUnknownTray()
    renderPage(
      <NeedsYouHarness>
        <LocationsTab />
      </NeedsYouHarness>,
    )
    expect(await screen.findByText('Unknown spool in 3DP-31B-598 AMS-A slot 2')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Tell ScadBuddy which spool…' })).toBeInTheDocument()
  })
})
