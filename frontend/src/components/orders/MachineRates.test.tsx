import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../api/client'
import { resetMockState } from '../../mocks/handlers'
import { server } from '../../mocks/server'
import { importBambuddyEvent, machineRateEvents, setBambuddyHours, setBambuddyLogAnswers, setEventsReadAt, setLanCount } from '../../mocks/features/orders/machineRates'
import { formatLocalTime } from '../../lib/format'
import { clearToasts, getToasts } from '../../lib/toast'
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
    await api.setMachineRate(LINKED, {
      profile: { labour_rate_per_hour: '30' },
      wear_parts: [],
      maintenance: [
        { task: 'Lubricate rails', minutes: '20', consumables: '2' },
        { task: 'Wipe the camera', minutes: '1', consumables: '0' },
      ],
    })
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
    // The names offered are Bambuddy's own, never a task it does not track.
    const offered = [...document.querySelectorAll(`#bambuddy-tasks-${LINKED} option`)].map((o) => o.getAttribute('value'))
    expect(offered).toEqual(['Lubricate rails', 'Clean build plate', 'Check belts'])
  })

  it("offers Bambuddy's task names for a linked printer with no inputs yet", async () => {
    const { user } = renderPage(<MachineRates />)
    await user.click(await screen.findByRole('button', { name: 'Machine rate of 3DP-31B-598' }))
    const panel = await screen.findByRole('region', { name: 'Machine rate of 3DP-31B-598' })
    expect(await within(panel).findByTestId('machine-rate-book')).toBeInTheDocument()
    const offered = [...document.querySelectorAll(`#bambuddy-tasks-${LINKED} option`)].map((o) => o.getAttribute('value'))
    expect(offered).toEqual(['Lubricate rails', 'Clean build plate', 'Check belts'])
  })

  it('refuses more decimal places than a field keeps', async () => {
    const { user } = renderPage(<MachineRates />)
    await user.click(await screen.findByRole('button', { name: 'Machine rate of Garage' }))
    const form = await screen.findByRole('form', { name: 'Machine rate inputs of Garage' })
    await user.type(within(form).getByLabelText('Watts'), '200.25')
    await user.click(within(form).getByRole('button', { name: 'Save machine rate' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Enter the watts as a number with at most 1 decimal place')
    await user.clear(within(form).getByLabelText('Watts'))
    await user.type(within(form).getByLabelText('Watts'), '200.5')
    await user.type(within(form).getByLabelText('Price of a kWh'), '0.12345')
    await user.click(within(form).getByRole('button', { name: 'Save machine rate' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Enter the price of a kWh as a number with at most 4 decimal places')
    await user.clear(within(form).getByLabelText('Price of a kWh'))
    await user.type(within(form).getByLabelText('Price of a kWh'), '0.1234')
    await user.click(within(form).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(async () => expect((await api.machineRate(GARAGE)).inputs?.profile).toMatchObject({ watts: '200.5', kwh_price: '0.1234' }))
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

// #5774 (machine-rate actuals spec 2026-11-17 §9) — logged events, actuals and confidence.
describe('MachineRates actuals (#5774)', () => {
  beforeEach(() => {
    resetMockState()
    clearToasts()
  })

  const nozzle = (window = 3, pricedBy: 'estimate' | 'actual' = 'estimate') =>
    api.setMachineRate(GARAGE, {
      profile: { labour_rate_per_hour: '30', actuals_window: window },
      wear_parts: [{ name: 'Nozzle', cost: '35', life_hours: '1000', priced_by: pricedBy }],
      maintenance: [{ task: 'Clean plate', minutes: '10', consumables: '0', interval_hours: '25' }],
    })
  const replace = (hours: string, at: string, cost = '35') => api.addMachineRateEvent(GARAGE, { kind: 'part', name: 'Nozzle', hours_at: hours, cost, at })
  const clean = (hours: string, at: string) => api.addMachineRateEvent(GARAGE, { kind: 'task', name: 'Clean plate', hours_at: hours, at })

  async function open(user: ReturnType<typeof renderPage>['user'], name: string) {
    await user.click(await screen.findByRole('button', { name: `Machine rate of ${name}` }))
    const panel = await screen.findByRole('region', { name: `Machine rate of ${name}` })
    await within(panel).findByTestId('machine-rate-breakdown')
    return panel
  }

  it('shows the estimate and the actual side by side, and the toggle chooses which prices', async () => {
    await nozzle(1)
    await replace('100', '2026-10-01T12:00:00Z')
    await replace('700', '2026-11-01T12:00:00Z')
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    const estimate = within(wear).getByTestId('figure-estimate')
    const actual = within(wear).getByTestId('figure-actual')
    expect(estimate).toHaveTextContent('Estimate 0.035/h')
    expect(actual).toHaveTextContent('Actual 0.0583/h')
    expect(estimate).toHaveAttribute('data-prices', 'true')
    expect(actual).toHaveAttribute('data-prices', 'false')
    expect(within(wear).getByTestId('confidence')).toHaveTextContent('100 % confidence: 1 of 1 replacements logged, 600 h each on average. The estimate is chosen.')

    await user.click(within(wear).getByRole('radio', { name: 'Actual' }))
    expect(within(wear).getByRole('radio', { name: 'Actual' })).toHaveAttribute('aria-checked', 'true')
    expect(within(wear).getByTestId('figure-actual')).toHaveAttribute('data-prices', 'true')
    expect(within(wear).getByTestId('figure-estimate')).toHaveAttribute('data-prices', 'false')
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(async () => expect((await api.machineRate(GARAGE)).breakdown.wear?.[0]).toMatchObject({ priced_by: 'actual', basis: 'actual', per_hour: '0.0583' }))
    expect(await within(panel).findByText(/Nozzle: 0.0583 per hour from logged replacements \(actual, 100 % confidence\)/)).toBeInTheDocument()
    expect(within(panel).getByTestId('machine-rate-breakdown')).toHaveTextContent('Some parts priced from actuals; lowest confidence 100 %')
  })

  it('says the estimate prices while an actual has fewer than N events', async () => {
    await nozzle(3, 'actual')
    await replace('100', '2026-10-01T12:00:00Z')
    await replace('700', '2026-11-01T12:00:00Z')
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    expect(within(wear).getByTestId('figure-estimate')).toHaveAttribute('data-prices', 'true')
    expect(within(wear).getByTestId('figure-actual')).toHaveAttribute('data-prices', 'false')
    expect(within(wear).getByTestId('confidence')).toHaveTextContent('Estimate prices, 33 % confidence: 1 of 3 replacements logged')
    expect(within(panel).getByRole('list', { name: 'Rate parts' })).toHaveTextContent('Nozzle: 35 over 1000 h = 0.035 (estimate, 33 % confidence)')
  })

  it('shows a row with no events at 0 % confidence, and a task by its gaps', async () => {
    await nozzle(2)
    await clean('10', '2026-10-01T12:00:00Z')
    await clean('45', '2026-10-05T12:00:00Z')
    await clean('83', '2026-10-09T12:00:00Z')
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    expect(within(wear).getByTestId('figure-actual')).toHaveTextContent('No events yet')
    expect(within(wear).getByTestId('confidence')).toHaveTextContent('0 % confidence: 0 of 2 replacements logged')
    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    // 2 x 5.00 over 35 + 38 h.
    expect(within(task).getByTestId('figure-actual')).toHaveTextContent('Actual 0.137/h')
    expect(within(task).getByTestId('confidence')).toHaveTextContent(
      '100 % confidence: 2 of 2 gaps logged, done every 36.5 h on average; some use the typed minutes or consumables',
    )
  })

  it('lists the events with their counter, gap and why a gap was skipped', async () => {
    await nozzle(3)
    await clean('10', '2026-10-01T12:00:00Z')
    await clean('10', '2026-10-02T12:00:00Z')
    await api.linkPrinterLan(GARAGE, { host: '10.0.0.5', access_code: '12345678' })
    setLanCount(GARAGE, { run_hours: '40' })
    await clean('40', '2026-10-05T12:00:00Z')
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    await user.click(within(task).getByRole('button', { name: 'Done (3)' }))
    const items = within(within(task).getByRole('list', { name: 'Done: Clean plate' })).getAllByRole('listitem') as [HTMLElement, HTMLElement, HTMLElement]
    expect(items).toHaveLength(3)
    expect(items[0]).toHaveTextContent('40 h, ScadBuddy count')
    expect(items[0]).toHaveTextContent('Skipped: counter changed')
    expect(items[0]).toHaveTextContent('logged here')
    expect(items[1]).toHaveTextContent('10 h, typed')
    expect(items[1]).toHaveTextContent('Skipped: no hours between')
    expect(items[1]).toHaveTextContent('typed minutes and consumables')
  })

  it('logs a replacement with the reading from Bambuddy', async () => {
    await api.setMachineRate(LINKED, { profile: {}, wear_parts: [{ name: 'Nozzle', cost: '35', life_hours: '1000' }], maintenance: [] })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
    expect(within(form).getByLabelText('Counter reading (h)')).toHaveValue('495.3')
    expect(form).toHaveTextContent('from Bambuddy')
    expect(within(form).getByLabelText('Cost')).toHaveValue('35')
    await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
    await waitFor(() => expect(machineRateEvents(LINKED)).toHaveLength(1))
    expect(machineRateEvents(LINKED)[0]).toMatchObject({ kind: 'part', name: 'Nozzle', hours_at: '495.3', hours_source: 'bambuddy', cost: '35' })
    expect(getToasts().map((t) => t.title)).toContain('Replacement logged')
    expect(await within(wear).findByRole('button', { name: 'Replacements (1)' })).toBeInTheDocument()
  })

  it("logs a replacement with the reading from ScadBuddy's count", async () => {
    await nozzle()
    await api.linkPrinterLan(GARAGE, { host: '10.0.0.5', access_code: '12345678' })
    setLanCount(GARAGE, { run_hours: '612.4' })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
    expect(within(form).getByLabelText('Counter reading (h)')).toHaveValue('612.4')
    expect(form).toHaveTextContent('from ScadBuddy’s count')
    // Logged late: the reading is corrected.
    await user.clear(within(form).getByLabelText('Counter reading (h)'))
    await user.type(within(form).getByLabelText('Counter reading (h)'), '600')
    await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
    await waitFor(() => expect(machineRateEvents(GARAGE)[0]).toMatchObject({ hours_at: '600', hours_source: 'lan_watch' }))
  })

  it('asks for the reading on a printer with no counter, or the hours used instead', async () => {
    await nozzle()
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
    expect(within(form).getByLabelText('Counter reading (h)')).toHaveValue('')
    await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Enter the counter reading')
    await user.click(within(form).getByRole('button', { name: 'Hours used instead' }))
    await user.type(within(form).getByLabelText('Hours used (h)'), '640')
    await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
    await waitFor(() => expect(machineRateEvents(GARAGE)[0]).toMatchObject({ hours_at: null, hours_used: '640' }))
  })

  it('says why there is no reading when Bambuddy does not answer', async () => {
    setBambuddyHours(null)
    await api.setMachineRate(LINKED, { profile: {}, wear_parts: [{ name: 'Nozzle', cost: '35', life_hours: '1000' }], maintenance: [] })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    expect(within(panel).getByTestId('print-hours')).toHaveTextContent('Bambuddy did not answer, so there is no counter reading now.')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
    await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Bambuddy did not answer, so there is no counter reading. Try again, or enter the reading.')
  })

  it("logs a task on a printer Bambuddy does not track, and points a linked printer's tasks at Bambuddy", async () => {
    await nozzle()
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    await user.click(within(task).getByRole('button', { name: 'Log done' }))
    const form = within(task).getByRole('form', { name: 'Log Clean plate done' })
    await user.type(within(form).getByLabelText('Counter reading (h)'), '12.5')
    await user.type(within(form).getByLabelText('Minutes'), '8')
    await user.click(within(form).getByRole('button', { name: 'Log done' }))
    await waitFor(() => expect(machineRateEvents(GARAGE)[0]).toMatchObject({ kind: 'task', hours_at: '12.5', hours_source: 'typed', minutes: '8' }))
    expect(getToasts().map((t) => t.title)).toContain('Maintenance logged')

    await api.setMachineRate(LINKED, { profile: {}, wear_parts: [], maintenance: [{ task: 'Lubricate rails', minutes: '20', consumables: '2' }] })
    await user.click(within(panel).getByRole('button', { name: 'Close' }))
    const linked = await open(user, '3DP-31B-598')
    expect(linked).toHaveTextContent('Mark tasks done in Bambuddy; ScadBuddy reads them.')
    expect(within(linked).queryByRole('button', { name: 'Log done' })).toBeNull()
  })

  it('adds minutes and consumables to an imported event, and edits and deletes a logged one', async () => {
    await api.setMachineRate(LINKED, {
      profile: {},
      wear_parts: [{ name: 'Nozzle', cost: '35', life_hours: '1000' }],
      maintenance: [{ task: 'Clean build plate', minutes: '10', consumables: '0' }],
    })
    importBambuddyEvent(LINKED, 'Clean build plate', '66.5', '2026-10-01T12:00:00Z')
    importBambuddyEvent(LINKED, 'Clean build plate', '101.5', '2026-10-05T12:00:00Z')
    await api.addMachineRateEvent(LINKED, { kind: 'part', name: 'Nozzle', cost: '35', hours_at: '400' })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')

    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    await user.click(within(task).getByRole('button', { name: 'Done (2)' }))
    const done = within(task).getByRole('list', { name: 'Done: Clean build plate' })
    const newest = within(done).getAllByRole('listitem')[0]!
    expect(newest).toHaveTextContent('from Bambuddy')
    expect(newest).toHaveTextContent('35 h since the one before')
    expect(within(newest).queryByRole('button', { name: 'Delete' })).toBeNull()
    await user.click(within(newest).getByRole('button', { name: 'Add minutes and consumables' }))
    await user.type(within(newest).getByLabelText('Minutes'), '12')
    await user.type(within(newest).getByLabelText('Consumables'), '0.5')
    await user.click(within(newest).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(machineRateEvents(LINKED)[1]).toMatchObject({ minutes: '12', consumables: '0.5' }))
    expect(await within(task).findByText(/12 min, 0.5 consumables/)).toBeInTheDocument()

    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Replacements (1)' }))
    const item = within(within(wear).getByRole('list', { name: 'Replacements: Nozzle' })).getAllByRole('listitem')[0]!
    await user.click(within(item).getByRole('button', { name: 'Edit' }))
    await user.clear(within(item).getByLabelText('Cost'))
    await user.type(within(item).getByLabelText('Cost'), '42')
    await user.click(within(item).getByRole('button', { name: 'Save' }))
    await waitFor(() => expect(machineRateEvents(LINKED)[2]).toMatchObject({ cost: '42' }))
    await user.click(await within(wear).findByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(machineRateEvents(LINKED)).toHaveLength(2))
    expect(await within(wear).findByText('No replacements logged. Log one when you change this part.')).toBeInTheDocument()
  })

  it('shows the print-hours line for each counter, and saves the offset and the window', async () => {
    await nozzle()
    setLanCount(LINKED, { run_hours: '12.4' })
    const { user } = renderPage(<MachineRates />)
    let panel = await open(user, 'Garage')
    expect(within(panel).getByTestId('print-hours')).toHaveTextContent('No counter: enter the reading when you log an event.')
    await user.click(within(panel).getByRole('button', { name: 'Close' }))

    await api.setMachineRate(LINKED, { profile: {}, wear_parts: [], maintenance: [] })
    panel = await open(user, '3DP-31B-598')
    expect(within(panel).getByTestId('print-hours')).toHaveTextContent('495.3 h from Bambuddy')
    expect(within(panel).getByTestId('print-hours')).toHaveTextContent('ScadBuddy’s count: 12.4 h, a check only')
    await user.click(within(panel).getByRole('button', { name: 'Close' }))

    await api.linkPrinterLan(GARAGE, { host: '10.0.0.5', access_code: '12345678' })
    setLanCount(GARAGE, { run_hours: '612.4', unseen_hours: '3.1', started_at: '2026-11-20T09:00:00Z' })
    panel = await open(user, 'Garage')
    const line = within(panel).getByTestId('print-hours')
    expect(line).toHaveTextContent('612.4 h, counted by ScadBuddy since 2026-11-20')
    expect(line).toHaveTextContent('3.1 h not seen (the printer was offline and its state changed)')
    await user.clear(within(panel).getByLabelText('Offset (h)'))
    await user.type(within(panel).getByLabelText('Offset (h)'), '100')
    await user.clear(within(panel).getByLabelText('Actuals window (events)'))
    await user.type(within(panel).getByLabelText('Actuals window (events)'), '5')
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(async () => expect((await api.machineRate(GARAGE)).inputs?.profile).toMatchObject({ lan_hours_offset: '100', actuals_window: 5 }))
    expect(await within(panel).findByText(/712.4 h, counted by ScadBuddy/)).toBeInTheDocument()
  })

  it('logs a replacement outside a secure context, where crypto.randomUUID is missing', async () => {
    await nozzle()
    const original = Object.getOwnPropertyDescriptor(crypto, 'randomUUID')
    Object.defineProperty(crypto, 'randomUUID', { value: undefined, configurable: true })
    try {
      const { user } = renderPage(<MachineRates />)
      const panel = await open(user, 'Garage')
      const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
      await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
      const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
      await user.type(within(form).getByLabelText('Counter reading (h)'), '120')
      await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
      await waitFor(() => expect(machineRateEvents(GARAGE)).toHaveLength(1))
      expect(machineRateEvents(GARAGE)[0]!.source).toMatch(/^[0-9a-f]{32}$/)
    } finally {
      if (original) Object.defineProperty(crypto, 'randomUUID', original)
      else delete (crypto as { randomUUID?: unknown }).randomUUID
    }
  })

  it('still saves the machine rate while a log form is open on a printer with no counter', async () => {
    await nozzle()
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    expect(within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })).toBeInTheDocument()
    await user.clear(within(panel).getByLabelText('Actuals window (events)'))
    await user.type(within(panel).getByLabelText('Actuals window (events)'), '4')
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(async () => expect((await api.machineRate(GARAGE)).inputs?.profile).toMatchObject({ actuals_window: 4 }))
  })

  it('says a late-dated event needs the reading on that day', async () => {
    await nozzle()
    await api.linkPrinterLan(GARAGE, { host: '10.0.0.5', access_code: '12345678' })
    setLanCount(GARAGE, { run_hours: '612.4' })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
    expect(form).not.toHaveTextContent('change it to the reading on that day')
    await user.clear(within(form).getByLabelText('Date'))
    await user.type(within(form).getByLabelText('Date'), '2026-01-05')
    expect(form).toHaveTextContent('This is the reading now: change it to the reading on that day.')
  })

  it('refuses a date after today typed by hand', async () => {
    await nozzle()
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Log replacement' }))
    const form = within(wear).getByRole('form', { name: 'Log replacement of Nozzle' })
    await user.type(within(form).getByLabelText('Counter reading (h)'), '120')
    const tomorrow = new Date(Date.now() + 86_400_000)
    const day = `${tomorrow.getFullYear()}-${String(tomorrow.getMonth() + 1).padStart(2, '0')}-${String(tomorrow.getDate()).padStart(2, '0')}`
    await user.clear(within(form).getByLabelText('Date'))
    await user.type(within(form).getByLabelText('Date'), day)
    await user.click(within(form).getByRole('button', { name: 'Log replacement' }))
    expect(await within(form).findByRole('alert')).toHaveTextContent('Enter a date no later than today.')
    expect(machineRateEvents(GARAGE)).toHaveLength(0)
  })

  it('saves an emptied offset as 0, not as the old offset', async () => {
    await nozzle()
    await api.linkPrinterLan(GARAGE, { host: '10.0.0.5', access_code: '12345678' })
    setLanCount(GARAGE, { run_hours: '612.4' })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    await user.clear(within(panel).getByLabelText('Offset (h)'))
    await user.type(within(panel).getByLabelText('Offset (h)'), '100')
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(async () => expect((await api.machineRate(GARAGE)).inputs?.profile).toMatchObject({ lan_hours_offset: '100' }))
    await user.clear(within(panel).getByLabelText('Offset (h)'))
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(async () => expect(Number((await api.machineRate(GARAGE)).inputs?.profile?.lan_hours_offset)).toBe(0))
  })

  it('refuses a window outside 1 to 20', async () => {
    await nozzle()
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    await user.clear(within(panel).getByLabelText('Actuals window (events)'))
    await user.type(within(panel).getByLabelText('Actuals window (events)'), '21')
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    expect(await within(panel).findByRole('alert')).toHaveTextContent('Enter the actuals window as a whole number from 1 to 20.')
  })

  it("moves a part's events with it when it is renamed, and names events no row has", async () => {
    await nozzle()
    await replace('100', '2026-10-01T12:00:00Z')
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.clear(within(wear).getByLabelText('Part'))
    await user.type(within(wear).getByLabelText('Part'), 'Hardened nozzle')
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    await waitFor(() => expect(machineRateEvents(GARAGE)[0]?.name).toBe('Hardened nozzle'))

    await user.click(within(panel).getByRole('button', { name: 'Remove wear part Hardened nozzle' }))
    await user.click(within(panel).getByRole('button', { name: 'Save machine rate' }))
    expect(await within(panel).findByTestId('orphaned')).toHaveTextContent('Events kept for rows removed: Hardened nozzle (1 replacement)')
  })
})

// #6665 — the last piece of #5774: "Read from Bambuddy", every event paged, a confirmed delete,
// and rows keyed by name so an open list stays with its row.
describe('MachineRates actuals follow-up (#6665)', () => {
  beforeEach(() => {
    resetMockState()
    clearToasts()
  })

  const linkedNozzle = () =>
    api.setMachineRate(LINKED, {
      profile: {},
      wear_parts: [{ name: 'Nozzle', cost: '35', life_hours: '1000' }],
      maintenance: [{ task: 'Clean build plate', minutes: '10', consumables: '0' }],
    })

  async function open(user: ReturnType<typeof renderPage>['user'], name: string) {
    await user.click(await screen.findByRole('button', { name: `Machine rate of ${name}` }))
    const panel = await screen.findByRole('region', { name: `Machine rate of ${name}` })
    await within(panel).findByTestId(/machine-rate-(breakdown|book)/)
    return panel
  }

  it('reads the maintenance log from Bambuddy and says when it was last read', async () => {
    await linkedNozzle()
    setBambuddyLogAnswers(false)
    setEventsReadAt(LINKED, new Date(Date.now() - 10 * 60_000).toISOString())
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    expect(within(panel).getByTestId('events-read')).toHaveTextContent('Last read 10 minutes ago')
    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    expect(within(task).getByRole('button', { name: 'Done (0)' })).toBeInTheDocument()

    setBambuddyLogAnswers(true)
    importBambuddyEvent(LINKED, 'Clean build plate', '66.5', '2026-10-01T12:00:00Z')
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    expect(await within(task).findByRole('button', { name: 'Done (1)' })).toBeInTheDocument()
    expect(within(panel).getByTestId('events-read')).toHaveTextContent('Last read just now')
    expect(getToasts().map((t) => t.title)).toContain('Maintenance read from Bambuddy')
  })

  it('keeps unsaved edits when it reads from Bambuddy', async () => {
    await linkedNozzle()
    setEventsReadAt(LINKED, '2026-11-01T10:00:00Z')
    setBambuddyLogAnswers(false)
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    await user.clear(within(panel).getByLabelText('Watts'))
    await user.type(within(panel).getByLabelText('Watts'), '150')
    setBambuddyLogAnswers(true)
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    await waitFor(() => expect(within(panel).getByTestId('events-read')).toHaveTextContent('Last read just now'))
    expect(within(panel).getByLabelText('Watts')).toHaveValue('150')
  })

  it('says why Bambuddy could not be read, in plain words', async () => {
    const { user } = renderPage(<MachineRates />)
    let panel = await open(user, '3DP-31B-598')
    expect(within(panel).getByTestId('events-read')).toHaveTextContent('Not read from Bambuddy yet')
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    expect(await within(panel).findByText('Save the machine rate first, then read its maintenance from Bambuddy.')).toHaveAttribute('role', 'alert')

    await linkedNozzle()
    setEventsReadAt(LINKED, '2026-11-18T14:02:00')
    setBambuddyLogAnswers(false)
    // Opened again, so the page holds the read time the server now has.
    await user.click(within(panel).getByRole('button', { name: 'Close' }))
    panel = await open(user, '3DP-31B-598')
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    expect(await within(panel).findByText(`Bambuddy did not answer. The rate uses the events last read at ${formatLocalTime(Date.parse('2026-11-18T14:02:00'))}.`)).toHaveAttribute('role', 'alert')

    server.use(
      http.post('/api/v1/printers/:id/machine-rate/import', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/machine-rate/not_linked', title: 'not_linked', status: 409, detail: 'The printer is not linked to Bambuddy: it has no log to read.' },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    expect(await within(panel).findByText('This printer is no longer linked to Bambuddy, so there is no maintenance log to read.')).toBeInTheDocument()
  })

  it('drops "did not answer" once the page reads Bambuddy again', async () => {
    await linkedNozzle()
    setEventsReadAt(LINKED, '2026-11-18T14:02:00')
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    // The import times out, and the view read right after it gets an answer.
    server.use(
      http.post('/api/v1/printers/:id/machine-rate/import', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/machine-rate/log_unavailable', title: 'log_unavailable', status: 503, detail: 'Bambuddy did not answer.' },
          { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    await waitFor(() => expect(within(panel).getByTestId('events-read')).toHaveTextContent('Last read just now'))
    expect(within(panel).queryByText(/Bambuddy did not answer\. The rate uses/)).not.toBeInTheDocument()
  })

  it('decides "did not answer" on the server\'s read times, never the browser\'s clock', async () => {
    const unavailable = () =>
      http.post('/api/v1/printers/:id/machine-rate/import', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/machine-rate/log_unavailable', title: 'log_unavailable', status: 503, detail: 'Bambuddy did not answer.' },
          { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      )
    // A server clock behind the browser: the re-read answered, stamped earlier than the browser's now.
    await linkedNozzle()
    const behind = new Date(Date.now() - 2 * 3600_000).toISOString()
    setEventsReadAt(LINKED, behind)
    setBambuddyLogAnswers(false)
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    const real = await api.machineRate(LINKED)
    const answered = new Date(Date.parse(behind) + 60_000).toISOString()
    server.use(unavailable(), http.get('/api/v1/printers/:id/machine-rate', () => HttpResponse.json({ ...real, actuals: { ...real.actuals, events_read_at: answered } })))
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    await waitFor(() => expect(within(panel).getByTestId('events-read').querySelector('time')).toHaveAttribute('dateTime', answered))
    expect(within(panel).queryByText(/Bambuddy did not answer\. The rate uses/)).not.toBeInTheDocument()

    // A server clock ahead of the browser: the read failed, and its stamp is still the one seen.
    const ahead = new Date(Date.now() + 3600_000).toISOString()
    server.use(unavailable(), http.get('/api/v1/printers/:id/machine-rate', () => HttpResponse.json({ ...real, actuals: { ...real.actuals, events_read_at: ahead } })))
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    await waitFor(() => expect(within(panel).getByTestId('events-read').querySelector('time')).toHaveAttribute('dateTime', ahead))
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    expect(await within(panel).findByText(/Bambuddy did not answer\. The rate uses/)).toHaveAttribute('role', 'alert')
  })

  it('keeps the stored-intervals line when the re-read gets the log but not the tasks', async () => {
    await linkedNozzle()
    setEventsReadAt(LINKED, '2026-11-18T14:02:00')
    setBambuddyLogAnswers(false)
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    const real = await api.machineRate(LINKED)
    server.use(
      http.post('/api/v1/printers/:id/machine-rate/import', () =>
        HttpResponse.json(
          { type: 'https://scadbuddy.dev/problems/machine-rate/log_unavailable', title: 'log_unavailable', status: 503, detail: 'Bambuddy did not answer.' },
          { status: 503, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
      http.get('/api/v1/printers/:id/machine-rate', () =>
        HttpResponse.json({ ...real, breakdown: { ...real.breakdown, intervals: 'stored' }, actuals: { ...real.actuals, events_read_at: '2026-11-18T15:00:00' } }),
      ),
    )
    await user.click(within(panel).getByRole('button', { name: 'Read from Bambuddy' }))
    const line = await within(panel).findByText(`Bambuddy did not answer. The rate uses the events last read at ${formatLocalTime(Date.parse('2026-11-18T15:00:00'))}.`)
    // A newer read answered, so the line is the stored-intervals note, not an alert.
    expect(line).not.toHaveAttribute('role', 'alert')
  })

  it('says when the page could not read Bambuddy, and which read the rate uses', async () => {
    await linkedNozzle()
    const real = await api.machineRate(LINKED)
    server.use(
      http.get('/api/v1/printers/:id/machine-rate', () =>
        HttpResponse.json({ ...real, breakdown: { ...real.breakdown, intervals: 'stored' }, actuals: { ...real.actuals, events_read_at: '2026-11-18T14:02:00' } }),
      ),
    )
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    expect(within(panel).getByText(`Bambuddy did not answer. The rate uses the events last read at ${formatLocalTime(Date.parse('2026-11-18T14:02:00'))}.`)).toBeInTheDocument()
  })

  it('offers no Bambuddy read on a printer Bambuddy does not track', async () => {
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    expect(within(panel).queryByRole('button', { name: 'Read from Bambuddy' })).toBeNull()
  })

  it('pages back through every event of a row', async () => {
    await linkedNozzle()
    for (let i = 0; i < 55; i++) importBambuddyEvent(LINKED, 'Clean build plate', String(10 * i), `2026-0${1 + Math.floor(i / 28)}-${String(1 + (i % 28)).padStart(2, '0')}T12:00:00Z`)
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    const task = within(panel).getByRole('group', { name: 'Maintenance task 1' })
    await user.click(within(task).getByRole('button', { name: /^Done \(/ }))
    const list = () => within(task).getByRole('list', { name: 'Done: Clean build plate' })
    expect(within(list()).getAllByRole('listitem').length).toBeLessThan(50)
    await user.click(within(task).getByRole('button', { name: 'Show older events' }))
    expect(await within(task).findByText(/Older events, not in the rate/)).toBeInTheDocument()
    await waitFor(() => expect(within(list()).getAllByRole('listitem')).toHaveLength(50))
    await user.click(within(task).getByRole('button', { name: 'Show more' }))
    await waitFor(() => expect(within(list()).getAllByRole('listitem')).toHaveLength(55))
    expect(within(task).getByText('That is every event.')).toBeInTheDocument()
    expect(within(task).queryByRole('button', { name: 'Show more' })).toBeNull()
    // Each event once, newest first.
    const items = within(list()).getAllByRole('listitem')
    expect(items[0]).toHaveTextContent('2026-02-27')
    expect(items[54]).toHaveTextContent('2026-01-01')
  })

  it('asks before it deletes a logged event', async () => {
    await linkedNozzle()
    await api.addMachineRateEvent(LINKED, { kind: 'part', name: 'Nozzle', cost: '35', hours_at: '400' })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, '3DP-31B-598')
    const wear = within(panel).getByRole('group', { name: 'Wear part 1' })
    await user.click(within(wear).getByRole('button', { name: 'Replacements (1)' }))
    await user.click(within(wear).getByRole('button', { name: 'Delete' }))
    const dialog = await screen.findByRole('alertdialog')
    expect(dialog).toHaveTextContent('Delete this replacement of Nozzle?')
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(machineRateEvents(LINKED)).toHaveLength(1)
    await user.click(within(wear).getByRole('button', { name: 'Delete' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Delete' }))
    await waitFor(() => expect(machineRateEvents(LINKED)).toHaveLength(0))
  })

  it('keeps an open event list with its row when a row above it is removed', async () => {
    await api.setMachineRate(GARAGE, {
      profile: {},
      wear_parts: [
        { name: 'Nozzle', cost: '35', life_hours: '1000' },
        { name: 'Plate', cost: '40', life_hours: '2000' },
      ],
      maintenance: [],
    })
    await api.addMachineRateEvent(GARAGE, { kind: 'part', name: 'Plate', cost: '40', hours_at: '120' })
    const { user } = renderPage(<MachineRates />)
    const panel = await open(user, 'Garage')
    const plate = within(panel).getByRole('group', { name: 'Wear part 2' })
    await user.click(within(plate).getByRole('button', { name: 'Replacements (1)' }))
    expect(within(panel).getByRole('list', { name: 'Replacements: Plate' })).toBeInTheDocument()
    await user.click(within(panel).getByRole('button', { name: 'Remove wear part Nozzle' }))
    const moved = within(panel).getByRole('group', { name: 'Wear part 1' })
    expect(within(moved).getByLabelText('Part')).toHaveValue('Plate')
    expect(within(moved).getByRole('list', { name: 'Replacements: Plate' })).toBeInTheDocument()
  })
})
