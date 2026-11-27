import { screen, waitFor, within } from '@testing-library/react'
import { delay, http, HttpResponse } from 'msw'
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

  it('drops a late answer to an older search', async () => {
    // "bambu" (every row) answers only after "bambu pink" (Hot Pink) did.
    let pinkAsked: () => void = () => {}
    const pink = new Promise<void>((resolve) => (pinkAsked = resolve))
    let bambuAsked = false
    let bambuAnswered = false
    server.use(
      http.get('/api/v1/filament/catalog/colours', async ({ request }) => {
        const q = new URL(request.url).searchParams.get('q')
        if (q === 'bambu pink') pinkAsked()
        if (q !== 'bambu') return undefined
        bambuAsked = true
        await pink
        await delay(100)
        bambuAnswered = true
        return undefined
      }),
    )
    const { user } = renderPage(<CatalogList kind="colours" />)
    await screen.findByRole('row', { name: /^Jade White/ })
    const box = screen.getByRole('searchbox', { name: 'Search colours' })
    await user.type(box, 'bambu')
    await waitFor(() => expect(bambuAsked).toBe(true))
    await user.type(box, ' pink')
    await waitFor(() => expect(screen.queryByRole('row', { name: /^Jade White/ })).not.toBeInTheDocument())
    await waitFor(() => expect(bambuAnswered).toBe(true))
    await delay(100)
    expect(screen.queryByRole('row', { name: /^Jade White/ })).not.toBeInTheDocument()
    expect(screen.getByRole('row', { name: /^Hot Pink/ })).toBeInTheDocument()
  })

  it('follows a run it did not start until it ends', async () => {
    const run = {
      id: '8d6f3c1e-0000-4000-8000-0000000061a2',
      source: 'filamentcolors',
      pages_read: 2,
      pages_total: 23,
      total: 2256,
      added: 200,
      updated: 0,
      unchanged: 0,
      kept: 0,
      skipped: 0,
      error: null,
      started_at: '2026-11-21T12:00:00Z',
      finished_at: null,
    }
    let reads = 0
    server.use(
      http.get('/api/v1/filament/colour-syncs', () => {
        reads += 1
        return HttpResponse.json([reads < 2 ? { ...run, state: 'running' } : { ...run, state: 'done', pages_read: 23, added: 2256, finished_at: '2026-11-21T12:05:00Z' }])
      }),
    )
    renderPage(<CatalogList kind="colours" />)
    expect(await screen.findByText('Reading page 3 of 23… 200 added so far.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sync from filamentcolors/ })).toBeDisabled()
    expect(await screen.findByText(/^Done: 2,256 added/, {}, { timeout: 4000 })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sync from filamentcolors/ })).toBeEnabled()
  })

  it('says a sync is already running when the press is refused', async () => {
    const detail = 'a bambu colour sync is running; follow it on GET /filament/colour-syncs'
    server.use(
      http.post('/api/v1/filament/colour-syncs', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Conflict', status: 409, detail, reason: 'colour_sync_running' }, { status: 409 }),
      ),
    )
    const { user } = renderPage(<CatalogList kind="colours" />)
    await screen.findByRole('row', { name: /^Jade White/ })
    await user.click(screen.getByRole('button', { name: /Add Bambu Lab colours/ }))
    expect(await screen.findByRole('alert')).toHaveTextContent(detail)
    expect(screen.getByRole('button', { name: /Add Bambu Lab colours/ })).toBeEnabled()
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
        'Stopped: filamentcolors.xyz did not answer (ConnectTimeout). 300 added, 0 changed, 0 already up to date, 2 kept (edited by a person, or from another source); what was read before stays.',
      ),
    ).toBeInTheDocument()
  })
})

describe('the Bambu colour code lookup (#6907)', () => {
  const lookup = async (code: string) => {
    const page = renderPage(<CatalogList kind="colours" />)
    const sources = await screen.findByRole('region', { name: 'Colour sources' })
    await page.user.type(within(sources).getByRole('textbox', { name: 'Bambu colour code' }), code)
    await page.user.click(within(sources).getByRole('button', { name: 'Look up' }))
    return { ...page, sources }
  }

  it("shows a code's name, colours, filament type and id", async () => {
    const { sources } = await lookup('10100')
    const found = await within(sources).findByRole('group', { name: 'Bambu colour code 10100' })
    expect(found).toHaveTextContent('Jade White')
    expect(found).toHaveTextContent('#FFFFFFFF')
    expect(found).toHaveTextContent('PLA')
    expect(found).toHaveTextContent('GFA00')
  })

  it('keeps the alpha of a translucent colour', async () => {
    const { sources } = await lookup('13612')
    const found = await within(sources).findByRole('group', { name: 'Bambu colour code 13612' })
    expect(found).toHaveTextContent('#F4EE2A80')
  })

  it('shows every colour of a multi-colour code', async () => {
    const { sources } = await lookup('15900')
    const multi = await within(sources).findByRole('group', { name: 'Bambu colour code 15900' })
    expect(within(multi).getAllByRole('img')).toHaveLength(2)
  })

  it('says when the table has no such code', async () => {
    const { sources } = await lookup('99999')
    expect(await within(sources).findByRole('alert')).toHaveTextContent("Bambu Studio's colour table has no code 99999.")
  })

  it("says why when the image has no colour table", async () => {
    server.use(
      http.get('/api/v1/filament/bambu-colour-codes', () =>
        HttpResponse.json({ type: 'about:blank', title: 'Service Unavailable', status: 503, detail: 'no colour table at /profiles/filaments_color_codes.json' }, { status: 503 }),
      ),
    )
    const { sources } = await lookup('10100')
    expect(await within(sources).findByRole('alert')).toHaveTextContent('no colour table at /profiles/filaments_color_codes.json')
  })

  it('sends the code as typed, trimmed, and asks nothing for an empty one', async () => {
    const asked: string[] = []
    server.events.on('request:start', ({ request }) => {
      if (request.url.includes('/bambu-colour-codes')) asked.push(new URL(request.url).searchParams.get('code') ?? '')
    })
    const page = renderPage(<CatalogList kind="colours" />)
    const sources = await screen.findByRole('region', { name: 'Colour sources' })
    expect(within(sources).getByRole('button', { name: 'Look up' })).toBeDisabled()
    await page.user.type(within(sources).getByRole('textbox', { name: 'Bambu colour code' }), ' 10100 {Enter}')
    await within(sources).findByRole('group', { name: 'Bambu colour code 10100' })
    expect(asked).toEqual(['10100'])
  })
})
