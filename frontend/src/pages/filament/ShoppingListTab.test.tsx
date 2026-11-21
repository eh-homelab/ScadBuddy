import { screen, waitFor, within } from '@testing-library/react'
import { http, HttpResponse } from 'msw'
import { beforeEach, describe, expect, it } from 'vitest'
import { listShopping } from '../../api/filamentPurchasing'
import { spoolId, T } from '../../mocks/features/filament/seed'
import { ITEM } from '../../mocks/features/filamentPurchasingSeed'
import { server } from '../../mocks/server'
import { choose } from '../../test/select'
import { renderPage } from '../../test/utils'
import { QUANTITY_KEY, ShoppingListTab } from './ShoppingListTab'

const table = () => screen.findByRole('table', { name: 'Shopping list' })
const rowOf = (id: string) => within(screen.getByRole('table', { name: 'Shopping list' })).getAllByRole('row').find((row) => row.dataset.item === id)!
const order = () => within(screen.getByRole('table', { name: 'Shopping list' })).getAllByRole('row').slice(1).map((row) => row.dataset.item)

/** Every request with `method` to the shopping list, read as it leaves. */
function recorded(method: string) {
  const seen: { path: string; search: string; body: unknown }[] = []
  server.events.on('request:start', ({ request }) => {
    const url = new URL(request.url)
    if (request.method !== method || !url.pathname.includes('/filament/shopping-list')) return
    void request.clone().text().then((text) => seen.push({ path: url.pathname, search: url.search, body: text ? JSON.parse(text) : null }))
  })
  return seen
}

beforeEach(() => {
  window.localStorage.clear()
})

