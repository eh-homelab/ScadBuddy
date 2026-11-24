import { screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { listCatalog, listTypes, upsertCatalog, upsertType, type SpoolTypeCreate } from '../../api/filament'
import { listShopping } from '../../api/filamentPurchasing'
import { server } from '../../mocks/server'
import { SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { M, ST, T } from '../../mocks/features/filament/seed'
import { FILAMENT_PETG } from '../../mocks/features/orders/state'
import { renderPage } from '../../test/utils'
import { filamentLabel } from '../../components/orders/FilamentsEditor'
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

  it('adds a type to the shopping list from its row', async () => {
    const { user } = renderPage(<TypesTab />)
    const jade = typeRows(await types()).find((row) => row.dataset.type === T.JADE)!
    await user.click(within(jade).getByRole('button', { name: /^Buy BL-PLA-BASIC-JADE-WHITE/ }))
    expect(await screen.findByText('Added BL-PLA-BASIC-JADE-WHITE to the shopping list.')).toBeInTheDocument()
    expect((await listShopping()).filter((item) => item.type_id === T.JADE)).toHaveLength(1)
  })

  it('adds one item for a double click on Buy', async () => {
    const { user } = renderPage(<TypesTab />)
    const jade = typeRows(await types()).find((row) => row.dataset.type === T.JADE)!
    const buy = within(jade).getByRole('button', { name: /^Buy BL-PLA-BASIC-JADE-WHITE/ })
    await user.dblClick(buy)
    await screen.findByText('Added BL-PLA-BASIC-JADE-WHITE to the shopping list.')
    expect((await listShopping()).filter((item) => item.type_id === T.JADE)).toHaveLength(1)
  })

  it('shows the newest per-kg price of a type from its suppliers', async () => {
    renderPage(<TypesTab />)
    const rows = typeRows(await types())
    const jade = rows.find((row) => row.dataset.type === T.JADE)!
    await waitFor(() => expect(within(jade).getByText('$17.305/kg')).toBeInTheDocument())
    const mistletoe = rows.find((row) => row.dataset.type === T.MISTLETOE)!
    expect(within(mistletoe).getByLabelText('No supplier price')).toBeInTheDocument()
  })

  it('keeps the prices of the sheets that loaded when one fails, and does not call the rest unpriced', async () => {
    server.use(http.get(`/api/v1/filament/suppliers/${SUPPLIER.MICRO}/price-sheet`, () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    renderPage(<TypesTab />)
    const rows = typeRows(await types())
    const jade = rows.find((row) => row.dataset.type === T.JADE)!
    await waitFor(() => expect(within(jade).getByText('$17.305/kg')).toBeInTheDocument())
    const inland = rows.find((row) => row.dataset.type === T.INLAND_BLACK)!
    expect(within(inland).getByText('Not loaded')).toBeInTheDocument()
    expect(within(inland).queryByLabelText('No supplier price')).toBeNull()
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

    // #7159 — a row whose brand and colour a type has opens a choice instead of a duplicate.
    describe('when a type of the brand and colour exists (#7159)', () => {
      /** The type PATCH bodies sent while `run` runs; the listener is dropped whatever happens. */
      async function typePatches(run: () => Promise<void>): Promise<unknown[]> {
        const bodies: Promise<unknown>[] = []
        server.events.on('request:start', ({ request }) => {
          if (request.method === 'PATCH' && request.url.includes('/filament/types/')) bodies.push(request.clone().json())
        })
        try {
          await run()
          return await Promise.all(bodies)
        } finally {
          server.events.removeAllListeners()
        }
      }
      const dialogOf = () => screen.findByRole('dialog', { name: 'This filament may already have a type' })

      it('links the exact match, chosen first, and sets only its cost', async () => {
        const row = await api.createFilament({ material: 'PLA', subtype: 'Silk', brand: 'elegoo', effect: 'silk', colour: '#d4af37', cost_per_kg: '22.5' })
        const before = (await listTypes()).length
        const patches = await typePatches(async () => {
          const { user } = renderPage(<TypesTab />)
          await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
          const dialog = await dialogOf()
          expect(within(dialog).getByRole('radio', { name: /EL-PLA-SILK-GOLD/ })).toBeChecked()
          expect(within(dialog).getByText(/PLA, Silk, 1000 g label, 1 spool$/)).toBeInTheDocument()
          expect(within(dialog).queryByText(/this row still has no type/)).toBeNull()
          await user.click(within(dialog).getByRole('button', { name: 'Link EL-PLA-SILK-GOLD' }))
          await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('link', { name: 'EL-PLA-SILK-GOLD' })
        })
        expect(patches).toEqual([{ cost_per_kg: '22.5' }])
        expect((await listTypes()).length).toBe(before)
        expect((await listTypes()).find((type) => type.id === T.GOLD)).toMatchObject({ filament_id: row.id, subtype_id: ST.SILK, label_weight_g: 1000 })
      })

      it('links a type whose only difference is its effect, which the price book does not match on', async () => {
        const row = await api.createFilament({ material: 'PLA', subtype: 'Silk', brand: 'Elegoo', colour: '#D4AF37', cost_per_kg: '23' })
        const { user } = renderPage(<TypesTab />)
        await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
        const dialog = await dialogOf()
        expect(within(dialog).getByText('Its effect differs from this row')).toBeInTheDocument()
        expect(within(dialog).queryByText(/this row still has no type/)).toBeNull()
        await user.click(within(dialog).getByRole('button', { name: 'Link EL-PLA-SILK-GOLD' }))
        await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('link', { name: 'EL-PLA-SILK-GOLD' })
      })

      // The prod case: the existing type has another subtype, so the price book cannot link it to this row.
      it('offers a type of another subtype too, says first that it cannot link this row, and only sets its cost', async () => {
        const row = await api.createFilament({ material: 'PLA', brand: 'Elegoo', colour: '#D4AF37', cost_per_kg: '21' })
        const patches = await typePatches(async () => {
          const { user } = renderPage(<TypesTab />)
          await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
          const dialog = await dialogOf()
          expect(within(dialog).getByRole('radio', { name: /EL-PLA-SILK-GOLD/ })).toBeChecked()
          expect(within(dialog).getByText('Its subtype and effect differ from this row')).toBeInTheDocument()
          expect(within(dialog).getByText(/this row still has no type\. Setting its cost gives 21 per kg to its own price book row/)).toBeInTheDocument()
          await user.click(within(dialog).getByRole('button', { name: "Set EL-PLA-SILK-GOLD's cost" }))
          expect(await screen.findByRole('status', { name: 'Type link' })).toHaveTextContent(/EL-PLA-SILK-GOLD now costs 21 per kg.*its own price book row/)
        })
        expect(patches).toEqual([{ cost_per_kg: '21' }])
        expect(within(screen.getByTestId(`filament-${row.id}`)).getByRole('button', { name: /Make a type/ })).toBeInTheDocument()
      })

      // #7344: the price book matches the alpha, so a type that differs only in it cannot link this row.
      it('offers a type that differs only in opacity, says first that it cannot link this row, and only sets its cost', async () => {
        const row = await api.createFilament({ material: 'PLA', subtype: 'Silk', brand: 'Elegoo', effect: 'silk', colour: '#D4AF37', alpha: 128, cost_per_kg: '24' })
        const patches = await typePatches(async () => {
          const { user } = renderPage(<TypesTab />)
          await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
          const dialog = await dialogOf()
          expect(within(dialog).getByRole('radio', { name: /EL-PLA-SILK-GOLD/ })).toBeChecked()
          expect(within(dialog).getByText('Its opacity differs from this row')).toBeInTheDocument()
          expect(within(dialog).getByText(/this row still has no type\. Setting its cost gives 24 per kg to its own price book row/)).toBeInTheDocument()
          expect(within(dialog).queryByRole('button', { name: 'Link EL-PLA-SILK-GOLD' })).toBeNull()
          await user.click(within(dialog).getByRole('button', { name: "Set EL-PLA-SILK-GOLD's cost" }))
          expect(await screen.findByRole('status', { name: 'Type link' })).toHaveTextContent(/EL-PLA-SILK-GOLD now costs 24 per kg.*its own price book row/)
        })
        expect(patches).toEqual([{ cost_per_kg: '24' }])
        expect(within(screen.getByTestId(`filament-${row.id}`)).getByRole('button', { name: /Make a type/ })).toBeInTheDocument()
      })

      // #7344: the dialog's warning is a forecast of the server's match; the PATCH answer's `filament_id` is the truth.
      it('says which row the server linked, not what the dialog forecast', async () => {
        const row = await api.createFilament({ material: 'PLA', brand: 'Elegoo', colour: '#D4AF37', cost_per_kg: '21' })
        server.use(
          http.patch('/api/v1/filament/types/:id', async ({ request, params }) => {
            const type = (await listTypes()).find((one) => one.id === params.id)
            return HttpResponse.json({ ...type, ...((await request.json()) as object), filament_id: row.id })
          }),
        )
        const { user } = renderPage(<TypesTab />)
        await user.click(await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ }))
        const dialog = await dialogOf()
        expect(within(dialog).getByText(/this row still has no type/)).toBeInTheDocument()
        await user.click(within(dialog).getByRole('button', { name: "Set EL-PLA-SILK-GOLD's cost" }))
        expect(await screen.findByRole('status', { name: 'Type link' })).toHaveTextContent(`Linked EL-PLA-SILK-GOLD to ${filamentLabel(row)}.`)
      })

      it('makes a new type anyway when asked, and nothing on Cancel', async () => {
        const row = await api.createFilament({ material: 'PLA', brand: 'Elegoo', colour: '#D4AF37', cost_per_kg: '21' })
        const before = (await listTypes()).length
        const { user } = renderPage(<TypesTab />)
        const make = await within(await screen.findByTestId(`filament-${row.id}`)).findByRole('button', { name: /Make a type/ })
        await user.click(make)
        await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Cancel' }))
        await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
        expect((await listTypes()).length).toBe(before)
        await user.click(make)
        await user.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Make a new type' }))
        await waitFor(async () => expect((await listTypes()).length).toBe(before + 1))
        expect((await listTypes()).find((type) => type.filament_id === row.id)).toMatchObject({ rgba: '#D4AF37FF', subtype_id: null })
      })
    })
  })

  it('fills every type from its slicer profile and reports each', async () => {
    const { user } = renderPage(<TypesTab />)
    await types()
    await user.click(screen.getByRole('button', { name: 'Fill from slicer profiles' }))
    const report = await screen.findByRole('region', { name: 'Fill from slicer profiles' })
    expect(within(report).getByText(/Filled 20 fields on 3 types/)).toBeInTheDocument()
    expect(within(report).getByRole('link', { name: 'BL-PLA-BASIC-JADE-WHITE' })).toBeInTheDocument()
    expect(within(report).getAllByText(/the type has no slicer preset/).length).toBeGreaterThan(0)
    const jade = (await listTypes()).find((type) => type.id === T.JADE)!
    expect([jade.nozzle_temp_min, jade.density_g_cm3]).toEqual([190, '1.26'])
  })
})
