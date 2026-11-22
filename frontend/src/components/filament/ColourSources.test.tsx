import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { CatalogList } from './CatalogList'

// #6907: the Colours catalog's sources and its search.

describe('the colour catalog', () => {
  it('adds Bambu Lab colours and says what the sync did', async () => {
    const { user } = renderPage(<CatalogList kind="colours" />)
    await screen.findByRole('row', { name: /^Jade White/ })
    await user.click(screen.getByRole('button', { name: /Add Bambu Lab colours/ }))
    expect(await screen.findByText(/Done: 2 added, 0 changed, 0 already up to date\./)).toBeInTheDocument()
    expect(await screen.findByRole('row', { name: /^Translucent Yellow/ })).toBeInTheDocument()
  })

  it('names where a synced colour came from, and none for a person’s own', async () => {
    renderPage(<CatalogList kind="colours" />)
    const jade = await screen.findByRole('row', { name: /^Jade White/ })
    expect(within(jade).getByText("From Bambu Studio's colour codes")).toBeInTheDocument()
    expect(within(screen.getByRole('row', { name: /^Hot Pink/ })).queryByText(/^From /)).not.toBeInTheDocument()
  })

  it('searches the colours on the server and offers no re-order', async () => {
    const { user } = renderPage(<CatalogList kind="colours" />)
    await screen.findByRole('row', { name: /^Hot Pink/ })
    expect(screen.queryByRole('button', { name: /^Move / })).not.toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: 'Search colours' }), 'ff6a13')
    await waitFor(() => expect(screen.queryByRole('row', { name: /^Hot Pink/ })).not.toBeInTheDocument())
    expect(screen.getByRole('row', { name: /^Translucent Orange/ })).toBeInTheDocument()
    await user.clear(screen.getByRole('searchbox', { name: 'Search colours' }))
    await user.type(screen.getByRole('searchbox', { name: 'Search colours' }), 'nothing like it')
    expect(await screen.findByText('No colour matches "nothing like it".')).toBeInTheDocument()
  })

  it('says why a filamentcolors sync stopped', async () => {
    const failed = {
      id: '8d6f3c1e-0000-4000-8000-0000000061a1',
      source: 'filamentcolors',
      state: 'failed',
      pages_read: 3,
      pages_total: 23,
      total: 2256,
      added: 300,
      updated: 0,
      unchanged: 0,
      kept: 2,
      skipped: 0,
      error: 'filamentcolors.xyz did not answer (ConnectTimeout)',
      started_at: '2026-11-21T12:00:00Z',
      finished_at: '2026-11-21T12:01:00Z',
    }
    server.use(
      http.post('/api/v1/filament/colour-syncs', () => HttpResponse.json(failed)),
      http.get('/api/v1/filament/colour-syncs', () => HttpResponse.json([failed])),
    )
    const { user } = renderPage(<CatalogList kind="colours" />)
    await screen.findByRole('row', { name: /^Jade White/ })
    await user.click(screen.getByRole('button', { name: /Sync from filamentcolors/ }))
    expect(
      await screen.findByText(
        'Stopped: filamentcolors.xyz did not answer (ConnectTimeout). 300 added, 0 changed, 0 already up to date, 2 kept as a person edited them; what was read before stays.',
      ),
    ).toBeInTheDocument()
  })
})
