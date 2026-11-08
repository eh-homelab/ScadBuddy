import { screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { renderPage } from '../../test/utils'
import { MachineRates } from './MachineRates'

const LINKED = '00000000-0000-4000-8000-000000000001'
const GARAGE = '00000000-0000-4000-8000-0000000000aa'

// #4625 (order operations spec §6) — each printer's machine rate under Settings' Orders section.
describe('MachineRates (#4625)', () => {
  beforeEach(() => resetMockState())

  it("sets a printer's inputs, shows each part of the rate, and removes them", async () => {
    const { user } = renderPage(<MachineRates />)
    await user.click(await screen.findByRole('button', { name: 'Machine rate of Garage' }))
    const panel = await screen.findByRole('region', { name: 'Machine rate of Garage' })
    expect(await within(panel).findByTestId('machine-rate-book')).toHaveTextContent(/price book.s machine rate/)

    const form = within(panel).getByRole('form', { name: 'Machine rate inputs of Garage' })
    await user.type(within(form).getByLabelText('Purchase price'), '2400')
    await user.type(within(form).getByLabelText('Life (h)'), '5000')
    await user.type(within(form).getByLabelText('Watts'), '200')
    await user.type(within(form).getByLabelText('Price of a kWh'), '0.15')
    await user.type(within(form).getByLabelText('Labour per hour'), '20')
    await user.click(within(form).getByRole('button', { name: 'Add wear part' }))
    const wear = within(form).getByRole('group', { name: 'Wear part 1' })
    await user.type(within(wear).getByLabelText('Part'), 'Nozzle')
    await user.type(within(wear).getByLabelText('Cost'), '35')
    await user.type(within(wear).getByLabelText('Life (h)'), '1000')
    await user.click(within(form).getByRole('button', { name: 'Add task' }))
    const task = within(form).getByRole('group', { name: 'Maintenance task 1' })
    await user.type(within(task).getByLabelText('Task'), 'Lubricate rails')
    await user.type(within(task).getByLabelText('Minutes'), '15')
    await user.type(within(task).getByLabelText('Consumables'), '1')
    await user.type(within(task).getByLabelText('Every (h)'), '50')
    await user.click(within(form).getByRole('button', { name: 'Save machine rate' }))

    const breakdown = await within(panel).findByTestId('machine-rate-breakdown')
    expect(breakdown).toHaveTextContent('0.665 USD per hour')
    const parts = within(breakdown).getByRole('list', { name: 'Rate parts' })
    expect(parts).toHaveTextContent('Depreciation 0.48 per hour (2400 over 5000 h)')
    expect(parts).toHaveTextContent('Power 0.03 per hour (200 W at 0.15 per kWh)')
    expect(parts).toHaveTextContent('Nozzle: 35 over 1000 h = 0.035')
    expect(parts).toHaveTextContent('Lubricate rails, every 50 h: 0.12')
    expect((await api.machineRate(GARAGE)).inputs?.maintenance?.[0]).toMatchObject({ task: 'Lubricate rails', interval_hours: '50' })

    await user.click(within(panel).getByRole('button', { name: 'Remove inputs' }))
    await waitFor(async () => expect((await api.machineRate(GARAGE)).inputs).toBeNull())
    expect(await within(panel).findByTestId('machine-rate-book')).toBeInTheDocument()
  })

  it("takes a linked printer's intervals from Bambuddy and says which tasks are not counted", async () => {
    await api.setMachineRate(LINKED, { profile: { labour_rate_per_hour: '30' }, wear_parts: [], maintenance: [{ task: 'Lubricate rails', minutes: '20', consumables: '2' }] })
    const { user } = renderPage(<MachineRates />)
    await user.click(await screen.findByRole('button', { name: 'Machine rate of 3DP-31B-598' }))
    const panel = await screen.findByRole('region', { name: 'Machine rate of 3DP-31B-598' })
    const breakdown = await within(panel).findByTestId('machine-rate-breakdown')
    expect(breakdown).toHaveTextContent('read from Bambuddy')
    expect(breakdown).toHaveTextContent('Lubricate rails, every 50 h: 0.24')
    expect(breakdown).toHaveTextContent('Check belts, every 30 days: a calendar task, in days: not counted')
    // Its interval is Bambuddy's: none is typed here.
    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    expect(within(task).queryByLabelText('Every (h)')).toBeNull()
  })

  it('says what to fix before it saves', async () => {
    const { user } = renderPage(<MachineRates />)
    await user.click(await screen.findByRole('button', { name: 'Machine rate of Garage' }))
    const form = await screen.findByRole('form', { name: 'Machine rate inputs of Garage' })
    await user.type(within(form).getByLabelText('Watts'), 'lots')
    await user.click(within(form).getByRole('button', { name: 'Save machine rate' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Enter the watts as a number')
    expect((await api.machineRate(GARAGE)).inputs).toBeNull()
  })
})
