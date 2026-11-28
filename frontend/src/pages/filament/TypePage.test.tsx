import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Link, Route, Routes, useLocation } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { getType, listTypes, startProfileFetch, upsertType } from '../../api/filament'
import { listShopping, patchSupplier } from '../../api/filamentPurchasing'
import { SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { server } from '../../mocks/server'
import { entityMock } from '../../mocks/features/entities'
import { T } from '../../mocks/features/filament/seed'
import { filamentLinks } from '../../lib/filamentLinks'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { TypePage } from './TypePage'

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/filament/types/:id" element={<TypePage />} />
      <Route path="*" element={<p>Elsewhere</p>} />
    </Routes>,
    { route },
  )
}

const code = () => screen.findByLabelText('Code')

describe('the type page', () => {
  it('shows the type: its fields, presets, K values and spools', async () => {
    render(`/filament/types/${T.JADE}`)
    expect(await code()).toHaveValue('BL-PLA-BASIC-JADE-WHITE')
    expect(screen.getByLabelText('Cost per kg')).toHaveValue('15.91')
    expect(screen.getByLabelText('Colour name')).toHaveValue('Jade White')
    expect(screen.getByRole('combobox', { name: 'Material' })).toHaveTextContent('PLA')
    expect(within(screen.getByRole('table', { name: 'Slicer presets' })).getByDisplayValue('GFA00')).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: 'K values' })).getByDisplayValue('0.020')).toBeInTheDocument()
    const spools = screen.getByRole('table', { name: 'Spools of this type' })
    expect(await within(spools).findByRole('link', { name: '#1' })).toBeInTheDocument()
  })

  it('links to its label', async () => {
    render(`/filament/types/${T.JADE}`)
    expect(await screen.findByRole('link', { name: 'Print label' })).toHaveAttribute('href', filamentLinks.typeLabel(T.JADE))
  })

  it('has no label link on the new type page', async () => {
    render('/filament/types/new')
    await code()
    expect(screen.queryByRole('link', { name: 'Print label' })).toBeNull()
  })

  it('saves a new 0.6 preset with one PUT of the presets, and the old row stays', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Add preset' }))
    const row = screen.getByRole('row', { name: 'New preset' })
    await user.type(within(row).getByLabelText('Printer model'), 'H2C')
    await user.clear(within(row).getByLabelText('Nozzle'))
    await user.type(within(row).getByLabelText('Nozzle'), '0.6')
    await user.type(within(row).getByLabelText('Slicer filament'), 'GFA00')
    await user.type(within(row).getByLabelText('Preset name'), 'Bambu PLA Basic @BBL H2C 0.6 nozzle')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).presets).toHaveLength(2))
    const presets = (await getType(T.JADE)).presets
    expect(presets).toContainEqual(expect.objectContaining({ printer_model: 'H2C', nozzle_diameter: '0.6', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C 0.6 nozzle' }))
    expect(presets).toContainEqual(expect.objectContaining({ nozzle_diameter: '0.4' }))
  })

  it('does not save a preset whose nozzle is *', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    const nozzle = within(screen.getByRole('table', { name: 'Slicer presets' })).getByLabelText('Nozzle')
    await user.clear(nozzle)
    await user.type(nozzle, '*')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findAllByText(/is not a nozzle/)).not.toHaveLength(0)
    expect((await getType(T.JADE)).presets[0]!.nozzle_diameter).toBe('0.4')
  })

  it('saves a changed field with a PATCH and a changed K value with a PUT', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    const cost = await screen.findByLabelText('Cost per kg')
    await user.clear(cost)
    await user.type(cost, '17.5')
    const k = within(screen.getByRole('table', { name: 'K values' })).getByLabelText('K value')
    await user.clear(k)
    await user.type(k, '0.025')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).k_values[0]!.k_value).toBe('0.025'))
    expect((await getType(T.JADE)).cost_per_kg).toMatch(/^17\.5/)
    expect((await getType(T.JADE)).k_values[0]).toMatchObject({ cali_idx: 3, name: 'Jade White 0.4' })
  })

  it('shows and saves the drying values (#6720)', async () => {
    const { user } = render(`/filament/types/${T.ORANGE}`)
    const temp = await screen.findByLabelText('Drying temperature (°C)')
    expect(temp).toHaveValue('70')
    const hours = screen.getByLabelText('Drying time (h)')
    expect(hours).toHaveValue('')
    await user.type(hours, '7.5')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.ORANGE)).drying_hours).toMatch(/^7\.5/))
    expect((await getType(T.ORANGE)).drying_temp_c).toBe(70)
  })

  it('refuses drying hours that are not a number', async () => {
    const { user } = render(`/filament/types/${T.ORANGE}`)
    await user.type(await screen.findByLabelText('Drying time (h)'), 'soon')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findAllByText('Enter the drying hours as a number, such as 1.24.')).not.toHaveLength(0)
  })

  it('shows the server refusal and keeps the edits', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await user.type(await screen.findByLabelText('Bed temperature, min (°C)'), '90')
    await user.type(screen.getByLabelText('Bed temperature, max (°C)'), '60')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('bed_temp_min is above bed_temp_max')
    expect(screen.getByLabelText('Bed temperature, max (°C)')).toHaveValue('60')
  })

  it('holds navigation while edits are unsaved', async () => {
    const { user } = renderPage(
      <>
        <Link to="/other">Away</Link>
        <Routes>
          <Route path="/filament/types/:id" element={<TypePage />} />
          <Route path="*" element={<p>Elsewhere</p>} />
        </Routes>
      </>,
      { route: `/filament/types/${T.JADE}` },
    )
    await user.type(await screen.findByLabelText('Colour name'), 'x')
    await user.click(screen.getByRole('link', { name: 'Away' }))
    expect(await screen.findByText('Leave without saving?')).toBeInTheDocument()
    expect(screen.queryByText('Elsewhere')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Leave without saving' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })

  it('adds a new type from the new page', async () => {
    const { user } = render('/filament/types/new')
    await user.type(await code(), 'TEST-PLA-RED')
    await choose(user, screen.getByRole('combobox', { name: 'Material' }), 'PLA')
    await user.click(screen.getByRole('button', { name: 'Translucent' }))
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await listTypes()).some((type) => type.code === 'TEST-PLA-RED')).toBe(true))
    const made = (await listTypes()).find((type) => type.code === 'TEST-PLA-RED')!
    expect(made.rgba).toMatch(/80$/)
    expect(made.label_weight_g).toBe(1000)
  })

  it('fills the colour and the extra colours from a catalog colour, and saves them (#6912)', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'From the catalog' }))
    await user.type(screen.getByRole('combobox', { name: 'Search the colour catalog' }), 'galaxy')
    await user.click(await screen.findByRole('option', { name: /Galaxy Duo/ }))
    expect(screen.getByRole('textbox', { name: 'Colour (hex)' })).toHaveValue('#1B3A8CFF')
    expect(screen.getByLabelText('Extra colours')).toHaveValue('#2E8B57FF')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).extra_colours).toEqual(['#2E8B57FF']))
    expect((await getType(T.JADE)).rgba).toBe('#1B3A8CFF')
  })

  it('says a type is gone for a 404', async () => {
    render('/filament/types/62620000-0000-4000-8000-0000000000ff')
    expect(await screen.findByText(/That type is gone/)).toBeInTheDocument()
  })

  it('keeps a created type as one type: a failed preset save retries as an edit, never a second POST', async () => {
    let posts = 0
    const puts: string[] = []
    let refuse = true
    server.use(
      http.post('/api/v1/filament/types', () => {
        posts += 1
        return undefined
      }),
      http.put('/api/v1/filament/types/:id/presets', ({ params }) => {
        puts.push(String(params.id))
        if (refuse) return HttpResponse.json({ type: 'about:blank', title: 'Unprocessable', status: 422, detail: 'preset refused' }, { status: 422, headers: { 'Content-Type': 'application/problem+json' } })
        return undefined
      }),
    )
    const { user } = render('/filament/types/new')
    await user.type(await code(), 'TEST-RETRY')
    await choose(user, screen.getByRole('combobox', { name: 'Material' }), 'PLA')
    await user.click(screen.getByRole('button', { name: 'Add preset' }))
    const row = screen.getByRole('row', { name: 'New preset' })
    await user.type(within(row).getByLabelText('Printer model'), 'H2C')
    await user.type(within(row).getByLabelText('Slicer filament'), 'GFA00')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('preset refused')
    const made = (await listTypes()).filter((type) => type.code === 'TEST-RETRY')
    expect(made).toHaveLength(1)
    refuse = false
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(made[0]!.id)).presets).toHaveLength(1))
    expect(posts).toBe(1)
    expect(puts).toEqual([made[0]!.id, made[0]!.id])
    expect((await listTypes()).filter((type) => type.code === 'TEST-RETRY')).toHaveLength(1)
  })

  it('moves to the new type address with replace once everything saved, and says Saved', async () => {
    function Where() {
      return <p data-testid="where">{useLocation().pathname}</p>
    }
    const { user } = renderPage(
      <>
        <Where />
        <Routes>
          <Route path="/filament/types/:id" element={<TypePage />} />
        </Routes>
      </>,
      { route: '/filament/types/new' },
    )
    await user.type(await code(), 'TEST-WHERE')
    await choose(user, screen.getByRole('combobox', { name: 'Material' }), 'PLA')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => {
      const made = (await listTypes()).find((type) => type.code === 'TEST-WHERE')
      expect(made).toBeDefined()
      expect(screen.getByTestId('where')).toHaveTextContent(`/filament/types/${made!.id}`)
    })
  })

  it('says Saved after a save, in a polite status', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await user.type(await screen.findByLabelText('Colour name'), 'x')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findByRole('status')).toHaveTextContent('Saved')
    expect(screen.getByRole('button', { name: 'Save type' })).toBeDisabled()
  })

  it('releases the leave guard once saved', async () => {
    const { user } = renderPage(
      <>
        <Link to="/other">Away</Link>
        <Routes>
          <Route path="/filament/types/:id" element={<TypePage />} />
          <Route path="*" element={<p>Elsewhere</p>} />
        </Routes>
      </>,
      { route: `/filament/types/${T.JADE}` },
    )
    await user.type(await screen.findByLabelText('Colour name'), 'x')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await screen.findByRole('status')
    await user.click(screen.getByRole('link', { name: 'Away' }))
    expect(await screen.findByText('Elsewhere')).toBeInTheDocument()
  })

  it('clears the subtype when the material changes to one it does not belong to', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    expect(screen.getByRole('combobox', { name: 'Subtype' })).toHaveTextContent('Basic')
    await choose(user, screen.getByRole('combobox', { name: 'Material' }), 'PETG')
    expect(screen.getByRole('combobox', { name: 'Subtype' })).not.toHaveTextContent('Basic')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).material_id).not.toBe((await listTypes()).find((one) => one.id === T.BLACK)!.material_id))
    expect((await getType(T.JADE)).subtype_id).toBeNull()
  })

  it('offers only the subtypes of the chosen material', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await choose(user, screen.getByRole('combobox', { name: 'Material' }), 'PETG')
    await user.click(screen.getByRole('combobox', { name: 'Subtype' }))
    const options = (await screen.findAllByRole('option')).map((one) => one.textContent)
    expect(options).toContain('HF')
    expect(options).not.toContain('Silk')
  })
})

