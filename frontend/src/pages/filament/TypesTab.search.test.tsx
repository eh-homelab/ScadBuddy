import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { listTypes } from '../../api/filament'
import { server } from '../../mocks/server'
import { T } from '../../mocks/features/filament/seed'
import { seedEntityProperties } from '../../mocks/features/entities'
import { renderPage } from '../../test/utils'
import { TypesTab } from './TypesTab'

// #6894: the Types tab's text search, kept in the URL as `q=`.

function Search() {
  return <output data-testid="search">{useLocation().search}</output>
}

function render(route = '/filament/types') {
  return renderPage(
    <>
      <TypesTab />
      <Search />
    </>,
    { route },
  )
}

const table = () => screen.findByRole('table', { name: 'Types' })
const ids = (body: HTMLElement) =>
  within(body)
    .getAllByRole('row')
    .flatMap((row) => (row.dataset.type ? [row.dataset.type] : []))

describe('the Types tab search', () => {
  it('filters as you type and keeps the query in the URL', async () => {
    const { user } = render()
    await table()
    await user.type(screen.getByRole('searchbox', { name: 'Search types' }), 'hot pink')
    await waitFor(async () => expect(ids(await table())).toEqual([T.PINK]))
    expect(screen.getByTestId('search').textContent).toBe('?q=hot+pink')
  })

  it('finds a type by brand, colour, hex with or without # and code', async () => {
    const { user } = render()
    const box = screen.getByRole('searchbox', { name: 'Search types' })
    await table()
    for (const [query, expected] of [
      ['elegoo', [T.GOLD]],
      ['#D4AF37', [T.GOLD]],
      ['d4af37', [T.GOLD]],
      ['mistletoe', [T.MISTLETOE]],
      ['su-tpu', [T.TPU_RED]],
    ] as const) {
      await user.clear(box)
      await user.type(box, query)
      await waitFor(async () => expect(ids(await table()), query).toEqual(expected))
    }
  })

  it('finds a type by its SKU from the price sheets', async () => {
    render('/filament/types?q=A00-W1')
    await waitFor(async () => expect(ids(await table())).toEqual([T.JADE]))
    expect(screen.getByRole('searchbox', { name: 'Search types' })).toHaveValue('A00-W1')
  })

  it('finds a type by a GTIN in its properties (#7026)', async () => {
    seedEntityProperties(`spool_type:${T.MISTLETOE}`, { gtin: ['0123', '0456'] })
    render('/filament/types?q=0456')
    await waitFor(async () => expect(ids(await table())).toEqual([T.MISTLETOE]))
  })

  it('reads the properties again when the tab loads its types again (#7026)', async () => {
    const { user } = render('/filament/types?q=0456')
    expect(await screen.findByText('No type matches “0456”.')).toBeInTheDocument()
    seedEntityProperties(`spool_type:${T.MISTLETOE}`, { gtin: ['0456'] })
    await user.click(screen.getByRole('button', { name: 'Fill from slicer profiles' }))
    await waitFor(async () => expect(ids(await table())).toEqual([T.MISTLETOE]))
  })

  it('keeps the property values it has when a reload cannot read them (#7026)', async () => {
    seedEntityProperties(`spool_type:${T.MISTLETOE}`, { gtin: ['0456'] })
    const { user } = render('/filament/types?q=0456')
    await waitFor(async () => expect(ids(await table())).toEqual([T.MISTLETOE]))
    let failed = 0
    server.use(
      http.get('/api/v1/entity-kinds/:kind/properties', () => {
        failed++
        return HttpResponse.json({ detail: 'down' }, { status: 503 })
      }),
    )
    await user.click(screen.getByRole('button', { name: 'Fill from slicer profiles' }))
    await waitFor(() => expect(failed).toBeGreaterThan(0))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(ids(await table())).toEqual([T.MISTLETOE])
  })

  it('still searches when the properties cannot be read (#7026)', async () => {
    server.use(http.get('/api/v1/entity-kinds/:kind/properties', () => HttpResponse.json({ detail: 'down' }, { status: 503 })))
    render('/filament/types?q=elegoo')
    await waitFor(async () => expect(ids(await table())).toEqual([T.GOLD]))
  })

  it('says when nothing matches, and keeps the search beside Show archived', async () => {
    const { user } = render('/filament/types?q=nothing-like-this')
    expect(await screen.findByText('No type matches “nothing-like-this”.')).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Show archived types' }))
    expect(screen.getByTestId('search').textContent).toBe('?q=nothing-like-this&archived=true')
  })

  it('says types exist, not "No types yet", when every type is archived and hidden', async () => {
    const all = await listTypes()
    server.use(http.get('/api/v1/filament/types', () => HttpResponse.json(all.map((type) => ({ ...type, archived_at: '2026-01-01T00:00:00Z' })))))
    render('/filament/types?q=pink')
    expect(await screen.findByText('No type matches “pink”.')).toBeInTheDocument()
    expect(screen.queryByText(/No types yet/)).toBeNull()
  })

  it('with no search, says to tick Show archived types when every type is archived', async () => {
    const all = await listTypes()
    server.use(http.get('/api/v1/filament/types', () => HttpResponse.json(all.map((type) => ({ ...type, archived_at: '2026-01-01T00:00:00Z' })))))
    render()
    expect(await screen.findByText('No types shown. Tick Show archived types.')).toBeInTheDocument()
    expect(screen.queryByText(/No types yet/)).toBeNull()
  })
})