describe('the Shopping list tab', () => {
  it('lists the items, newest pending first', async () => {
    renderPage(<ShoppingListTab />)
    await table()
    expect(order()).toEqual([ITEM.TPU, ITEM.ORANGE, ITEM.PINK, ITEM.CLEAR])
    const pink = rowOf(ITEM.PINK)
    expect(within(pink).getByText('Hot Pink')).toBeInTheDocument()
    expect(within(pink).getByText('Pending')).toBeInTheDocument()
    expect(within(rowOf(ITEM.CLEAR)).getByText('Purchased')).toBeInTheDocument()
  })

  it('marks an item an empty spool added, and links its note to that spool (#6868)', async () => {
    renderPage(<ShoppingListTab />)
    await table()
    const pink = rowOf(ITEM.PINK)
    expect(within(pink).getByText('Auto')).toHaveAccessibleDescription('Added when a spool ran out or was archived')
    expect(within(pink).getByRole('link', { name: 'Spool #3 ran out on 2026-11-14 18:42 UTC.' })).toHaveAttribute('href', expect.stringContaining(`/filament/spools/${spoolId(3)}`))
    expect(within(rowOf(ITEM.ORANGE)).queryByText('Auto')).not.toBeInTheDocument()
    expect(screen.getByText(/A spool that runs out adds its type here/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Settings' })).toHaveAttribute('href', expect.stringContaining('/settings/printing#filament'))
  })

  it('adds an item from a type with its quantity', async () => {
    const posted = recorded('POST')
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await choose(user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    await user.clear(screen.getByLabelText('Spools'))
    await user.type(screen.getByLabelText('Spools'), '2')
    await user.click(screen.getByRole('button', { name: 'Add to list' }))
    await waitFor(() => expect(posted.map((one) => one.body)).toEqual([{ type_id: T.JADE, quantity_spools: 2 }]))
    await waitFor(() => expect(order()).toHaveLength(5))
  })

  it('adds an item typed by hand when no type is chosen', async () => {
    const posted = recorded('POST')
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await user.type(screen.getByLabelText('Material'), 'ASA')
    await user.type(screen.getByLabelText('Colour name'), 'Grey')
    await user.click(screen.getByRole('button', { name: 'Add to list' }))
    await waitFor(() => expect(posted.map((one) => one.body)).toEqual([{ material: 'ASA', colour_name: 'Grey', quantity_spools: 1 }]))
  })

  it('refuses a quantity of 0 inline and sends nothing', async () => {
    const posted = recorded('POST')
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await choose(user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    await user.clear(screen.getByLabelText('Spools'))
    await user.type(screen.getByLabelText('Spools'), '0')
    await user.click(screen.getByRole('button', { name: 'Add to list' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Enter a whole number of spools, from 1 to 1000.')
    expect(posted).toEqual([])
    expect(await listShopping()).toHaveLength(4)
  })

  it('remembers the last quantity', async () => {
    const first = renderPage(<ShoppingListTab />)
    await table()
    await choose(first.user, await screen.findByRole('combobox', { name: 'Type' }), /BL-PLA-BASIC-JADE-WHITE/)
    await first.user.clear(screen.getByLabelText('Spools'))
    await first.user.type(screen.getByLabelText('Spools'), '3')
    await first.user.click(screen.getByRole('button', { name: 'Add to list' }))
    await waitFor(() => expect(order()).toHaveLength(5))
    first.unmount()
    renderPage(<ShoppingListTab />)
    await table()
    expect(screen.getByLabelText('Spools')).toHaveValue('3')
    expect(window.localStorage.getItem(QUANTITY_KEY)).toBe('3')
  })

  it('marks an item purchased and moves it below the pending ones', async () => {
    const patched = recorded('PATCH')
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await user.click(within(rowOf(ITEM.PINK)).getByRole('button', { name: 'Mark Hot Pink PLA purchased' }))
    await waitFor(() => expect(patched.map((one) => one.body)).toEqual([{ status: 'purchased' }]))
    await waitFor(() => expect(order()).toEqual([ITEM.TPU, ITEM.ORANGE, ITEM.CLEAR, ITEM.PINK]))
    expect(within(rowOf(ITEM.PINK)).getByText('Purchased')).toBeInTheDocument()
  })

  it('removes one item', async () => {
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await user.click(within(rowOf(ITEM.TPU)).getByRole('button', { name: 'Remove White TPU' }))
    await waitFor(() => expect(order()).toEqual([ITEM.ORANGE, ITEM.PINK, ITEM.CLEAR]))
  })

  it('clears the purchased items after a confirmation', async () => {
    const deleted = recorded('DELETE')
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await user.click(screen.getByRole('button', { name: 'Clear purchased' }))
    expect(deleted).toEqual([])
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Clear purchased' }))
    await waitFor(() => expect(deleted).toEqual([{ path: expect.stringMatching(/shopping-list$/), search: '?purchased_only=true', body: null }]))
    await waitFor(() => expect(order()).toEqual([ITEM.TPU, ITEM.ORANGE, ITEM.PINK]))
  })

  it('clears the whole list after a confirmation', async () => {
    const { user } = renderPage(<ShoppingListTab />)
    await table()
    await user.click(screen.getByRole('button', { name: 'Clear all' }))
    await user.click(within(await screen.findByRole('alertdialog')).getByRole('button', { name: 'Clear all' }))
    expect(await screen.findByText('Nothing to buy. Add what you need next.')).toBeInTheDocument()
    expect(await listShopping()).toEqual([])
  })

  describe('when a request fails', () => {
    const refused = (status: number, detail: string) => HttpResponse.json({ type: 'about:blank', title: 'Refused', status, detail }, { status, headers: { 'Content-Type': 'application/problem+json' } })

    it('says the list could not be loaded, with the server\'s words', async () => {
      server.use(http.get('*/api/v1/filament/shopping-list', () => refused(500, 'The database is busy')))
      renderPage(<ShoppingListTab />)
      expect(await screen.findByRole('alert')).toHaveTextContent('The database is busy')
      expect(screen.queryByRole('table', { name: 'Shopping list' })).not.toBeInTheDocument()
    })

    it('keeps the item as it was when a change is refused, and says why', async () => {
      server.use(http.patch('*/api/v1/filament/shopping-list/:id', () => refused(500, 'The item could not be saved')))
      const { user } = renderPage(<ShoppingListTab />)
      await table()
      await user.click(within(rowOf(ITEM.PINK)).getByRole('button', { name: 'Mark Hot Pink PLA purchased' }))
      expect(await screen.findByRole('alert')).toHaveTextContent('The item could not be saved')
      expect(order()).toEqual([ITEM.TPU, ITEM.ORANGE, ITEM.PINK, ITEM.CLEAR])
      expect(within(rowOf(ITEM.PINK)).queryByText('Purchased')).not.toBeInTheDocument()
    })
  })
})