describe('the prices panel', () => {
  const history = () => screen.findByRole('table', { name: 'Price history of Bambu Lab A00-W1' })
  const dates = (table: HTMLElement) => within(table).getAllByRole('row').slice(1).map((row) => row.querySelector('time')?.getAttribute('datetime'))

  it('lists the seeded history newest first, per pack and per kg', async () => {
    render(`/filament/types/${T.JADE}`)
    const table = await history()
    expect(dates(table)).toEqual(['2026-10-08', '2026-09-14', '2026-08-02'])
    const newest = within(table).getAllByRole('row')[1]!
    expect(within(newest).getAllByText('$17.305')).toHaveLength(2)
    expect(within(newest).getByText('Invoice')).toBeInTheDocument()
    expect(within(table).getAllByText('Added by hand')).toHaveLength(2)
    expect(screen.getByRole('img', { name: 'Price per kg of Bambu Lab A00-W1 over time' })).toBeInTheDocument()
  })

  it('fills the cost field with the newest per-kg price and sends nothing until Save', async () => {
    const writes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method !== 'GET') writes.push(`${request.method} ${new URL(request.url).pathname}`)
    })
    const { user } = render(`/filament/types/${T.JADE}`)
    await history()
    const cost = screen.getByLabelText('Cost per kg')
    expect(cost).toHaveValue('15.91')
    expect(screen.getByRole('button', { name: 'Save type' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Use as default cost from Bambu Lab A00-W1' }))
    expect((cost as HTMLInputElement).value).toMatch(/^17\.305/)
    expect(screen.getByRole('status')).toHaveTextContent('Save the type to keep it')
    expect(screen.getByRole('button', { name: 'Save type' })).toBeEnabled()
    expect(writes).toEqual([])
    expect((await getType(T.JADE)).cost_per_kg).toMatch(/^15\.91/)
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).cost_per_kg).toMatch(/^17\.305/))
    expect(writes).toEqual([`PATCH /api/v1/filament/types/${T.JADE}`])
  })

  it("fills the cost field from the calculator, the label weight as the net weight, and sends nothing until Save (#7152)", async () => {
    const writes: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.method !== 'GET') writes.push(`${request.method} ${new URL(request.url).pathname}`)
    })
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Work out the cost per kg' }))
    const calculator = screen.getByRole('region', { name: 'Cost calculator' })
    expect(within(calculator).getByLabelText('Net weight per spool (g)')).toHaveValue('1000')
    await user.type(within(calculator).getByLabelText(/^Price paid/), '21.99')
    await user.type(within(calculator).getByLabelText('Tax'), '1.76')
    await user.click(within(calculator).getByRole('button', { name: 'Work it out' }))
    await user.click(await within(calculator).findByRole('button', { name: 'Use as default cost' }))
    expect(screen.getByLabelText('Cost per kg')).toHaveValue('23.7500')
    expect(screen.getByRole('status')).toHaveTextContent('Save the type to keep it')
    expect(writes).toEqual([])
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).cost_per_kg).toMatch(/^23\.75/))
    expect(writes).toEqual([`PATCH /api/v1/filament/types/${T.JADE}`])
  })

  it('offers no Add price for a product of an archived supplier', async () => {
    await patchSupplier(SUPPLIER.BAMBU, { archived: true })
    render(`/filament/types/${T.JADE}`)
    await history()
    expect(screen.getByRole('article', { name: 'Bambu Lab A00-W1' })).toHaveTextContent('Archived')
    expect(screen.queryByRole('button', { name: 'Add price for Bambu Lab A00-W1' })).toBeNull()
  })

  it('says a type with no price has none yet', async () => {
    render(`/filament/types/${T.MISTLETOE}`)
    expect(await screen.findByText(/No prices yet/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /Use as default cost/ })).toBeNull()
  })

  it('is not on a new type', async () => {
    render('/filament/types/new')
    await code()
    expect(screen.queryByRole('region', { name: 'Prices' })).toBeNull()
  })

  it('adds a price with the shared form and shows it first', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    const table = await history()
    await user.click(screen.getByRole('button', { name: 'Add price for Bambu Lab A00-W1' }))
    await user.type(screen.getByLabelText('Price with tax for Bambu Lab A00-W1'), '17.25')
    const date = screen.getByLabelText('Date for Bambu Lab A00-W1')
    await user.clear(date)
    await user.type(date, '2026-11-16')
    await user.click(screen.getByRole('button', { name: 'Add price' }))
    await waitFor(() => expect(dates(table)[0]).toBe('2026-11-16'))
    expect(within(table).getAllByRole('row')[1]).toHaveTextContent('$17.25')
  })

  it('adds a supplier product for the type from the sheet', async () => {
    const { user } = render(`/filament/types/${T.MISTLETOE}`)
    await screen.findByText(/No prices yet/)
    await user.click(screen.getByRole('button', { name: 'Add supplier product' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Supplier' }), 'Micro Center')
    await user.type(screen.getByLabelText('SKU'), 'MC-MIST')
    await user.click(screen.getByRole('button', { name: 'Add product' }))
    expect(await screen.findByRole('article', { name: 'Micro Center MC-MIST' })).toBeInTheDocument()
  })

  it('adds the type to the shopping list as one pending item', async () => {
    const { user } = render(`/filament/types/${T.MISTLETOE}`)
    await code()
    const before = (await listShopping()).length
    await user.click(screen.getByRole('button', { name: 'Add to shopping list' }))
    expect(await screen.findByText(/Added to the shopping list/)).toBeInTheDocument()
    const after = await listShopping()
    expect(after).toHaveLength(before + 1)
    expect(after.filter((item) => item.type_id === T.MISTLETOE)).toEqual([expect.objectContaining({ quantity_spools: 1, status: 'pending' })])
  })

  it('adds one item for a double click on Add to shopping list', async () => {
    const { user } = render(`/filament/types/${T.MISTLETOE}`)
    await code()
    const before = (await listShopping()).filter((item) => item.type_id === T.MISTLETOE).length
    await user.dblClick(screen.getByRole('button', { name: 'Add to shopping list' }))
    await screen.findByText(/Added to the shopping list/)
    expect((await listShopping()).filter((item) => item.type_id === T.MISTLETOE)).toHaveLength(before + 1)
  })

  it('refuses a product page that is not http or https', async () => {
    const { user } = render(`/filament/types/${T.MISTLETOE}`)
    await screen.findByText(/No prices yet/)
    await user.click(screen.getByRole('button', { name: 'Add supplier product' }))
    await choose(user, await screen.findByRole('combobox', { name: 'Supplier' }), 'Micro Center')
    await user.type(screen.getByLabelText('SKU'), 'MC-MIST')
    await user.type(screen.getByLabelText('Product page'), 'javascript:alert(1)')
    await user.click(screen.getByRole('button', { name: 'Add product' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('must start with http')
  })

  it('saves a lead time and a safety margin (#6269), and clears them back to the defaults', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    const lead = await screen.findByLabelText('Lead time (days)')
    expect(lead).toHaveValue('')
    await user.type(lead, '14')
    await user.type(screen.getByLabelText('Safety margin (days)'), '5')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect(await getType(T.JADE)).toMatchObject({ lead_time_days: 14, safety_margin_days: 5 }))
    await user.clear(screen.getByLabelText('Lead time (days)'))
    await user.clear(screen.getByLabelText('Safety margin (days)'))
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect(await getType(T.JADE)).toMatchObject({ lead_time_days: null, safety_margin_days: null }))
  })

  it('shows the seeded lead time and refuses one over 365 days', async () => {
    const { user } = render(`/filament/types/${T.ORANGE}`)
    const lead = await screen.findByLabelText('Lead time (days)')
    expect(lead).toHaveValue('30')
    expect(screen.getByLabelText('Safety margin (days)')).toHaveValue('90')
    await user.clear(lead)
    await user.type(lead, '400')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findByText('Enter the lead time as at most 365 days.')).toBeInTheDocument()
    expect((await getType(T.ORANGE)).lead_time_days).toBe(30)
  })

  it('fills from the slicer profile: shows the diff, and writes only the rows kept', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Fill from slicer profile' }))
    const dialog = await screen.findByRole('dialog', { name: 'Fill from slicer profile' })
    expect(within(dialog).getByText(/Bambu PLA Basic @BBL H2C 0.4 nozzle/)).toBeInTheDocument()
    const diff = within(dialog).getByRole('table', { name: 'Changes from the profile' })
    const density = within(diff).getByRole('row', { name: /Density/ })
    expect(density).toHaveTextContent('Empty')
    expect(density).toHaveTextContent('1.26')
    // The diameter already matches, so it is not offered; the cost is a hint only.
    expect(within(diff).queryByRole('row', { name: /Diameter/ })).toBeNull()
    expect(within(dialog).getByText(/24.99/)).toBeInTheDocument()
    await user.click(within(density).getByRole('checkbox'))
    // The drying values come from the profile too (#6720).
    expect(within(diff).getByRole('row', { name: /Drying temperature/ })).toHaveTextContent('55')
    await user.click(within(dialog).getByRole('button', { name: 'Apply 6 changes' }))
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull())
    const type = await getType(T.JADE)
    expect([type.nozzle_temp_min, type.nozzle_temp_max, type.bed_temp_min, type.bed_temp_max, type.density_g_cm3]).toEqual([190, 230, 35, 65, null])
    expect([type.drying_temp_c, type.drying_hours]).toEqual([55, '8'])
    expect(screen.getByLabelText('Nozzle temperature, min (°C)')).toHaveValue('190')
    expect(screen.getByLabelText('Drying temperature (°C)')).toHaveValue('55')
    expect(await screen.findByText('Filled 6 fields from Bambu PLA Basic @BBL H2C 0.4 nozzle.')).toBeInTheDocument()
  })

  it('says why a type cannot be filled from a profile', async () => {
    const { user } = render(`/filament/types/${T.MISTLETOE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Fill from slicer profile' }))
    const dialog = await screen.findByRole('dialog', { name: 'Fill from slicer profile' })
    expect(await within(dialog).findByText(/the type has no slicer preset/)).toBeInTheDocument()
    expect(within(dialog).queryByRole('button', { name: /^Apply/ })).toBeNull()
  })

  it('asks to save first while there are unsaved edits', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await user.type(await screen.findByLabelText('Colour name'), 'x')
    expect(screen.getByRole('button', { name: 'Fill from slicer profile' })).toBeDisabled()
  })
})

describe('the 3DFP id and fetch (#6625)', () => {
  afterEach(() => vi.useRealTimers())

  it('saves a 3DFP id with the PATCH and links to its page on 3DFP', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    const field = screen.getByLabelText('3DFP id')
    expect(screen.queryByRole('link', { name: 'Open on 3DFP' })).not.toBeInTheDocument()
    await user.type(field, '1234')
    const link = screen.getByRole('link', { name: 'Open on 3DFP' })
    expect(link).toHaveAttribute('href', 'https://3dfilamentprofiles.com/filament/details/1234')
    expect(link).toHaveAttribute('target', '_blank')
    expect(link).toHaveAttribute('rel', 'noopener noreferrer')
    // The fetch uses the saved id: an unsaved one says to save first.
    expect(screen.getByRole('button', { name: 'Fetch from 3DFP' })).toHaveAccessibleDescription('Save the type first: the fetch uses the saved 3DFP id.')
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    await waitFor(async () => expect((await getType(T.JADE)).tdfp_id).toBe(1234))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Fetch from 3DFP' })).toBeEnabled())
  })

  it('refuses a 3DFP id that is not a whole number from 1 to 2147483647', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.type(screen.getByLabelText('3DFP id'), '2147483648')
    expect(screen.queryByRole('link', { name: 'Open on 3DFP' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save type' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter the 3DFP id as a whole number from 1 to 2147483647.')
    expect((await getType(T.JADE)).tdfp_id).toBeNull()
  })

  it('shows the presets a fetch set once it ends, and keeps an unsaved edit of another field', async () => {
    await upsertType({ tdfp_id: 1234 }, T.JADE)
    await startProfileFetch(T.JADE)
    // The page opens on a running fetch that the mock ends on its next read, 10 s later.
    server.use(
      http.get('*/api/v1/filament/types/:id/profile-fetch', ({ params }) => {
        server.resetHandlers()
        return HttpResponse.json({
          run: { id: '00000000-0000-4000-8000-00000000fe01', type_id: String(params.id), tdfp_id: 1234, status: 'running', targets: [{ printer_model: 'H2C', nozzles: ['0.2', '0.4', '0.6', '0.8'] }], missing: [], k_values: 0, notes: [], error: null, started_at: '2026-11-18T12:00:00Z', finished_at: null },
          gaps: [],
        })
      }),
    )
    vi.useFakeTimers({ shouldAdvanceTime: true })
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    const last = await screen.findByRole('status', { name: 'Last fetch' })
    expect(last).toHaveTextContent('Running')
    await user.clear(screen.getByLabelText('Colour name'))
    await user.type(screen.getByLabelText('Colour name'), 'Jade')
    await vi.advanceTimersByTimeAsync(10_000)
    await waitFor(() => expect(last).toHaveTextContent('Imported'))
    const table = screen.getByRole('table', { name: 'Slicer presets' })
    await waitFor(() => expect(within(table).getAllByDisplayValue('H2C')).toHaveLength(4))
    for (const nozzle of ['0.2', '0.4', '0.6', '0.8']) expect(within(table).getByDisplayValue(nozzle)).toBeInTheDocument()
    expect(screen.getByLabelText('Colour name')).toHaveValue('Jade')
  })

  describe('defined and required properties (#7703)', () => {
    it('saves the row and its defined properties with one request', async () => {
      const patches: Record<string, unknown>[] = []
      server.events.on('request:start', ({ request }) => {
        if (request.method === 'PATCH' && new URL(request.url).pathname === `/api/v1/filament/types/${T.JADE}`) void request.clone().json().then((body) => patches.push(body as Record<string, unknown>))
      })
      entityMock.schemas.spool_type = { ...entityMock.schemas.spool_type!, required: ['mpn'] }
      const { user } = render(`/filament/types/${T.JADE}`)
      const note = await screen.findByLabelText('Note')
      await user.type(note, 'x')
      await user.click(screen.getByRole('button', { name: 'Save type' }))
      expect(await screen.findByText('Fill the required properties first: mpn.')).toBeVisible()
      expect(screen.getByLabelText('mpn')).toHaveAttribute('aria-invalid', 'true')
      await user.type(screen.getByLabelText('mpn'), 'A00-W1')
      await user.click(screen.getByRole('button', { name: 'Save type' }))
      await waitFor(() => expect(entityMock.properties.get(`spool_type:${T.JADE}`)?.map((p) => [p.key, p.value])).toEqual([['mpn', 'A00-W1']]))
      expect(patches.at(-1)).toEqual({ note: 'x', properties: { mpn: 'A00-W1' } })
      expect((await getType(T.JADE)).note).toBe('x')
    })

    // #7951: the type opens when only its properties cannot be read.
    it('opens the type and says so when its properties cannot be read (#7951)', async () => {
      server.use(http.get(`*/api/v1/entities/${encodeURIComponent(`spool_type:${T.JADE}`)}/properties`, () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
      render(`/filament/types/${T.JADE}`)
      expect(await screen.findByLabelText('Note')).toBeVisible()
      expect(await screen.findByText(/The type's properties could not be loaded/)).toBeVisible()
      expect(screen.queryByText(/The type could not be loaded/)).toBeNull()
    })

    // #7809 review: `before` is what was sent, so a blank list item does not keep the form dirty.
    it('is clean after saving a list with a blank trailing item', async () => {
      const { user } = render(`/filament/types/${T.JADE}`)
      await user.type(await screen.findByLabelText('gtin item 1'), '0123')
      await user.click(screen.getByRole('button', { name: 'Add gtin item' }))
      await user.click(screen.getByRole('button', { name: 'Save type' }))
      await waitFor(() => expect(entityMock.properties.get(`spool_type:${T.JADE}`)?.map((p) => [p.key, p.value])).toEqual([['gtin', ['0123']]]))
      await waitFor(() => expect(screen.getByRole('button', { name: 'Save type' })).toBeDisabled())
    })
  })
})

describe('finding and adding the 3DFP filament (#7025)', () => {
  // The request listeners each test adds end with it.
  afterEach(() => server.events.removeAllListeners())

  /** Each 3DFP request the page sends: its path, body and Idempotency-Key. */
  function tdfpRequests() {
    const sent: { path: string; body: Record<string, unknown>; key: string | null }[] = []
    server.events.on('request:start', async ({ request }) => {
      const path = new URL(request.url).pathname
      if (request.method === 'POST' && path.startsWith('/api/v1/filament/tdfp/'))
        sent.push({ path, body: (await request.clone().json()) as Record<string, unknown>, key: request.headers.get('Idempotency-Key') })
    })
    return sent
  }
  const finder = () => screen.getByRole('region', { name: 'Find on 3DFP' })

  it('searches with the type’s brand, material, subtype and colour, and a match fills the 3DFP id', async () => {
    const sent = tdfpRequests()
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    expect(within(finder()).getByLabelText('Brand')).toHaveValue('Bambu Lab')
    await user.click(within(finder()).getByRole('button', { name: 'Search 3DFP' }))
    const match = await within(finder()).findByRole('listitem', { name: /3DFP 4411/ })
    expect(sent[0]).toMatchObject({ path: '/api/v1/filament/tdfp/search', body: { brand: 'Bambu Lab', material: 'PLA', material_type: 'Basic', colour: 'Jade White' } })
    expect(match).toHaveTextContent('Jade White')
    expect(match).toHaveTextContent('1000 g')
    await user.click(within(match).getByRole('button', { name: 'Use this id' }))
    expect(screen.getByLabelText('3DFP id')).toHaveValue('4411')
    expect(screen.getByRole('status')).toHaveTextContent('The 3DFP id is now 4411. Save the type to keep it.')
    // Nothing was saved yet: the person saves the id with the type.
    expect((await getType(T.JADE)).tdfp_id).toBeNull()
  })

  it('says a search found nothing without saying 3DFP lacks it, and adds the missing filament to the type', async () => {
    const sent = tdfpRequests()
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    await user.clear(within(finder()).getByLabelText('Colour'))
    await user.type(within(finder()).getByLabelText('Colour'), 'Moon Rock')
    await user.click(within(finder()).getByRole('button', { name: 'Search 3DFP' }))
    expect(await within(finder()).findByText(/3DFP found no match\. Its index misses some brands/)).toBeInTheDocument()
    await user.click(within(finder()).getByRole('button', { name: 'Add the missing filament' }))
    const form = within(finder()).getByRole('form', { name: 'Add to 3DFP' })
    expect(within(form).getByLabelText('Colour name')).toHaveValue('Moon Rock')
    expect(within(form).getByLabelText('Hexes')).toHaveValue('#FFFFFF')
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    expect(await screen.findByText('Added to 3DFP as 38600. The type’s 3DFP id is now 38600.')).toBeInTheDocument()
    const create = sent.find((one) => one.path === '/api/v1/filament/tdfp/filaments')!
    expect(create.key).toBeTruthy()
    expect(create.body).toMatchObject({ brand: 'Bambu Lab', material: 'PLA', material_type: 'Basic', colour: 'Moon Rock', hexes: ['#8A8D8F'], nominal_weight_g: 1000, type_id: T.JADE })
    expect((await getType(T.JADE)).tdfp_id).toBe(38600)
    await waitFor(() => expect(screen.getByLabelText('3DFP id')).toHaveValue('38600'))
  })

  it('shows what 3DFP may already have, and adds anyway with a new key once the person says they differ', async () => {
    const sent = tdfpRequests()
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    await user.click(within(finder()).getByRole('button', { name: 'Add the missing filament' }))
    const form = within(finder()).getByRole('form', { name: 'Add to 3DFP' })
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    const dupes = await within(finder()).findByRole('list', { name: '3DFP may already have it' })
    expect(within(dupes).getByRole('listitem', { name: /3DFP 4411/ })).toHaveTextContent('same colour')
    expect((await getType(T.JADE)).tdfp_id).toBeNull()
    await user.click(within(dupes).getByRole('checkbox', { name: 'Not the same filament' }))
    await user.click(within(finder()).getByRole('button', { name: 'Add anyway' }))
    expect(await screen.findByText('Added to 3DFP as 38600. The type’s 3DFP id is now 38600.')).toBeInTheDocument()
    const creates = sent.filter((one) => one.path === '/api/v1/filament/tdfp/filaments')
    expect(creates).toHaveLength(2)
    expect(creates[1]!.body.not_duplicates).toEqual([4411])
    expect(creates[1]!.key).not.toBe(creates[0]!.key)
  })

  const openAdd = async (route = `/filament/types/${T.JADE}`) => {
    const page = render(route)
    await code()
    await page.user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    await page.user.click(within(finder()).getByRole('button', { name: 'Add the missing filament' }))
    return { ...page, form: within(finder()).getByRole('form', { name: 'Add to 3DFP' }) }
  }
  const answerCreate = (status: number, body: Record<string, unknown>) =>
    server.use(http.post('/api/v1/filament/tdfp/filaments', () => HttpResponse.json({ type: 'about:blank', title: 'x', status, ...body }, { status })))

  it('fills the id of a type not saved yet, for the person to save', async () => {
    const sent = tdfpRequests()
    const { user, form } = await openAdd('/filament/types/new')
    await user.type(within(form).getByLabelText('Brand'), 'Bambu Lab')
    await user.type(within(form).getByLabelText('Material'), 'PLA')
    await user.type(within(form).getByLabelText('Material type'), 'Basic')
    await user.type(within(form).getByLabelText('Colour name'), 'Moon Rock')
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    expect(await screen.findByText('Added to 3DFP as 38600. Save the type to keep the id.')).toBeInTheDocument()
    expect(screen.getByLabelText('3DFP id')).toHaveValue('38600')
    expect(sent.find((one) => one.path === '/api/v1/filament/tdfp/filaments')!.body.type_id).toBeNull()
  })

  it('sends #RRGGBB hexes when a colour with alpha is typed', async () => {
    const sent = tdfpRequests()
    const { user, form } = await openAdd()
    expect(within(form).getByText('#RRGGBB each, 1 to 4, separated by commas.')).toBeInTheDocument()
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8a8d8f80, #102030')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    await screen.findByText(/Added to 3DFP as/)
    expect(sent.find((one) => one.path === '/api/v1/filament/tdfp/filaments')!.body.hexes).toEqual(['#8A8D8F', '#102030'])
  })

  it('checks a changed filament again as a plain add after a duplicate', async () => {
    const { user, form } = await openAdd()
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    await within(finder()).findByRole('list', { name: '3DFP may already have it' })
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    expect(within(finder()).queryByRole('list', { name: '3DFP may already have it' })).not.toBeInTheDocument()
    expect(within(form).getByRole('button', { name: 'Add to 3DFP' })).toBeEnabled()
  })

  it('lets the person add anyway when the duplicate answer names only likely ids', async () => {
    let calls = 0
    server.use(
      http.post('/api/v1/filament/tdfp/filaments', async ({ request }) => {
        calls += 1
        const body = (await request.json()) as { not_duplicates?: number[] }
        if (calls === 1)
          return HttpResponse.json({ create: { status: 'duplicate', duplicates: [], likely: [{ tdfp_id: 4411, reason: 'close name' }], duplicate_text: 'Similar: Jade White' }, type_id: T.JADE, type_tdfp_id_set: false })
        expect(body.not_duplicates).toEqual([4411])
        return HttpResponse.json({ create: { status: 'created', tdfp_id: 38601, duplicate_text: '' }, type_id: T.JADE, type_tdfp_id_set: true })
      }),
    )
    const { user, form } = await openAdd()
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    const dupes = await within(finder()).findByRole('list', { name: '3DFP may already have it' })
    expect(within(finder()).getByText('Similar: Jade White')).toBeInTheDocument()
    await user.click(within(dupes).getByRole('checkbox', { name: 'Not the same filament' }))
    await user.click(within(finder()).getByRole('button', { name: 'Add anyway' }))
    expect(await screen.findByText(/Added to 3DFP as 38601/)).toBeInTheDocument()
  })

  it.each([
    [502, { detail: 'the site failed after the save', code: 'not_created' }, 'the site failed after the save. The filament may exist now: search before you add it again.'],
    [504, { detail: 'no outcome in time' }, 'No answer came in time. The add may still run: press the add button again to wait for it, not to add a second one.'],
  ] as const)('says what to do after a %i', async (status, body, text) => {
    answerCreate(status, body)
    const { user, form } = await openAdd()
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    expect(await within(finder()).findByRole('alert')).toHaveTextContent(text)
  })

  it('refuses a nominal weight that is not a number, and sends nothing', async () => {
    const sent = tdfpRequests()
    const { user, form } = await openAdd()
    await user.clear(within(form).getByLabelText('Nominal weight (g)'))
    await user.type(within(form).getByLabelText('Nominal weight (g)'), '1kg')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    expect(await within(finder()).findByRole('alert')).toHaveTextContent('Enter the nominal weight in grams, as a whole number.')
    expect(sent.filter((one) => one.path === '/api/v1/filament/tdfp/filaments')).toEqual([])
  })

  it('opens again with no matches, notice or error from the last time', async () => {
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    await user.click(within(finder()).getByRole('button', { name: 'Search 3DFP' }))
    await within(finder()).findByRole('listitem', { name: /3DFP 4411/ })
    await user.click(within(finder()).getByRole('button', { name: 'Close' }))
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    expect(within(finder()).queryByRole('list', { name: '3DFP matches' })).not.toBeInTheDocument()
  })

  it('reads the type again when it holds an id by the time the add answers, and keeps that id', async () => {
    server.use(
      http.post('/api/v1/filament/tdfp/filaments', async () => {
        // Another edit gave the type an id while the add ran (R-7025-14).
        await upsertType({ tdfp_id: 9999 }, T.JADE)
        return HttpResponse.json({ create: { status: 'created', tdfp_id: 38600, duplicate_text: '' }, type_id: T.JADE, type_tdfp_id_set: false })
      }),
    )
    const { user, form } = await openAdd()
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    expect(await screen.findByText('Added to 3DFP as 38600. The type already holds 3DFP id 9999, so it keeps it.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('3DFP id')).toHaveValue('9999'))
  })

  it('sends a second press after a 504 with the same Idempotency-Key', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post('/api/v1/filament/tdfp/filaments', ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        if (keys.length === 1) return HttpResponse.json({ type: 'about:blank', title: 'Gateway Timeout', status: 504, detail: 'no outcome in time' }, { status: 504 })
        return HttpResponse.json({ create: { status: 'created', tdfp_id: 38600, duplicate_text: '' }, type_id: T.JADE, type_tdfp_id_set: true })
      }),
    )
    const { user, form } = await openAdd()
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    await within(finder()).findByRole('alert')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    await screen.findByText(/Added to 3DFP as 38600/)
    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })

  it('fills the new id when the re-read type still has none', async () => {
    server.use(
      http.post('/api/v1/filament/tdfp/filaments', () =>
        HttpResponse.json({ create: { status: 'created', tdfp_id: 38600, duplicate_text: '' }, type_id: T.JADE, type_tdfp_id_set: false }),
      ),
    )
    const { user, form } = await openAdd()
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    expect(await screen.findByText('Added to 3DFP as 38600. Save the type to keep the id.')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByLabelText('3DFP id')).toHaveValue('38600'))
  })

  it('keeps the panel open while an add waits for its answer', async () => {
    let answer: () => void = () => {}
    const answered = new Promise<void>((resolve) => (answer = resolve))
    server.use(
      http.post('/api/v1/filament/tdfp/filaments', async () => {
        await answered
        return HttpResponse.json({ create: { status: 'created', tdfp_id: 38600, duplicate_text: '' }, type_id: T.JADE, type_tdfp_id_set: true })
      }),
    )
    const { user, form } = await openAdd()
    await user.clear(within(form).getByLabelText('Hexes'))
    await user.type(within(form).getByLabelText('Hexes'), '#8A8D8F')
    await user.click(within(form).getByRole('button', { name: 'Add to 3DFP' }))
    await waitFor(() => expect(within(finder()).getByRole('button', { name: 'Close' })).toBeDisabled())
    answer()
    await screen.findByText(/Added to 3DFP as 38600/)
    expect(within(finder()).getByRole('button', { name: 'Close' })).toBeEnabled()
  })

  it('says why a search failed', async () => {
    server.use(
      http.post('/api/v1/filament/tdfp/search', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'no profile-fetch worker runs', code: 'no_worker' }, { status: 503 }),
      ),
    )
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    await user.click(within(finder()).getByRole('button', { name: 'Search 3DFP' }))
    expect(await within(finder()).findByRole('alert')).toHaveTextContent('no profile-fetch worker runs')
  })

  it('offers no add for a type that already has a saved 3DFP id', async () => {
    await upsertType({ tdfp_id: 1234 }, T.JADE)
    const { user } = render(`/filament/types/${T.JADE}`)
    await code()
    await user.click(screen.getByRole('button', { name: 'Find on 3DFP' }))
    await user.click(within(finder()).getByRole('button', { name: 'Search 3DFP' }))
    await within(finder()).findByRole('listitem', { name: /3DFP 4411/ })
    expect(within(finder()).queryByRole('button', { name: 'Add the missing filament' })).not.toBeInTheDocument()
  })
})
