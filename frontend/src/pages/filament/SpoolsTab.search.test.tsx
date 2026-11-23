import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
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
})
