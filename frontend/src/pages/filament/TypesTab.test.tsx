import { screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { listCatalog, listTypes, upsertCatalog, upsertType, type SpoolTypeCreate } from '../../api/filament'
import { server } from '../../mocks/server'
import { M, ST, T } from '../../mocks/features/filament/seed'
import { FILAMENT_PETG } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { TypesTab } from './TypesTab'

const types = () => screen.findByRole('table', { name: 'Types' })
const typeRows = (table: HTMLElement) => within(table).getAllByRole('row').filter((row) => row.dataset.type)

describe('the Types tab', () => {
  it('lists each type with its code, colour and price', async () => {
    renderPage(<TypesTab />)
    const rows = typeRows(await types())
    const jade = rows.find((row) => row.dataset.type === T.JADE)!
    expect(within(jade).getByRole('link', { name: 'BL-PLA-BASIC-JADE-WHITE' })).toHaveAttribute('href', `/filament/types/${T.JADE}`)
    expect(jade.querySelector('[data-colour]')).toHaveAttribute('data-colour', '#FFFFFFFF')
    expect(within(jade).getByText('15.91')).toBeInTheDocument()
    const orange = rows.find((row) => row.dataset.type === T.ORANGE)!
    expect(orange.querySelector('[data-colour]')).toHaveAttribute('data-colour', '#FF6A1380')
  })

  it('shows a type with no price as unpriced', async () => {
    renderPage(<TypesTab />)
    const mistletoe = typeRows(await types()).find((row) => row.dataset.type === T.MISTLETOE)!
    expect(within(mistletoe).getByText('No price')).toBeInTheDocument()
  })

  it('has a New type link', async () => {
    renderPage(<TypesTab />)
    await types()
    expect(screen.getByRole('link', { name: 'New type' })).toHaveAttribute('href', '/filament/types/new')
  })

  describe('the price book panel', () => {
    it('lists the book rows; a row with no type has Make a type', async () => {
      renderPage(<TypesTab />)
      const book = await screen.findByRole('region', { name: 'Price book' })
      const black = await within(book).findByTestId(`filament-${FILAMENT_PETG}`)
      expect(within(black).getByRole('button', { name: /Make a type/ })).toBeInTheDocument()
    })

    it('Make a type posts the row material, colour and alpha, and then links to the type', async () => {
      const { user } = renderPage(<TypesTab />)
      const book = await screen.findByRole('region', { name: 'Price book' })
      const black = await within(book).findByTestId(`filament-${FILAMENT_PETG}`)
      await user.click(within(black).getByRole('button', { name: /Make a type/ }))
      await waitFor(async () => expect((await listTypes()).some((type) => type.rgba === '#1E1E1EFF')).toBe(true))
      const made = (await listTypes()).find((type) => type.rgba === '#1E1E1EFF')!
      expect(made).toMatchObject({ cost_per_kg: '30', colour_name: 'Black' })
      expect(made.filament_id).toBe(FILAMENT_PETG)
      const link = await within(await screen.findByTestId(`filament-${FILAMENT_PETG}`)).findByRole('link', { name: made.code })
      expect(link).toHaveAttribute('href', `/filament/types/${made.id}`)
      expect(await types()).toHaveTextContent(made.code)
    })

    it('offers no Make a type while the types are still being read', async () => {
      server.use(
        http.get('/api/v1/filament/types', async () => {
          await delay(400)
          return HttpResponse.json([])
        }),
      )
      renderPage(<TypesTab />)
      const book = await screen.findByRole('region', { name: 'Price book' })
      await within(book).findByTestId(`filament-${FILAMENT_PETG}`)
      expect(within(book).queryByRole('button', { name: /Make a type/ })).toBeNull()
      expect(within(book).queryByRole('columnheader', { name: 'Type' })).toBeNull()
      expect((await within(book).findAllByRole('button', { name: /Make a type/ })).length).toBeGreaterThan(0)
    })

    it('keeps the translucent alpha of a book row in the type it makes', async () => {
      const row = await api.createFilament({ material: 'PLA', colour: '#336699', alpha: 128, cost_per_kg: '20' })
      const { user } = renderPage(<TypesTab />)
      const made = await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ })
      await user.click(made)
      await waitFor(async () => expect((await listTypes()).some((type) => type.rgba === '#33669980')).toBe(true))
    })

    it('makes the subtype under the row material, not the same name of another material', async () => {
      const petgBasic = await upsertCatalog('subtypes', { name: 'Basic', material_id: M.PETG })
      const row = await api.createFilament({ material: 'PETG', subtype: 'Basic', colour: '#112233', cost_per_kg: '22' })
      const { user } = renderPage(<TypesTab />)
      await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
      await waitFor(async () => expect((await listTypes()).some((type) => type.rgba === '#112233FF')).toBe(true))
      const made = (await listTypes()).find((type) => type.rgba === '#112233FF')!
      expect(made.subtype_id).toBe(petgBasic.id)
      expect(made.subtype_id).not.toBe(ST.BASIC)
    })

    it('creates a missing subtype under the row material', async () => {
      const row = await api.createFilament({ material: 'PETG', subtype: 'Galaxy', colour: '#445566', cost_per_kg: '22' })
      const { user } = renderPage(<TypesTab />)
      await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
      await waitFor(async () => expect((await listTypes()).some((type) => type.rgba === '#445566FF')).toBe(true))
      const made = (await listTypes()).find((type) => type.rgba === '#445566FF')!
      expect((await listCatalog('subtypes')).find((one) => one.id === made.subtype_id)).toMatchObject({ name: 'Galaxy', material_id: M.PETG })
    })

    it('says which catalog names stay when the type itself is refused', async () => {
      const taken: SpoolTypeCreate = { code: 'ZED-PLA-RED', material_id: M.PLA, rgba: '#FF0000FF', label_weight_g: 1000, diameter_mm: '1.75' }
      await upsertType(taken)
      const row = await api.createFilament({ material: 'PLA', brand: 'Zed', colour_name: 'Red', colour: '#FF0000', cost_per_kg: '20' })
      const { user } = renderPage(<TypesTab />)
      await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
      expect(await screen.findByRole('alert')).toHaveTextContent(/Zed stays in the catalog/)
    })
  })
})
