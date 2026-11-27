import { screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { fakeRealtime } from '../lib/realtime.fake'
import { L } from '../mocks/features/filament/seed'
import { addTask, setTask } from '../mocks/features/tasks'
import { upsertLocation } from '../api/filament'
import { renderPage } from '../test/utils'
import { StationPage } from './StationPage'

// #8062 — the shop-floor screen for one place's open tasks.

const scanSchema = (expects: Record<string, unknown>) => ({
  type: 'object',
  properties: { spool: { type: 'string', title: 'Spool', 'x-sb-widget': 'scan', expects } },
  required: ['spool'],
})

function seed() {
  addTask({ id: 'st-2', title: 'Second job', place: L.SHELF_A, created_at: '2026-10-10T09:05:00Z' })
  addTask({
    id: 'st-1',
    title: 'First job',
    place: L.SHELF_A,
    created_at: '2026-10-10T09:01:00Z',
    schema: scanSchema({ kinds: ['spool'] }),
  })
  addTask({
    id: 'st-3',
    title: 'Check the shelf',
    place: L.SHELF_A,
    created_at: '2026-10-10T09:09:00Z',
    schema: scanSchema({ kinds: ['location'] }),
  })
  addTask({ id: 'other', title: 'Elsewhere job', place: L.DRY_BOX })
}

const station = () => renderPage(<StationPage />, { route: `/station/${L.SHELF_A}`, path: '/station/:place?' })

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
})
afterEach(() => vi.useRealTimers())

describe('the station page', () => {
  it('lists the place open tasks, oldest first', async () => {
    seed()
    station()
    const regions = await screen.findAllByRole('region')
    expect(regions.map((region) => region.getAttribute('aria-label'))).toEqual(
      expect.arrayContaining(['First job', 'Second job', 'Check the shelf']),
    )
    const names = regions.map((region) => region.getAttribute('aria-label'))
    expect(names.indexOf('First job')).toBeLessThan(names.indexOf('Second job'))
    expect(names.indexOf('Second job')).toBeLessThan(names.indexOf('Check the shelf'))
    expect(screen.queryByText('Elsewhere job')).toBeNull()
  })

  it('drops a task answered elsewhere on task.changed and keeps the scan field focused', async () => {
    seed()
    const realtime = fakeRealtime()
    station()
    const scan = await screen.findByRole('textbox', { name: 'Scan a code' })
    await waitFor(() => expect(scan).toHaveFocus())
    expect(realtime.following()).toContain(`tasks:${L.SHELF_A}`)
    setTask('st-2', { state: 'answered' })
    await realtime.signal(`tasks:${L.SHELF_A}`, 'task.changed')
    await waitFor(() => expect(screen.queryByRole('region', { name: 'Second job' })).toBeNull())
    expect(screen.getByRole('region', { name: 'First job' })).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: 'Scan a code' })).toHaveFocus()
  })

  it('takes the focus back when it is lost', async () => {
    seed()
    const { user } = station()
    const scan = await screen.findByRole('textbox', { name: 'Scan a code' })
    await user.click(document.body)
    await waitFor(() => expect(scan).toHaveFocus())
  })

  it('moves a scan that matches one task to that task scan field and fills it', async () => {
    seed()
    const { user } = station()
    const scan = await screen.findByRole('textbox', { name: 'Scan a code' })
    await user.type(scan, 'S-1{Enter}')
    const mine = within(await screen.findByRole('region', { name: 'First job' }))
    const field = mine.getByRole('textbox', { name: /Spool/ })
    await waitFor(() => expect(field).toHaveValue('S-1'))
    await waitFor(() => expect(field).toHaveFocus())
    expect(scan).toHaveValue('')
  })

  it('says so when a scan matches no task', async () => {
    seed()
    const { user } = station()
    const scan = await screen.findByRole('textbox', { name: 'Scan a code' })
    await user.type(scan, 'NOPE-1{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('No code NOPE-1')
  })

  it('matches a code the schema wrote in lower case', async () => {
    addTask({ id: 'lc', title: 'Lower case', place: L.SHELF_A, schema: scanSchema({ codes: ['l-2'] }) })
    const { user } = station()
    await user.type(await screen.findByRole('textbox', { name: 'Scan a code' }), 'L-2{Enter}')
    const field = within(await screen.findByRole('region', { name: 'Lower case' })).getByRole('textbox', { name: /Spool/ })
    await waitFor(() => expect(field).toHaveValue('L-2'))
  })

  it('takes a field that names both a kind and a code by either', async () => {
    addTask({ id: 'both', title: 'Either', place: L.SHELF_A, schema: scanSchema({ kinds: ['spool'], codes: ['L-2'] }) })
    const { user } = station()
    const scan = await screen.findByRole('textbox', { name: 'Scan a code' })
    await user.type(scan, 'L-2{Enter}')
    const field = within(await screen.findByRole('region', { name: 'Either' })).getByRole('textbox', { name: /Spool/ })
    await waitFor(() => expect(field).toHaveValue('L-2'))
    await user.type(scan, 'S-1{Enter}')
    await waitFor(() => expect(field).toHaveValue('S-1'))
  })

  it('never offers a scan to an entity field', async () => {
    addTask({
      id: 'ent',
      title: 'Pick a spool',
      place: L.SHELF_A,
      schema: { type: 'object', properties: { spool: { type: 'string', title: 'Spool', 'x-sb-widget': 'entity', kind: 'spool' } } },
    })
    const { user } = station()
    await user.type(await screen.findByRole('textbox', { name: 'Scan a code' }), 'S-1{Enter}')
    expect(await screen.findByRole('alert')).toHaveTextContent('No task here waits for S-1')
  })

  it('lets the person pick when several tasks wait for a code', async () => {
    addTask({ id: 'a', title: 'Job A', place: L.SHELF_A, schema: scanSchema({ kinds: ['spool'] }) })
    addTask({ id: 'b', title: 'Job B', place: L.SHELF_A, schema: scanSchema({ kinds: ['spool'] }) })
    const { user } = station()
    await user.type(await screen.findByRole('textbox', { name: 'Scan a code' }), 'S-1{Enter}')
    await user.click(await screen.findByRole('button', { name: 'Job B' }))
    const field = within(screen.getByRole('region', { name: 'Job B' })).getByRole('textbox', { name: /Spool/ })
    await waitFor(() => expect(field).toHaveValue('S-1'))
    expect(within(screen.getByRole('region', { name: 'Job A' })).getByRole('textbox', { name: /Spool/ })).toHaveValue('')
  })

  it('says what to do when the place has no tasks', async () => {
    station()
    expect(await screen.findByText(/No open tasks/)).toBeInTheDocument()
  })

  it('lists the stations at /station', async () => {
    await upsertLocation({ station: true }, L.SHELF_A)
    renderPage(<StationPage />, { route: '/station', path: '/station/:place?' })
    expect(await screen.findByRole('link', { name: /Shelf A/ })).toHaveAttribute('href', `/station/${L.SHELF_A}`)
  })

  it('says what to do when there is no station', async () => {
    renderPage(<StationPage />, { route: '/station', path: '/station/:place?' })
    expect(await screen.findByText(/turn on “Station for flow tasks”/)).toBeInTheDocument()
  })
})
