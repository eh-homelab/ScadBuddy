import { screen, waitFor, within } from '@testing-library/react'
import { useLocation } from 'react-router'
import { describe, expect, it } from 'vitest'
import { T } from '../../mocks/features/filament/seed'
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

  it('says when nothing matches, and keeps the search beside Show archived', async () => {
    const { user } = render('/filament/types?q=nothing-like-this')
    expect(await screen.findByText('No type matches “nothing-like-this”.')).toBeInTheDocument()
    await user.click(screen.getByRole('checkbox', { name: 'Show archived types' }))
    expect(screen.getByTestId('search').textContent).toBe('?q=nothing-like-this&archived=true')
  })
})
