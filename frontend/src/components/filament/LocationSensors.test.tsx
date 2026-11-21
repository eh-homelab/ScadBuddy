import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { listSensors } from '../../api/filament'
import { mockSettings } from '../../mocks/handlers'
import { L } from '../../mocks/features/filament/seed'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { LocationSensors } from './LocationSensors'

async function show(location: string, canAdd = true) {
  const sensors = await listSensors(location)
  const changed = vi.fn()
  const view = renderPage(<LocationSensors locationId={location} locationName="Place" sensors={sensors} canAdd={canAdd} onChanged={changed} />)
  return { ...view, changed }
}

describe('the sensors of a place', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('shows each sensor with its value and unit', async () => {
    await show(L.DRY_BOX)
    const humidity = screen.getByRole('listitem', { name: 'Dry box humidity' })
    expect(within(humidity).getByText('11 %')).toBeInTheDocument()
    expect(within(humidity).getByText('Humidity')).toBeInTheDocument()
  })

  it('says a sensor is over its limit, with the limit', async () => {
    await show(L.SHELF_A)
    const row = screen.getByRole('listitem', { name: 'Shelf A humidity' })
    expect(within(row).getByText('Above the limit')).toBeInTheDocument()
    expect(within(row).getByText('Alert above 55 %')).toBeInTheDocument()
  })

  it('says a sensor is under its limit when the value is below the alert-below limit', async () => {
    const [shelf] = await listSensors(L.SHELF_A)
    const cold = { ...shelf!, name: 'Cold shelf', alert_above: null, alert_below: 10, last_value: 5, alerting: true }
    renderPage(<LocationSensors locationId={L.SHELF_A} locationName="Place" sensors={[cold]} canAdd={false} onChanged={vi.fn()} />)
    const row = screen.getByRole('listitem', { name: 'Cold shelf' })
    expect(within(row).getByText('Below the limit')).toBeInTheDocument()
    expect(within(row).queryByText('Above the limit')).toBeNull()
  })

  it('says a sensor cannot be reached', async () => {
    await show(L.DRY_BOX)
    const row = screen.getByRole('listitem', { name: 'Dry box temperature' })
    expect(within(row).getByText('Not reachable')).toBeInTheDocument()
  })

  it('shows the last notification sent through Bambuddy', async () => {
    await show(L.SHELF_A)
    const row = screen.getByRole('listitem', { name: 'Shelf A humidity' })
    expect(within(row).getByText('Notifies through Bambuddy')).toBeInTheDocument()
    expect(within(row).getByText(/Notified through Bambuddy \(alert started\)/)).toHaveTextContent(/, to 2 channels\.$/)
  })

  it('says why the last notification was not sent', async () => {
    await show(L.DRY_BOX)
    const row = screen.getByRole('listitem', { name: 'Dry box humidity' })
    expect(within(row).getByText(/Notification not sent \(alert ended/)).toHaveTextContent(/Give the key the Send notifications permission in Bambuddy\./)
  })

  async function waiting() {
    const [shelf] = await listSensors(L.SHELF_A)
    const queued = { ...shelf!, last_notification: { ...shelf!.last_notification!, kind: 'stopped' as const, state: 'queued' as const, sent_at: null, channels: null } }
    renderPage(<LocationSensors locationId={L.SHELF_A} locationName="Place" sensors={[queued]} canAdd={false} onChanged={vi.fn()} />)
    return screen.findByText(/Notification waiting \(alert ended/)
  }

  it('promises a queued notice within a minute when Bambuddy is connected', async () => {
    const notice = await waiting()
    await waitFor(() => expect(notice).toHaveTextContent(/goes out through Bambuddy within about a minute/))
  })

  it('says a queued notice waits for Bambuddy when none is connected', async () => {
    const settings = { ...mockSettings(), bambuddy_url: '' }
    server.use(http.get('/api/v1/settings', () => HttpResponse.json(settings)))
    const notice = await waiting()
    await waitFor(() => expect(notice).toHaveTextContent(/goes out once Bambuddy is connected in Settings/))
    expect(notice).not.toHaveTextContent(/within about a minute/)
  })

  it.each([
    ['unauthorized', /Notification not sent .*Check the key in Settings\./],
    ['not_found', /Notification not sent .*Bambuddy must be 1\.3\.1 or later\./],
    ['interrupted', /Notification may not have been sent .*It is not sent again\./],
  ] as const)('says what a %s failure means', async (error, text) => {
    const [shelf] = await listSensors(L.SHELF_A)
    const failed = { ...shelf!, last_notification: { ...shelf!.last_notification!, state: 'failed' as const, channels: null, error } }
    renderPage(<LocationSensors locationId={L.SHELF_A} locationName="Place" sensors={[failed]} canAdd={false} onChanged={vi.fn()} />)
    expect(screen.getByText(/Notification (not|may not have been) sent/)).toHaveTextContent(text)
  })

  it('shows no notification line for a sensor that never sent one', async () => {
    await show(L.AMS_A)
    expect(screen.queryByText(/notif/i)).toBeNull()
  })

  it('turns notifications on for a sensor', async () => {
    const { user, changed } = await show(L.DRY_BOX)
    await user.click(screen.getByRole('button', { name: 'Edit Dry box temperature' }))
    await user.click(screen.getByRole('checkbox', { name: 'Notify me through Bambuddy when it starts and stops alerting' }))
    await user.click(screen.getByRole('button', { name: 'Save sensor' }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect((await listSensors(L.DRY_BOX)).find((one) => one.name === 'Dry box temperature')).toMatchObject({ notify: true })
  })

  it('opens a sensor history chart from the readings', async () => {
    // The mock's readings end at the sensor's `last_at`; the component keeps the last 24 h from now.
    vi.useFakeTimers({ toFake: ['Date'], now: Date.parse('2026-11-18T09:45:00Z') })
    const { user } = await show(L.SHELF_A)
    const row = screen.getByRole('listitem', { name: 'Shelf A humidity' })
    await user.click(within(row).getByRole('button', { name: 'Show history of Shelf A humidity' }))
    expect(await within(row).findByRole('img', { name: /Shelf A humidity: .* %, 96 readings/ })).toBeInTheDocument()
    await user.click(within(row).getByRole('button', { name: 'Hide history of Shelf A humidity' }))
    expect(within(row).queryByRole('img')).toBeNull()
  })

  it('shows the error when the history cannot be read', async () => {
    server.use(http.get('/api/v1/filament/sensors/:id/readings', () => HttpResponse.json({ detail: 'down' }, { status: 500 })))
    const { user } = await show(L.SHELF_A)
    await user.click(screen.getByRole('button', { name: 'Show history of Shelf A humidity' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('The history could not be loaded. Try again.')
  })

  it('edits the limit of a sensor and tells the place to reload', async () => {
    const { user, changed } = await show(L.SHELF_A)
    await user.click(screen.getByRole('button', { name: 'Edit Shelf A humidity' }))
    const above = screen.getByLabelText('Alert above')
    await user.clear(above)
    await user.type(above, '70')
    await user.click(screen.getByRole('button', { name: 'Save sensor' }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect((await listSensors(L.SHELF_A))[0]).toMatchObject({ alert_above: 70, alerting: false })
  })

  it('offers no archive for an AMS sensor, and says who made it', async () => {
    await show(L.AMS_A)
    expect(screen.queryByRole('button', { name: /^Archive/ })).toBeNull()
    expect(screen.getByText('Made by ScadBuddy from the printer')).toBeInTheDocument()
  })

  it('archives a Home Assistant sensor', async () => {
    const { user, changed } = await show(L.SHELF_A)
    await user.click(screen.getByRole('button', { name: 'Archive Shelf A humidity' }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect(await listSensors(L.SHELF_A)).toEqual([])
  })

  it('sends one archive call for a double click', async () => {
    let calls = 0
    server.use(
      http.post('/api/v1/filament/sensors/:id/archive', async () => {
        calls += 1
        await new Promise((resolve) => setTimeout(resolve, 50))
        return HttpResponse.json({})
      }),
    )
    const { user } = await show(L.SHELF_A)
    const button = screen.getByRole('button', { name: 'Archive Shelf A humidity' })
    await user.dblClick(button)
    await waitFor(() => expect(button).toBeEnabled())
    expect(calls).toBe(1)
  })

  it('says it is loading the Home Assistant sensors before the list arrives', async () => {
    server.use(
      http.get('/api/v1/filament/sensors/entities', async () => {
        await new Promise((resolve) => setTimeout(resolve, 100))
        return HttpResponse.json([])
      }),
    )
    const { user } = await show(L.WORKSHOP)
    await user.click(screen.getByRole('button', { name: 'Add a sensor to Place' }))
    expect(screen.getByText('Loading the Home Assistant sensors…')).toBeInTheDocument()
    expect(await screen.findByText('No sensor matches.')).toBeInTheDocument()
    expect(screen.queryByText('Loading the Home Assistant sensors…')).toBeNull()
  })

  it('adds a sensor by choosing a Home Assistant entity, which fills its name and unit', async () => {
    const { user, changed } = await show(L.WORKSHOP)
    await user.click(screen.getByRole('button', { name: 'Add a sensor to Place' }))
    await user.type(screen.getByLabelText('Search Home Assistant'), 'garage')
    await user.click(await screen.findByRole('button', { name: /Garage humidity/ }))
    expect(screen.getByLabelText('Name')).toHaveValue('Garage humidity')
    expect(screen.getByLabelText('Unit')).toHaveValue('%')
    await user.click(screen.getByRole('button', { name: 'Save sensor' }))
    await waitFor(() => expect(changed).toHaveBeenCalled())
    expect(await listSensors(L.WORKSHOP)).toMatchObject([{ name: 'Garage humidity', entity_id: 'sensor.garage_humidity', kind: 'humidity', source: 'home_assistant' }])
  })

  it('says Home Assistant cannot be listed when Bambuddy is not set up', async () => {
    server.use(http.get('/api/v1/filament/sensors/entities', () => HttpResponse.json({ detail: 'No Bambuddy is configured.' }, { status: 409 })))
    const { user } = await show(L.WORKSHOP)
    await user.click(screen.getByRole('button', { name: 'Add a sensor to Place' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Home Assistant sensors need Bambuddy. Connect Bambuddy in Settings first.')
  })

  it('offers no add button where a sensor cannot be added', async () => {
    await show(L.AMS_A, false)
    expect(screen.queryByRole('button', { name: /Add a sensor/ })).toBeNull()
  })
})
