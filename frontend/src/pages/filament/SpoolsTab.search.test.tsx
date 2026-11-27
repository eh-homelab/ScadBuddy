import { render as rtlRender, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useLocation, useNavigate, useParams } from 'react-router'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'
import { server } from '../../mocks/server'
import { seedEntityProperties } from '../../mocks/features/entities'
import { spoolId, T } from '../../mocks/features/filament/seed'
import { renderPage } from '../../test/utils'
import { SpoolsTab } from './SpoolsTab'

// #6894: the Spools tab's text search, kept in the URL as `q=`.

function Search() {
  return <output data-testid="search">{useLocation().search}</output>
}

function render(route = '/filament/spools') {
  return renderPage(
    <>
      <SpoolsTab />
      <Search />
    </>,
    { route },
  )
}

const table = () => screen.findByRole('table', { name: 'Spools' })
const numbers = (body: HTMLElement) =>
  within(body)
    .getAllByRole('row')
    .flatMap((row) => (row.dataset.spool ? [row.dataset.spool] : []))

describe('the Spools tab search', () => {
  it('filters as you type and keeps the query in the URL', async () => {
    const { user } = render()
    await table()
    await user.type(screen.getByRole('searchbox', { name: 'Search spools' }), 'hot pink')
    await waitFor(async () => expect(numbers(await table())).toEqual(['3']))
    expect(screen.getByTestId('search').textContent).toBe('?q=hot+pink')
  })

  it('finds a spool by #number or number', async () => {
    render('/filament/spools?q=%234')
    await waitFor(async () => expect(numbers(await table())).toEqual(['4']))
    expect(screen.getByRole('searchbox', { name: 'Search spools' })).toHaveValue('#4')
  })

  it('finds a spool by its hex, with or without #, by brand and by place', async () => {
    const { user } = render()
    const box = screen.getByRole('searchbox', { name: 'Search spools' })
    await table()
    for (const [query, expected] of [
      ['#FF6A13', ['2']],
      ['ff6a13', ['2']],
      ['polymaker', ['5']],
      ['dry box', ['2', '5']],
      ['teal', ['6']],
    ] as const) {
      await user.clear(box)
      await user.type(box, query)
      await waitFor(async () => expect(numbers(await table()), query).toEqual(expected))
    }
  })

  it('finds a spool by its type SKU once the products are read', async () => {
    render('/filament/spools?q=A00-W1')
    await waitFor(async () => expect(numbers(await table())).toEqual(['1']))
  })

  it('finds a spool by the tray it is in', async () => {
    render('/filament/spools?q=3dp-31b+slot')
    await waitFor(async () => expect(numbers(await table())).toEqual(['4', '8']))
  })

  it("finds a spool by its open tag's UID or tray UUID, as a reader prints it (#7151)", async () => {
    const { user } = render()
    const box = screen.getByRole('searchbox', { name: 'Search spools' })
    await table()
    for (const [query, expected] of [
      ['04:A1:B2', ['3']],
      ['7f3e1c0a', ['4']],
      ['a1b2c3d4', ['3', '4']],
    ] as const) {
      await user.clear(box)
      await user.type(box, query)
      await waitFor(async () => expect(numbers(await table()), query).toEqual(expected))
    }
    // A tag on no spool finds none.
    await user.clear(box)
    await user.type(box, '04ffee00')
    expect(await screen.findByText(/No spool matches/)).toBeInTheDocument()
  })

  it('says when nothing matches the search', async () => {
    render('/filament/spools?q=nothing-like-this')
    expect(await screen.findByText('No spool matches “nothing-like-this” with these filters.')).toBeInTheDocument()
  })

  it('puts the cursor in the search on /', async () => {
    const { user } = render()
    await table()
    await user.keyboard('/')
    expect(screen.getByRole('searchbox', { name: 'Search spools' })).toHaveFocus()
  })

  it('reads the properties again when the tab loads its spools again (#7026)', async () => {
    const { user } = render('/filament/spools?q=LOT-77')
    expect(await screen.findByText('No spool matches “LOT-77” with these filters.')).toBeInTheDocument()
    seedEntityProperties(`spool:${spoolId(3)}`, { lot: 'LOT-77' })
    await user.click(screen.getByRole('button', { name: 'Sync weights from AMS' }))
    await waitFor(async () => expect(numbers(await table())).toEqual(['3']))
  })

  it('keeps the property values it has when a reload cannot read them (#7026)', async () => {
    seedEntityProperties(`spool:${spoolId(3)}`, { lot: 'LOT-77' })
    const { user } = render('/filament/spools?q=LOT-77')
    await waitFor(async () => expect(numbers(await table())).toEqual(['3']))
    let failed = 0
    server.use(
      http.get('/api/v1/entity-kinds/:kind/properties', () => {
        failed++
        return HttpResponse.json({ detail: 'down' }, { status: 503 })
      }),
    )
    await user.click(screen.getByRole('button', { name: 'Sync weights from AMS' }))
    await waitFor(() => expect(failed).toBeGreaterThan(0))
    await new Promise((resolve) => setTimeout(resolve, 50))
    expect(numbers(await table())).toEqual(['3'])
  })
})

