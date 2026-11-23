import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { Route, Routes } from 'react-router'
import { describe, expect, it, onTestFinished, vi } from 'vitest'
import { getSpool, getType, listSpoolEvents, listSpools, patchSpool, setSpoolKValues, setSpoolPresets } from '../../api/filament'
import { H2C, L, spoolId, T, TAG } from '../../mocks/features/filament/seed'
import { INVOICE, LINE, SUPPLIER } from '../../mocks/features/filamentPurchasingSeed'
import { ORDER_CLOSED } from '../../mocks/features/orders/state'
import { mockSettings, setMockSettings } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { SpoolPage } from './SpoolPage'
import { SpoolsTab } from './SpoolsTab'

function render(route: string) {
  return renderPage(
    <Routes>
      <Route path="/filament/spools/:id" element={<SpoolPage />} />
      <Route path="/filament/spools" element={<p>The spools tab</p>} />
      <Route path="*" element={<p>Elsewhere</p>} />
    </Routes>,
    { route },
  )
}

const open = (number: number) => render(`/filament/spools/${spoolId(number)}`)
const heading = (number: number) => screen.findByRole('heading', { level: 1, name: new RegExp(`Spool #${number}\\b`) })

/** The bodies of every request to `path` with `method`, read as they leave. */
function recorded(method: string, match: RegExp) {
  const bodies: unknown[] = []
  server.events.on('request:start', ({ request }) => {
    if (request.method === method && match.test(new URL(request.url).pathname)) void request.clone().json().then((body) => bodies.push(body), () => {})
  })
  return bodies
}

