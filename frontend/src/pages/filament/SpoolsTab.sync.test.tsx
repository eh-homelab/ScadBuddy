import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { clearToasts, getToasts } from '../../lib/toast'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SpoolsTab } from './SpoolsTab'
import { syncText } from './syncText'

afterEach(() => clearToasts())

describe('the weights sync result in words', () => {
  it('names what it did and each kind it skipped, with the count', () => {
    expect(syncText({ booked: 3, locked: 1, printing: 2, no_remain: 0, stale: 0, stale_printers: [] })).toEqual({ title: 'Synced 3 spools', detail: 'Skipped 1 locked, 2 on a printing printer.' })
  })

  it('uses the singular, and leaves out a count of zero', () => {
    expect(syncText({ booked: 1, locked: 0, printing: 0, no_remain: 1, stale: 0, stale_printers: [] })).toEqual({ title: 'Synced 1 spool', detail: 'Skipped 1 with no AMS weight.' })
  })

  it('says when nothing was synced and nothing was skipped', () => {
    expect(syncText({ booked: 0, locked: 0, printing: 0, no_remain: 0, stale: 0, stale_printers: [] })).toEqual({ title: 'Synced 0 spools', detail: 'No spool is loaded in an AMS.' })
  })

  it('names a stale report and its printers (#6679)', () => {
    const stale_printers = [
      { id: '00000000-0000-0000-0000-000000000001', name: 'H2C' },
      { id: '00000000-0000-0000-0000-000000000002', name: 'P1S' },
    ]
    expect(syncText({ booked: 2, locked: 0, printing: 0, no_remain: 0, stale: 4, stale_printers })).toEqual({
      title: 'Synced 2 spools',
      detail: 'Skipped 4 whose printer has not reported lately (H2C, P1S).',
    })
  })

  it('names a printer with no name by its short id', () => {
    const stale_printers = [{ id: '0badc0de-0000-0000-0000-000000000001', name: null }]
    expect(syncText({ booked: 0, locked: 0, printing: 0, no_remain: 0, stale: 1, stale_printers }).detail).toBe(
      'Skipped 1 whose printer has not reported lately (printer 0badc0de).',
    )
  })
})

describe('the Sync weights from AMS button', () => {
  it('syncs, shows the counts in a toast and reloads the list', async () => {
    const { user } = renderPage(<SpoolsTab />)
    await screen.findByRole('table', { name: 'Spools' })
    await user.click(screen.getByRole('button', { name: 'Sync weights from AMS' }))
    await waitFor(() => expect(getToasts().map((one) => one.title)).toContain('Synced 3 spools'))
    expect(getToasts()[0]!.detail).toBe('Skipped 1 locked, 2 on a printing printer.')
  })

  it('keeps the button from a second click while it runs', async () => {
    let release = () => {}
    server.use(
      http.post('/api/v1/filament/spools/sync-ams-weights', async () => {
        await new Promise<void>((resolve) => (release = resolve))
        return HttpResponse.json({ booked: 0, locked: 0, printing: 0, no_remain: 0, stale: 0, stale_printers: [], spools: [] })
      }),
    )
    const { user } = renderPage(<SpoolsTab />)
    await screen.findByRole('table', { name: 'Spools' })
    await user.click(screen.getByRole('button', { name: 'Sync weights from AMS' }))
    expect(await screen.findByRole('button', { name: 'Syncing weights…' })).toBeDisabled()
    release()
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sync weights from AMS' })).toBeEnabled())
  })

  it('shows the server text when the sync fails', async () => {
    server.use(http.post('/api/v1/filament/spools/sync-ams-weights', () => HttpResponse.json({ detail: 'Bambuddy is down.' }, { status: 502 })))
    const { user } = renderPage(<SpoolsTab />)
    await screen.findByRole('table', { name: 'Spools' })
    await user.click(screen.getByRole('button', { name: 'Sync weights from AMS' }))
    await waitFor(() => expect(getToasts().map((one) => one.title)).toContain('The weights could not be synced.'))
    expect(getToasts()[0]!.detail).toBe('Bambuddy is down.')
    expect(screen.getByRole('alert')).toHaveTextContent('Bambuddy is down.')
  })
})

describe('the needs drying badge in the spools table', () => {
  it('marks the spool that needs drying and no other', async () => {
    renderPage(<SpoolsTab />)
    const table = await screen.findByRole('table', { name: 'Spools' })
    const rows = within(table).getAllByRole('row').filter((row) => row.dataset.spool)
    const flagged = rows.filter((row) => within(row).queryByText('Needs drying'))
    expect(flagged.map((row) => row.dataset.spool)).toEqual(['2'])
  })
})