// #8372: a scan-made search (`from=scan`) opens its one match.

const GTIN = '6975373321234'
const SCAN = `/filament/spools?q=gtin:${GTIN}&from=scan`

function SpoolPageStub() {
  const navigate = useNavigate()
  return (
    <>
      <h1>Spool page {useParams().id}</h1>
      <button type="button" onClick={() => void navigate(-1)}>
        Back
      </button>
    </>
  )
}

function renderScan(route = SCAN) {
  const user = userEvent.setup()
  rtlRender(
    <MemoryRouter initialEntries={['/start', route]} initialIndex={1}>
      <Routes>
        <Route path="/start" element={<h1>Start</h1>} />
        <Route
          path="/filament/spools"
          element={
            <>
              <SpoolsTab />
              <Search />
            </>
          }
        />
        <Route path="/filament/spools/:id" element={<SpoolPageStub />} />
      </Routes>
    </MemoryRouter>,
  )
  return { user }
}

describe('a scan-made spool search (#8372)', () => {
  it("opens the one spool whose type has the code, in the list's place in the history", async () => {
    seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
    const { user } = renderScan()
    expect(await screen.findByRole('heading', { name: `Spool page ${spoolId(2)}` })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Back' }))
    expect(await screen.findByRole('heading', { name: 'Start' })).toBeInTheDocument()
  })

  it('lists several matches, takes from out of the address, and a later edit does not open one', async () => {
    seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
    seedEntityProperties(`spool_type:${T.PINK}`, { gtin: [GTIN] })
    const { user } = renderScan()
    await waitFor(async () => expect(numbers(await table())).toEqual(['2', '3']))
    await waitFor(() => expect(screen.getByTestId('search').textContent).toBe(`?q=gtin%3A${GTIN}`))
    await user.type(screen.getByRole('searchbox', { name: 'Search spools' }), ' pink')
    await waitFor(async () => expect(numbers(await table())).toEqual(['3']))
    expect(screen.queryByRole('heading', { name: /Spool page/ })).not.toBeInTheDocument()
  })

  it('shows an empty list with the code in the search box when nothing matches', async () => {
    renderScan()
    expect(await screen.findByText(`No spool matches “gtin:${GTIN}” with these filters.`)).toBeInTheDocument()
    expect(screen.getByRole('searchbox', { name: 'Search spools' })).toHaveValue(`gtin:${GTIN}`)
    await waitFor(() => expect(screen.getByTestId('search').textContent).toBe(`?q=gtin%3A${GTIN}`))
  })

  it('waits for the type properties before it decides', async () => {
    seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
    let release = () => {}
    const held = new Promise<void>((resolve) => (release = resolve))
    let reached = false
    server.use(
      http.get('/api/v1/entity-kinds/spool_type/properties', async () => {
        reached = true
        await held
        return undefined
      }),
    )
    renderScan()
    await waitFor(() => expect(reached).toBe(true))
    expect(await screen.findByText(`Looking for “gtin:${GTIN}”…`)).toBeInTheDocument()
    expect(screen.queryByText(/No spool matches/)).not.toBeInTheDocument()
    expect(screen.queryByRole('heading', { name: /Spool page/ })).not.toBeInTheDocument()
    expect(screen.getByTestId('search').textContent).toContain('from=scan')
    release()
    expect(await screen.findByRole('heading', { name: `Spool page ${spoolId(2)}` })).toBeInTheDocument()
  })

  it('does not open one match without from=scan', async () => {
    seedEntityProperties(`spool_type:${T.ORANGE}`, { gtin: [GTIN] })
    renderScan(`/filament/spools?q=gtin:${GTIN}`)
    await waitFor(async () => expect(numbers(await table())).toEqual(['2']))
    expect(screen.queryByRole('heading', { name: /Spool page/ })).not.toBeInTheDocument()
  })
})