describe('the spool page', () => {
  it('has the seven sections, each a landmark with a heading, and an index', async () => {
    open(1)
    await heading(1)
    for (const name of ['Identity', 'Weight', 'Colour and cost', 'Printers', 'Storage', 'Files and links', 'History']) {
      expect(screen.getByRole('region', { name })).toBeInTheDocument()
      expect(screen.getByRole('heading', { level: 2, name })).toBeInTheDocument()
      expect(within(screen.getByRole('navigation', { name: 'Sections' })).getByRole('link', { name })).toHaveAttribute('href', `#${name.toLowerCase().replace(/ /g, '-')}`)
    }
    expect(screen.getByRole('combobox', { name: 'Go to section' })).toBeInTheDocument()
  })

  it('offers the SpoolBuddy scale tare beside the tag panels (#7155)', async () => {
    open(1)
    await heading(1)
    const identity = screen.getByRole('region', { name: 'Identity' })
    expect(within(identity).getByRole('heading', { level: 3, name: 'Scale' })).toBeInTheDocument()
    expect(await within(identity).findByRole('button', { name: 'Tare scale spoolbuddy-bench' })).toBeEnabled()
  })

  it('resets an overridden colour with one PATCH of null, and the mark stays until the answer', async () => {
    await patchSpool(spoolId(3), { rgba: '#112233FF' })
    const edited = (await listSpoolEvents(spoolId(3))).filter((event) => event.kind === 'edited').length
    const patches = recorded('PATCH', /\/spools\//)
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    // Holds the answer; a resolver that returns nothing falls through to the mock.
    server.use(http.patch('*/api/v1/filament/spools/:id', async () => { await gate }))
    const { user } = open(3)
    await heading(3)
    const field = () => document.querySelector('[data-field="rgba"]') as HTMLElement
    expect(within(field()).getByText('Set on this spool')).toBeInTheDocument()
    await user.click(within(field()).getByRole('button', { name: 'Reset Colour to type' }))
    await waitFor(() => expect(patches).toEqual([{ rgba: null }]))
    expect(within(field()).getByText('Set on this spool')).toBeInTheDocument()
    release()
    await waitFor(() => expect(within(field()).queryByText('Set on this spool')).toBeNull())
    expect((await getSpool(spoolId(3))).overridden).not.toContain('rgba')
    expect((await getSpool(spoolId(3))).effective.rgba).toBe('#F5547CFF')
    expect((await listSpoolEvents(spoolId(3))).filter((event) => event.kind === 'edited')).toHaveLength(edited + 1)
  })

  it('keeps the mark and shows the refusal when the reset is refused', async () => {
    await patchSpool(spoolId(3), { rgba: '#112233FF' })
    server.use(
      http.patch('*/api/v1/filament/spools/:id', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'the spool changed under you' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    const { user } = open(3)
    await heading(3)
    const field = () => document.querySelector('[data-field="rgba"]') as HTMLElement
    await user.click(within(field()).getByRole('button', { name: 'Reset Colour to type' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('the spool changed under you')
    expect(within(field()).getByText('Set on this spool')).toBeInTheDocument()
    expect((await getSpool(spoolId(3))).overridden).toContain('rgba')
  })

  it('restores an archived spool and nothing else, then offers the last tag, held by spool #3', async () => {
    const { user } = open(19)
    await heading(19)
    await user.click(screen.getByRole('button', { name: 'Restore' }))
    const move = await screen.findByRole('button', { name: 'Tag is on spool #3 — Move it here' })
    expect((await getSpool(spoolId(19))).archived_at).toBeNull()
    expect((await getSpool(spoolId(19))).tag).toBeNull()
    expect((await getSpool(spoolId(3))).tag?.id).toBe(TAG.MOVED)
    await user.click(move)
    await waitFor(async () => expect((await getSpool(spoolId(19))).tag?.id).toBe(TAG.MOVED))
    expect((await getSpool(spoolId(3))).tag).toBeNull()
    expect(await screen.findByRole('button', { name: 'Take off' })).toBeInTheDocument()
  })

  it('asks for a material when it is Unknown, tied to the material select, which stays closed', async () => {
    open(7)
    await heading(7)
    const prompt = screen.getByText('Choose a material')
    const material = screen.getByRole('combobox', { name: 'Material' })
    expect(material).toHaveAttribute('aria-invalid', 'true')
    expect(material).toHaveAttribute('aria-describedby', prompt.id)
    expect(prompt.id).not.toBe('')
    expect(screen.queryByRole('listbox')).toBeNull()
  })

  it('has no material prompt once a material is chosen', async () => {
    open(1)
    await heading(1)
    expect(screen.queryByText('Choose a material')).toBeNull()
    expect(screen.getByRole('combobox', { name: 'Material' })).not.toHaveAttribute('aria-invalid', 'true')
  })

  it('offers no Delete for a spool that printed, only Archive and why', async () => {
    open(4)
    await heading(4)
    expect(screen.queryByRole('button', { name: 'Delete' })).toBeNull()
    expect(screen.getByRole('button', { name: 'Archive' })).toBeInTheDocument()
    expect(screen.getByText('It has prints, so job costs keep their source.')).toBeInTheDocument()
  })

  it('deletes a spool that never printed, after asking', async () => {
    const { user } = open(11)
    await heading(11)
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete spool' }))
    expect(await screen.findByText('The spools tab')).toBeInTheDocument()
    expect((await listSpools({ includeArchived: true })).some((spool) => spool.number === 11)).toBe(false)
  })

  it('closes the delete dialog and shows the error when the delete is refused', async () => {
    server.use(
      http.delete('*/api/v1/filament/spools/:id', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail: 'the spool has prints in its ledger; archive it instead' }, { status: 409, headers: { 'Content-Type': 'application/problem+json' } }),
      ),
    )
    const { user } = open(11)
    await heading(11)
    await user.click(screen.getByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete spool' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('archive it instead')
    expect(screen.queryByRole('alertdialog')).toBeNull()
  })

  it('shows the type preset and K value as From type, and overrides a preset with one PUT', async () => {
    const { user } = open(1)
    await heading(1)
    const presets = screen.getByRole('table', { name: 'Slicer presets' })
    const row = within(presets).getByRole('row', { name: /H2C 0\.4 from type/i })
    expect(within(row).getByText('From type')).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: 'K values' })).getByText('From type')).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Override' }))
    const own = within(presets).getByLabelText('Slicer filament')
    await user.clear(own)
    await user.type(own, 'GFA01')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    await waitFor(async () => expect((await getSpool(spoolId(1))).presets[0]).toMatchObject({ slicer_filament: 'GFA01', overridden: true }))
  })

  it('saves only the changed fields in one PATCH, and the effective value stays the type’s for the rest', async () => {
    const patches = recorded('PATCH', /\/spools\//)
    const { user } = open(1)
    const cost = await screen.findByLabelText('Cost per kg')
    expect(cost).toHaveAttribute('placeholder', '15.91')
    await user.type(cost, '17.5')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(patches).toEqual([{ cost_per_kg: '17.5' }])
    expect((await getSpool(spoolId(1))).overridden).toEqual(['cost_per_kg'])
  })

  it('moves the spool through the move route when its location changes', async () => {
    const patches = recorded('PATCH', /\/spools\//)
    const { user } = open(1)
    await heading(1)
    await choose(user, screen.getByRole('combobox', { name: 'Location' }), /Dry box 1/)
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    await waitFor(async () => expect((await getSpool(spoolId(1))).location_id).toBe(L.DRY_BOX))
    expect(patches).toEqual([])
  })

  it('records a weighing from the scale reading, less the empty spool', async () => {
    const { user } = open(1)
    await heading(1)
    expect(screen.getByText(/Empty spool/)).toHaveTextContent('250 g')
    const posts = recorded('POST', /\/events$/)
    await user.type(screen.getByLabelText('Scale reading, with the spool (g)'), '900')
    await user.click(screen.getByRole('button', { name: 'Record weighing' }))
    await waitFor(() => expect(posts).toEqual([{ kind: 'weighed', grams: 650, source: 'scale' }]))
    await waitFor(async () => expect((await getSpool(spoolId(1))).grams_left).toBe(650))
  })

  it('reads the ledger oldest first, and the history lists the newest first', async () => {
    open(1)
    await heading(1)
    const ledger = await listSpoolEvents(spoolId(1))
    expect(ledger.map((event) => event.at)).toEqual([...ledger.map((event) => event.at)].sort())
    expect(ledger.length).toBeGreaterThan(1)
    const history = await screen.findByRole('list', { name: 'History of this spool' })
    expect(within(history).getAllByRole('listitem')[0]).toHaveTextContent('Weighed')
    expect(within(history).getAllByRole('listitem').at(-1)).toHaveTextContent('Added')
  })

  it('refuses a drying temperature that is not a whole number, and records a valid one', async () => {
    const posts = recorded('POST', /\/events$/)
    const { user } = open(1)
    await heading(1)
    await user.type(screen.getByLabelText('Drying temperature (°C)'), 'abc')
    await user.click(screen.getByRole('button', { name: 'Record drying' }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/drying temperature/i)
    expect(posts).toEqual([])
    await user.clear(screen.getByLabelText('Drying temperature (°C)'))
    await user.type(screen.getByLabelText('Drying temperature (°C)'), '55')
    await user.click(screen.getByRole('button', { name: 'Record drying' }))
    // The hours come from the material's drying default (#6720).
    await waitFor(() => expect(posts).toEqual([{ kind: 'dried', temp_c: 55, hours: '8' }]))
  })

  it('names the colour group by its label', async () => {
    open(1)
    await heading(1)
    expect(screen.getByRole('group', { name: 'Colour' })).toContainElement(screen.getByLabelText('Colour (hex)'))
  })

  it('overrides the colour and the extra colours with a catalog colour (#6912)', async () => {
    const patches = recorded('PATCH', /\/spools\//)
    const { user } = open(1)
    await heading(1)
    await user.click(screen.getByRole('button', { name: 'From the catalog' }))
    await user.type(screen.getByRole('combobox', { name: 'Search the colour catalog' }), 'galaxy')
    await user.click(await screen.findByRole('option', { name: /Galaxy Duo/ }))
    expect(screen.getByLabelText('Extra colours')).toHaveValue('#2E8B57FF')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    expect(await screen.findByText('Saved')).toBeInTheDocument()
    expect(patches).toEqual([{ rgba: '#1B3A8CFF', extra_colours: ['#2E8B57FF'] }])
  })

  it('jumps to the same section twice from the select', async () => {
    const scroll = vi.fn()
    Element.prototype.scrollIntoView = scroll
    const { user } = open(1)
    await heading(1)
    await choose(user, screen.getByRole('combobox', { name: 'Go to section' }), 'Weight')
    await choose(user, screen.getByRole('combobox', { name: 'Go to section' }), 'Weight')
    // The select's own list scrolls too: count the jumps to the section.
    expect(scroll.mock.contexts.filter((el) => (el as Element).id === 'weight')).toHaveLength(2)
  })

  it('keeps the marks the saved spool has while the type in the form changes', async () => {
    await patchSpool(spoolId(3), { cost_per_kg: '20' })
    const { user } = open(3)
    await heading(3)
    const mark = () => within(document.querySelector('[data-field="cost_per_kg"]') as HTMLElement).queryByText('Set on this spool')
    expect(mark()).toBeInTheDocument()
    await choose(user, screen.getByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-BLACK/)
    expect(mark()).toBeInTheDocument()
  })

  it('loads the history of a spool whose later writes failed after it was made', async () => {
    server.use(http.put('*/api/v1/filament/spools/:id/presets', () => HttpResponse.json({ type: 'about:blank', title: 'Unprocessable Content', status: 422, detail: 'two rows name the same printer and nozzle' }, { status: 422, headers: { 'Content-Type': 'application/problem+json' } })))
    const { user } = render('/filament/spools/new')
    await choose(user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-HOT-PINK/)
    await user.click(screen.getByRole('button', { name: 'Add preset' }))
    const row = screen.getByRole('row', { name: 'New preset' })
    await user.type(within(row).getByLabelText('Printer model'), 'H2C')
    await user.type(within(row).getByLabelText('Slicer filament'), 'GFA00')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('two rows name')
    const history = await screen.findByRole('list', { name: 'History of this spool' })
    expect(within(history).getByText('Added')).toBeInTheDocument()
  })

  it('shows what the grams left are made of', async () => {
    open(4)
    await heading(4)
    const basis = screen.getByRole('list', { name: 'How the grams left add up' })
    expect(within(basis).getAllByRole('listitem').length).toBeGreaterThan(0)
    expect(basis).toHaveTextContent('148')
  })

  it('locks the type picker while the presets of a spool that needs a type are edited', async () => {
    const { user } = open(6)
    await heading(6)
    expect(screen.getByRole('combobox', { name: 'Type' })).toBeEnabled()
    const filament = within(screen.getByRole('table', { name: 'Slicer presets' })).getByLabelText('Slicer filament')
    await user.type(filament, 'X')
    expect(screen.getByRole('combobox', { name: 'Type' })).toBeDisabled()
    expect(screen.getByText('Save or discard your preset changes before choosing a type')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Discard changes' }))
    expect(screen.getByRole('combobox', { name: 'Type' })).toBeEnabled()
  })

  it('takes the first type of a spool that needs one: the migrated rows are marked dropped, and the save writes no presets', async () => {
    const puts = recorded('PUT', /\/spools\/.*\/presets$/)
    const { user } = open(6)
    await heading(6)
    await choose(user, screen.getByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    const table = screen.getByRole('table', { name: 'Slicer presets' })
    expect(within(table).getByRole('row', { name: 'H2C 0.4 Dropped when the type is saved' })).toBeInTheDocument()
    expect(screen.getByText(/Saving the type drops H2C 0\.4,/)).toBeInTheDocument()
    expect(within(table).queryByLabelText('Slicer filament')).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    const presets = screen.getByRole('table', { name: 'Slicer presets' })
    expect(await within(presets).findByRole('row', { name: /H2C 0\.4 from type/i })).toBeInTheDocument()
    expect(within(presets).queryByLabelText('Slicer filament')).toBeNull()
    expect(puts).toEqual([])
    expect((await getSpool(spoolId(6))).type_id).toBe(T.JADE)
    expect((await getSpool(spoolId(6))).presets.every((row) => !row.overridden)).toBe(true)
  })

  it('keeps a preset the person saved when the first type drops the migrated ones', async () => {
    // The migrated 0.4 row goes back unchanged (it stays migrated); the 0.6 row is the person's.
    const own = (await getSpool(spoolId(6))).presets.map(({ overridden: _o, migrated: _m, ...row }) => row)
    await setSpoolPresets(spoolId(6), [...own, { printer_model: 'H2C', nozzle_diameter: '0.6', slicer_filament: 'GFX01', slicer_filament_name: null }])
    const { user } = open(6)
    await heading(6)
    await choose(user, screen.getByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    const table = screen.getByRole('table', { name: 'Slicer presets' })
    expect(within(table).getByRole('row', { name: 'H2C 0.6 Kept' })).toBeInTheDocument()
    expect(within(table).getByRole('row', { name: 'H2C 0.4 Dropped when the type is saved' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    await waitFor(async () => expect((await getSpool(spoolId(6))).type_id).toBe(T.JADE))
    const presets = (await getSpool(spoolId(6))).presets
    expect(presets).toContainEqual(expect.objectContaining({ nozzle_diameter: '0.6', slicer_filament: 'GFX01', overridden: true }))
    expect(presets).toContainEqual(expect.objectContaining({ nozzle_diameter: '0.4', slicer_filament: 'GFA00', overridden: false }))
  })

  it('shows no Reset or Remove on an own preset row until the type has loaded', async () => {
    await setSpoolPresets(spoolId(1), [{ printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA09', slicer_filament_name: null }])
    let release!: () => void
    const gate = new Promise<void>((resolve) => {
      release = resolve
    })
    server.use(http.get('*/api/v1/filament/types/:id', async () => { await gate }))
    open(1)
    const table = await screen.findByRole('table', { name: 'Slicer presets' })
    expect(await within(table).findByDisplayValue('GFA09')).toBeInTheDocument()
    expect(within(table).queryByRole('button', { name: /to type|^Remove/ })).toBeNull()
    release()
    expect(await within(table).findByRole('button', { name: 'Reset preset 1 to type' })).toBeInTheDocument()
  })

  it('says so when the type could not be loaded, and offers no Reset or Remove', async () => {
    await setSpoolPresets(spoolId(1), [{ printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA09', slicer_filament_name: null }])
    server.use(http.get('*/api/v1/filament/types/:id', () => HttpResponse.json({ type: 'about:blank', title: 'Not Found', status: 404, detail: 'no spool type gone' }, { status: 404, headers: { 'Content-Type': 'application/problem+json' } })))
    open(1)
    expect(await screen.findByText(/The type's presets and K values could not be loaded: no spool type gone/)).toBeInTheDocument()
    expect(within(screen.getByRole('table', { name: 'Slicer presets' })).queryByRole('button', { name: /to type|^Remove/ })).toBeNull()
  })

  it('offers Reset to type only for a row the type has, and Remove for one it lacks', async () => {
    const { user } = open(1)
    await heading(1)
    const table = screen.getByRole('table', { name: 'Slicer presets' })
    await user.click(within(table).getByRole('button', { name: 'Override' }))
    await user.click(screen.getByRole('button', { name: 'Add preset' }))
    expect(within(table).getByRole('button', { name: 'Reset preset 1 to type' })).toBeInTheDocument()
    expect(within(table).queryByRole('button', { name: /Reset preset 2/ })).toBeNull()
    expect(within(table).getByRole('button', { name: /^Remove preset/ })).toBeInTheDocument()
  })

  it('makes a type from a spool that needs one, and attaches it', async () => {
    const puts = recorded('PUT', /\/types\/.*\/presets$/)
    const { user } = open(6)
    await heading(6)
    await user.click(screen.getByRole('button', { name: 'Create type from this spool' }))
    await waitFor(async () => expect((await getSpool(spoolId(6))).type_id).not.toBeNull())
    const spool = await getSpool(spoolId(6))
    const type = await getType(spool.type_id!)
    expect(type).toMatchObject({ colour_name: 'Teal', rgba: '#1B9AAAFF' })
    expect(type.presets).toHaveLength(1)
    // Only the four fields of a preset: the server refuses any other key (`extra="forbid"`).
    expect(puts).toEqual([[{ printer_model: 'H2C', nozzle_diameter: '0.4', slicer_filament: 'GFA00', slicer_filament_name: 'Bambu PLA Basic @BBL H2C' }]])
    expect(spool.overridden).toEqual([])
  })

  it('sends the eight fields of each own K value to the new type', async () => {
    await setSpoolKValues(spoolId(6), [{ printer_id: H2C.printer_id, extruder: 0, nozzle_diameter: '0.4', nozzle_type: 'hardened_steel', k_value: '0.031', name: 'Teal K', cali_idx: 3, setting_id: 'S1' }])
    const puts = recorded('PUT', /\/types\/.*\/k-values$/)
    const { user } = open(6)
    await heading(6)
    await user.click(screen.getByRole('button', { name: 'Create type from this spool' }))
    await waitFor(() => expect(puts).toHaveLength(1))
    expect(puts[0]).toEqual([{ printer_id: H2C.printer_id, extruder: 0, nozzle_diameter: '0.4', nozzle_type: 'hardened_steel', k_value: '0.031', name: 'Teal K', cali_idx: 3, setting_id: 'S1' }])
    await waitFor(async () => expect((await getSpool(spoolId(6))).k_values.filter((row) => row.overridden)).toEqual([]))
  })

  it('offers no printer tray as a place to move a spool to', async () => {
    const { user } = open(1)
    await heading(1)
    await user.click(screen.getByRole('combobox', { name: 'Location' }))
    expect(await screen.findByRole('option', { name: /Dry box 1/ })).toBeInTheDocument()
    expect(screen.queryByRole('option', { name: /AMS/ })).toBeNull()
  })

  it('keeps the list filters in its back link, and says when a reload after a tag change fails', async () => {
    const { user } = renderPage(
      <Routes>
        <Route path="/filament/spools/:id" element={<SpoolPage />} />
        <Route path="/filament/spools" element={<SpoolsTab />} />
      </Routes>,
      { route: '/filament/spools?status=all' },
    )
    await user.click(await screen.findByRole('link', { name: '#2' }))
    await heading(2)
    expect(screen.getByRole('link', { name: /^Spools$/ })).toHaveAttribute('href', '/filament/spools?status=all')
    server.use(http.get('*/api/v1/filament/spools/:id', () => HttpResponse.json({ type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'the spools are down' }, { status: 503, headers: { 'Content-Type': 'application/problem+json' } })))
    await user.type(screen.getByLabelText('Tag UID'), '04 AA BB CC')
    await user.click(screen.getByRole('button', { name: 'Bind tag' }))
    expect(await screen.findByText(/the spools are down/)).toBeInTheDocument()
  })

  it('makes a new spool with one POST and opens it', async () => {
    const posts = recorded('POST', /\/filament\/spools$/)
    const { user } = render('/filament/spools/new')
    await choose(user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-HOT-PINK/)
    await user.type(screen.getByLabelText('Grams left'), '900')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    await waitFor(async () => expect((await listSpools()).length).toBe(12))
    expect(posts).toHaveLength(1)
    expect(await heading(20)).toBeInTheDocument()
  })

  it('shows a server refusal and keeps the edits', async () => {
    const { user } = open(1)
    const temp = await screen.findByLabelText('Nozzle temperature, min (°C)')
    await user.type(temp, '300')
    await user.type(screen.getByLabelText('Nozzle temperature, max (°C)'), '200')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    expect(await screen.findByRole('alert')).toBeInTheDocument()
    expect(temp).toHaveValue('300')
  })

  it("links the invoice line the spool was bought on and its supplier, and lists the type's suppliers (#6869)", async () => {
    open(1)
    await heading(1)
    const link = await screen.findByRole('link', { name: 'Invoice US-441907' })
    expect(link).toHaveAttribute('href', `/filament/invoices/${INVOICE.CONFIRMED_A}#line-${LINE.CONFIRMED_A1}`)
    expect(link.parentElement).toHaveTextContent(/from Bambu Lab \(line 1/)
    expect(within(link.parentElement!).getByRole('link', { name: 'Bambu Lab' })).toHaveAttribute('href', `/filament/suppliers/${SUPPLIER.BAMBU}`)
    const purchasing = screen.getByText('Purchasing').parentElement!
    expect(purchasing).toHaveTextContent('Bambu Lab')
    expect(purchasing).toHaveTextContent('A00-W1')
  })

  it('shows no Invoice row for a spool with no invoice', async () => {
    open(2)
    await heading(2)
    expect(screen.queryByRole('link', { name: /^Invoice / })).not.toBeInTheDocument()
  })
})

describe('the spool page\'s history (#6269)', () => {
  it('shows the grams chart, the metrics and the timeline from the history route', async () => {
    // The numbers `metrics.series` and `spool_metrics` give for #2's seed ledger at this time
    // (run with `uv run --frozen python`; Task 7 fix 1 report): 1000 g to 820 g with 3 anchors,
    // 120 g over the 17.1 days since #2 was created is 7.0 g a day, and 820 g lasts to 2027-03-15.
    vi.useFakeTimers({ toFake: ['Date'] }).setSystemTime('2026-11-18T12:00:00Z')
    onTestFinished(() => void vi.useRealTimers())
    open(2)
    await heading(2)
    const history = screen.getByRole('region', { name: 'History' })
    expect(await within(history).findByRole('img', { name: 'Grams left: 1000 g to 820 g, 3 anchor readings' })).toBeInTheDocument()
    expect(history.querySelectorAll('[data-plate]')).toHaveLength(3)
    const metrics = within(history).getByLabelText('Spool metrics')
    for (const name of ['Burn rate', 'Empty around', 'Waste', 'AMS error']) expect(within(metrics).getByText(name)).toBeInTheDocument()
    expect(within(metrics).getByText('Burn rate').parentElement).toHaveTextContent('7 g a day')
    expect(within(metrics).getByText('Burn rate').parentElement).toHaveTextContent('Since it was added 17 days ago')
    expect(within(metrics).getByText('Empty around').parentElement).toHaveTextContent(new Date('2027-03-15T00:00:00Z').toLocaleDateString(undefined, { dateStyle: 'medium', timeZone: 'UTC' }))
    expect(within(metrics).getByText('30 g')).toBeInTheDocument()
    expect(within(metrics).getByText('±20 g')).toBeInTheDocument()
    const timeline = within(history).getByRole('list', { name: 'History of this spool' })
    expect(within(timeline).getByRole('link', { name: 'Print 41' })).toHaveAttribute('href', '/prints/41')
    expect(within(timeline).getByRole('link', { name: 'Its order' })).toHaveAttribute('href', `/orders/${ORDER_CLOSED}`)
    expect(within(timeline).getByText('Cost per kg: not set → 25.00')).toBeInTheDocument()
    expect(within(timeline).getByText('Assistant')).toBeInTheDocument()
    // A BB plate is booked as the API books it: its grams off, measured, and no grams left.
    expect(within(timeline).getByText(', −55 g, measured, AMS A1')).toBeInTheDocument()
  })

  it('says why the burn rate is empty, with the window the settings hold', async () => {
    setMockSettings({ ...mockSettings(), filament_burn_window_days: 14 })
    open(1)
    await heading(1)
    const metrics = await within(screen.getByRole('region', { name: 'History' })).findByLabelText('Spool metrics')
    const burn = within(metrics).getByText('Burn rate').parentElement!
    expect(burn).toHaveTextContent('—')
    expect(burn).toHaveTextContent('No prints in the last 14 days')
  })
})
