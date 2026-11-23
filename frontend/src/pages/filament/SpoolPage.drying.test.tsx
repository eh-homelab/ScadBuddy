import { screen, waitFor, within } from '@testing-library/react'
import { Route, Routes } from 'react-router'
import { describe, expect, it } from 'vitest'
import { getSpool } from '../../api/filament'
import { spoolId } from '../../mocks/features/filament/seed'
import { mockSettings, setMockSettings } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { SpoolPage } from './SpoolPage'

const open = (number: number) =>
  renderPage(
    <Routes>
      <Route path="/filament/spools/:id" element={<SpoolPage />} />
    </Routes>,
    { route: `/filament/spools/${spoolId(number)}` },
  )

describe('the spool page and drying', () => {
  it('shows the badge in the header and the hours outside dry storage in Storage', async () => {
    open(2)
    const header = await screen.findByRole('heading', { level: 1, name: /Spool #2\b/ })
    expect(within(header.parentElement!).getByText('Needs drying')).toBeInTheDocument()
    const storage = screen.getByRole('region', { name: 'Storage' })
    expect(within(storage).getByText('61.5 hours outside dry storage')).toBeInTheDocument()
  })

  it('fills the drying form from the type, else the material, and records what it shows (#6720)', async () => {
    const { user } = open(2)
    await screen.findByRole('heading', { level: 1, name: /Spool #2\b/ })
    const temp = await screen.findByLabelText('Drying temperature (°C)')
    await waitFor(() => expect(temp).toHaveValue('70'))
    expect(screen.getByLabelText('Hours')).toHaveValue('8')
    // A profile fill takes the AMS HT's values, which can be above an AMS 2 Pro's 65 °C (#6856).
    expect(screen.getByText("Filled from its type and material. A slicer profile gives the AMS HT's values; lower them when you dry in an AMS 2 Pro (65 °C at most).")).toBeInTheDocument()

    await user.clear(temp)
    await user.type(temp, '60')
    expect(screen.queryByText(/Filled from its/)).toBeNull()
    await user.click(screen.getByRole('button', { name: 'Record drying' }))
    await waitFor(async () => expect((await getSpool(spoolId(2))).last_dried_temp_c).toBe(60))
    expect((await getSpool(spoolId(2))).last_dried_hours).toMatch(/^8/)
    // Recorded, the form shows the defaults again.
    await waitFor(() => expect(screen.getByLabelText('Drying temperature (°C)')).toHaveValue('70'))
  })

  it("overrides its type's drying temperature, which the drying form then starts from (#6856)", async () => {
    const { user } = open(2)
    await screen.findByRole('heading', { level: 1, name: /Spool #2\b/ })
    const own = await screen.findByLabelText('Default drying temperature (°C)')
    expect(own).toHaveAttribute('placeholder', '70')
    await user.type(own, '55')
    await user.click(screen.getByRole('button', { name: 'Save spool' }))
    await waitFor(async () => expect((await getSpool(spoolId(2))).overridden).toContain('drying_temp_c'))
    expect((await getSpool(spoolId(2))).drying_defaults).toMatchObject({ temp_c: 55, temp_from: 'spool' })
    await waitFor(() => expect(screen.getByLabelText('Drying temperature (°C)')).toHaveValue('55'))
    expect(screen.getByText(/^Filled from this spool and its material\./)).toBeInTheDocument()
  })

  it('fills the drying form from the Settings defaults when the catalog sets none (#7156)', async () => {
    setMockSettings({ ...mockSettings(), filament_drying_temp_c: 45, filament_drying_hours: '4' })
    const { user } = open(10)
    await screen.findByRole('heading', { level: 1, name: /Spool #10\b/ })
    const temp = await screen.findByLabelText('Drying temperature (°C)')
    await waitFor(() => expect(temp).toHaveValue('45'))
    expect(screen.getByLabelText('Hours')).toHaveValue('4')
    expect(screen.getByText('Filled from the default in Settings.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Record drying' }))
    await waitFor(async () => expect((await getSpool(spoolId(10))).last_dried_temp_c).toBe(45))
  })

  it('offers no drying when neither the catalog nor Settings sets one (#7156)', async () => {
    open(10)
    await screen.findByRole('heading', { level: 1, name: /Spool #10\b/ })
    const temp = await screen.findByLabelText('Drying temperature (°C)')
    expect(temp).toHaveValue('')
    expect(screen.queryByText(/^Filled from/)).toBeNull()
  })

  it('shows neither the badge nor a warning for a spool that is fine', async () => {
    open(1)
    await screen.findByRole('heading', { level: 1, name: /Spool #1\b/ })
    expect(screen.queryByText('Needs drying')).toBeNull()
    expect(within(screen.getByRole('region', { name: 'Storage' })).getByText('No time outside dry storage')).toBeInTheDocument()
  })
})
