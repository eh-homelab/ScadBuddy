import { screen, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { getLocation } from '../../api/filament'
import { L, spoolId } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { LocationPage } from './LocationPage'

const open = (id: string) => renderPage(<LocationPage />, { route: `/filament/locations/${id}`, path: '/filament/locations/:id' })

describe('the location page', () => {
  it('shows a slot: name, code, path, its spool and a label link', async () => {
    open(L.AMS_A1)
    expect(await screen.findByRole('heading', { level: 1, name: 'H2C AMS-A slot 1' })).toBeInTheDocument()
    expect(screen.getByText('L-4')).toBeInTheDocument()
    const path = screen.getByRole('navigation', { name: 'Path' })
    expect(within(path).getByRole('link', { name: '3DP-31B-598' })).toHaveAttribute('href', `/filament/locations/${L.H2C}`)
    expect(within(path).getByRole('link', { name: 'H2C AMS-A' })).toHaveAttribute('href', `/filament/locations/${L.AMS_A}`)
    const spool = screen.getByRole('link', { name: /S-4/ })
    expect(spool).toHaveAttribute('href', `/filament/spools/${spoolId(4)}`)
    expect(spool).toHaveTextContent('120 g')
    expect(screen.getByRole('link', { name: 'Print label' })).toHaveAttribute('href', `/filament/labels?locations=${L.AMS_A1}`)
    expect(screen.queryByRole('link', { name: 'Printer settings' })).toBeNull()
    expect(screen.getByRole('link', { name: 'View spools' })).toHaveAttribute('href', `/filament/spools?place=${L.AMS_A1}`)
    expect(screen.queryByText('Archived')).toBeNull()
  })

  it('shows Archived for an archived place', async () => {
    const found = await getLocation(L.SHELF_A)
    server.use(http.get('*/api/v1/filament/locations/:id', () => HttpResponse.json({ ...found, archived_at: '2026-11-01T00:00:00Z' })))
    open(L.SHELF_A)
    expect(await screen.findByText('Archived')).toBeInTheDocument()
  })

  it('shows a printer place: its units and the settings link', async () => {
    open(L.H2C)
    expect(await screen.findByRole('heading', { level: 1, name: '3DP-31B-598' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'H2C AMS-A' })).toHaveAttribute('href', `/filament/locations/${L.AMS_A}`)
    expect(screen.getByRole('link', { name: 'Printer settings' })).toHaveAttribute('href', '/settings/printing#printers')
  })

  it('says so for an unknown id', async () => {
    open('00000000-0000-4000-8000-000000000000')
    expect(await screen.findByText('No such location')).toBeInTheDocument()
  })

  it('has no fixed width that is wider than a phone', async () => {
    // jsdom has no layout, so a real 390 px check is not possible here: the root must be fluid and its long text must wrap or truncate.
    const { container } = open(L.AMS_A1)
    await screen.findByRole('heading', { level: 1 })
    const root = container.firstElementChild as HTMLElement
    expect(root.className).toMatch(/\bmax-w-/)
    expect(root.className).not.toMatch(/(^|\s)(min-)?w-\[?[1-9]/)
    expect(screen.getByRole('heading', { level: 1 }).className).toMatch(/break-words|truncate/)
  })
})
