import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { spoolId } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { choose } from '../../test/select'
import { LedgerTab } from './LedgerTab'

const ledger = () => screen.findByRole('table', { name: 'Ledger' })
const dataRows = (table: HTMLElement) => within(table).getAllByRole('row').filter((row) => row.dataset.event)

describe('the Ledger tab', () => {
  it('lists events newest first, each linking its spool', async () => {
    renderPage(<LedgerTab />)
    const rows = dataRows(await ledger())
    expect(rows.length).toBeGreaterThan(10)
    const first = rows[0]!
    expect(first).toHaveTextContent('Print ended')
    expect(within(first).getByRole('link', { name: '#4' })).toHaveAttribute('href', `/filament/spools/${spoolId(4)}`)
  })

  it('Older asks with the cursor the last page gave, and adds the next rows', async () => {
    const asked: (string | null)[] = []
    server.use(
      http.get('/api/v1/filament/events', ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor')
        asked.push(cursor)
        const row = (n: number) => ({
          id: `00000000-0000-4000-8000-00000000000${n}`,
          spool_id: spoolId(1),
          spool_number: 1,
          at: `2026-11-0${n}T09:00:00Z`,
          kind: 'weighed',
          source: 'person',
          printer_id: null,
          ams_id: null,
          tray_id: null,
          grams_before: null,
          grams_after: null,
          delta_g: null,
          ref_kind: null,
          ref_id: null,
          actor: null,
          data: {},
        })
        return cursor === 'opaque-1'
          ? HttpResponse.json({ events: [row(1)], next_cursor: null })
          : HttpResponse.json({ events: [row(2)], next_cursor: 'opaque-1' })
      }),
    )
    const { user } = renderPage(<LedgerTab />)
    expect(dataRows(await ledger())).toHaveLength(1)
    await user.click(screen.getByRole('button', { name: 'Older' }))
    await waitFor(() => expect(dataRows(screen.getByRole('table', { name: 'Ledger' }))).toHaveLength(2))
    expect(asked).toEqual([null, 'opaque-1'])
    expect(screen.queryByRole('button', { name: 'Older' })).toBeNull()
  })

  it('reads its filters from the URL and sends them', async () => {
    const seen: URLSearchParams[] = []
    server.use(
      http.get('/api/v1/filament/events', ({ request }) => {
        seen.push(new URL(request.url).searchParams)
        return HttpResponse.json({ events: [], next_cursor: null })
      }),
    )
    renderPage(<LedgerTab />, { route: '/filament/ledger?spool=4&kind=weighed&source=scale&from=2026-11-01' })
    await ledger()
    const params = seen.at(-1)!
    expect(params.get('spool_id')).toBe(spoolId(4))
    expect(params.get('kind')).toBe('weighed')
    expect(params.get('source')).toBe('scale')
    expect(params.get('since')).toMatch(/^2026-1[01]-/)
    expect(screen.getByLabelText('Spool number')).toHaveValue(4)
    expect(screen.getByText('No events match.')).toBeInTheDocument()
  })

  it('sends the end of the To day as until, so a first page that is all after it never comes back', async () => {
    const seen: URLSearchParams[] = []
    server.use(
      http.get('/api/v1/filament/events', ({ request }) => {
        const params = new URL(request.url).searchParams
        seen.push(params)
        // An empty page: the test reads only the request, and `until` is what the server filters by.
        return HttpResponse.json({ events: [], next_cursor: null })
      }),
    )
    renderPage(<LedgerTab />, { route: '/filament/ledger?to=2026-11-02' })
    await ledger()
    expect(seen).toHaveLength(1)
    expect(new Date(seen[0]!.get('until')!).getTime()).toBe(new Date('2026-11-03T00:00:00').getTime())
  })

  it('shows only the events before the end of the To day', async () => {
    renderPage(<LedgerTab />, { route: '/filament/ledger?to=2026-11-02' })
    const rows = dataRows(await ledger())
    expect(rows.length).toBeGreaterThan(0)
    for (const row of rows) expect(row.querySelector('time')!.getAttribute('datetime')! < '2026-11-04').toBe(true)
  })

  it('changing a filter writes it to the URL', async () => {
    const { user } = renderPage(<LedgerTab />)
    await ledger()
    await choose(user, screen.getByRole('combobox', { name: 'Kind' }), 'Dried')
    await waitFor(() => expect(dataRows(screen.getByRole('table', { name: 'Ledger' }))).toHaveLength(1))
    expect(screen.getByRole('combobox', { name: 'Kind' })).toHaveTextContent('Dried')
  })
})
