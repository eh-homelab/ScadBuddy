import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { listCatalog, upsertCatalog } from '../../api/filament'
import { server } from '../../mocks/server'
import { M } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { CatalogTab } from './CatalogTab'

const list = (name: string) => screen.findByRole('table', { name })
const rowOf = (table: HTMLElement, name: string) => {
  const found = within(table).getAllByRole('row').find((row) => row.dataset.name === name)
  if (!found) throw new Error(`no row ${name}`)
  return found
}

describe('the Catalog tab', () => {
  it('archives Wood: it leaves the default list and shows under Archived', async () => {
    await upsertCatalog('materials', { name: 'Wood' })
    const { user } = renderPage(<CatalogTab />)
    const materials = await list('Materials')
    await user.click(within(rowOf(materials, 'Wood')).getByRole('button', { name: 'Archive Wood' }))
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Materials' })).queryByText('Wood')).toBeNull())
    await user.click(screen.getByRole('checkbox', { name: 'Show archived' }))
    const shown = await list('Materials')
    await waitFor(() => expect(within(shown).getByText('Wood')).toBeInTheDocument())
    expect(within(rowOf(shown, 'Wood')).getByRole('button', { name: 'Restore Wood' })).toBeInTheDocument()
    expect((await listCatalog('materials')).some((row) => row.name === 'Wood')).toBe(false)
  })

  it('restores an archived row', async () => {
    const wood = await upsertCatalog('materials', { name: 'Wood' })
    await upsertCatalog('materials', { archived: true }, wood.id)
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?archived=true' })
    await user.click(within(rowOf(await list('Materials'), 'Wood')).getByRole('button', { name: 'Restore Wood' }))
    await waitFor(async () => expect((await listCatalog('materials')).some((row) => row.name === 'Wood')).toBe(true))
  })

  it('picks the catalog from the URL', async () => {
    renderPage(<CatalogTab />, { route: '/filament/catalog?kind=brands' })
    expect(within(await list('Brands')).getByText('Polymaker')).toBeInTheDocument()
  })

  it('adds a row and refuses a name that is taken, with the server text', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=brands' })
    await list('Brands')
    await user.type(screen.getByLabelText('New name'), 'Prusament')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Brands' })).getByText('Prusament')).toBeInTheDocument())
    await user.type(screen.getByLabelText('New name'), 'Prusament')
    await user.click(screen.getByRole('button', { name: 'Add' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('a row of this catalog already has that name')
  })

  it('renames a row', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=brands' })
    await user.click(within(rowOf(await list('Brands'), 'Sunlu')).getByRole('button', { name: 'Edit Sunlu' }))
    const name = screen.getByLabelText('Name')
    await user.clear(name)
    await user.type(name, 'SUNLU')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(within(screen.getByRole('table', { name: 'Brands' })).getByText('SUNLU')).toBeInTheDocument())
  })

  it('moves a row up, which renumbers sort_order', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=brands' })
    await user.click(within(rowOf(await list('Brands'), 'Inland')).getByRole('button', { name: 'Move Inland up' }))
    await waitFor(async () => {
      const brands = await listCatalog('brands')
      expect(brands.findIndex((row) => row.name === 'Inland')).toBeLessThan(brands.findIndex((row) => row.name === 'Insignia'))
    })
  })

  it('edits a material value columns', async () => {
    const { user } = renderPage(<CatalogTab />)
    await user.click(within(rowOf(await list('Materials'), 'PLA')).getByRole('button', { name: 'Edit PLA' }))
    const max = screen.getByLabelText('Nozzle max (°C)')
    await user.clear(max)
    await user.type(max, '235')
    await user.click(screen.getByRole('checkbox', { name: 'Abrasive' }))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => {
      const pla = (await listCatalog('materials')).find((row) => row.id === M.PLA)!
      expect(pla).toMatchObject({ nozzle_temp_max: 235, abrasive: true })
    })
  })

  it('shows an insert type bb_catalog_id read only', async () => {
    const real = await listCatalog('insert-types')
    server.use(
      http.get('/api/v1/filament/catalog/insert-types', () => HttpResponse.json(real.map((row) => ({ ...row, bb_catalog_id: 4242 })))),
    )
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=insert-types' })
    await user.click(within(rowOf(await list('Insert types'), 'Cardboard spool')).getByRole('button', { name: 'Edit Cardboard spool' }))
    const form = screen.getByRole('form', { name: 'Edit Cardboard spool' })
    expect(within(form).getByText('Bambuddy catalog id')).toBeInTheDocument()
    expect(within(form).getByText('4242')).toBeInTheDocument()
    expect(within(form).queryByDisplayValue('4242')).toBeNull()
  })

  it('names the row whose re-order failed, and reloads', async () => {
    server.use(http.patch('/api/v1/filament/catalog/brands/:id', () => HttpResponse.json({ detail: 'busy' }, { status: 500 })))
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=brands' })
    await user.click(within(rowOf(await list('Brands'), 'Inland')).getByRole('button', { name: 'Move Inland up' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/Could not move .*: busy/)
  })

  it('keeps focus on the moved row after a re-order', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=brands' })
    await user.click(within(rowOf(await list('Brands'), 'Inland')).getByRole('button', { name: 'Move Inland up' }))
    await waitFor(() => expect(within(rowOf(screen.getByRole('table', { name: 'Brands' }), 'Inland')).getByRole('button', { name: 'Move Inland up' })).toHaveFocus())
  })

  it('names an archived row a value points at, marked archived', async () => {
    await upsertCatalog('subtypes', { name: 'Tough', material_id: M.TPU })
    await upsertCatalog('materials', { archived: true }, M.TPU)
    renderPage(<CatalogTab />, { route: '/filament/catalog?kind=subtypes' })
    expect(within(rowOf(await list('Subtypes'), 'Tough')).getByText('Material: TPU (archived)')).toBeInTheDocument()
  })

  it('edits the extra colours of a colour as a list of hex values', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=colours' })
    await user.click(within(rowOf(await list('Colours'), 'Hot Pink')).getByRole('button', { name: 'Edit Hot Pink' }))
    const extra = screen.getByLabelText('Extra colours')
    await user.type(extra, '#112233, 445566AA')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect((await listCatalog('colours')).find((row) => row.name === 'Hot Pink')?.extra_colours).toEqual(['#112233FF', '#445566AA']))
  })

  it('refuses an extra colour that is not hex, and sends nothing', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=colours' })
    await user.click(within(rowOf(await list('Colours'), 'Hot Pink')).getByRole('button', { name: 'Edit Hot Pink' }))
    await user.type(screen.getByLabelText('Extra colours'), 'teal')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Extra colours')
    expect((await listCatalog('colours')).find((row) => row.name === 'Hot Pink')?.extra_colours).toBeNull()
  })

  it('clears the extra colours when only a comma is left, and refuses more than 16', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=colours' })
    await user.click(within(rowOf(await list('Colours'), 'Hot Pink')).getByRole('button', { name: 'Edit Hot Pink' }))
    const extra = screen.getByLabelText('Extra colours')
    await user.type(extra, Array.from({ length: 17 }, () => '#112233').join(','))
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('at most 16')
    expect((await listCatalog('colours')).find((row) => row.name === 'Hot Pink')?.extra_colours).toBeNull()
    await user.clear(extra)
    await user.type(extra, '#112233')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect((await listCatalog('colours')).find((row) => row.name === 'Hot Pink')?.extra_colours).toEqual(['#112233FF']))
    await user.click(within(rowOf(await list('Colours'), 'Hot Pink')).getByRole('button', { name: 'Edit Hot Pink' }))
    const again = screen.getByLabelText('Extra colours')
    await user.clear(again)
    await user.type(again, ',')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect((await listCatalog('colours')).find((row) => row.name === 'Hot Pink')?.extra_colours).toBeNull())
  })

  it('edits a colour with the colour field', async () => {
    const { user } = renderPage(<CatalogTab />, { route: '/filament/catalog?kind=colours' })
    const table = await list('Colours')
    expect(within(rowOf(table, 'Hot Pink')).getByTestId('rgba-swatch')).toHaveAttribute('data-colour', '#F5547CFF')
    await user.click(within(rowOf(table, 'Hot Pink')).getByRole('button', { name: 'Edit Hot Pink' }))
    const hex = screen.getByLabelText('Colour (hex)')
    await user.clear(hex)
    await user.type(hex, '#112233')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    await waitFor(async () => expect((await listCatalog('colours')).find((row) => row.name === 'Hot Pink')?.rgba).toBe('#112233FF'))
  })
})
