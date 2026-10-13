import { screen, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import { server } from '../../mocks/server'
import { renderPage } from '../../test/utils'
import { SpoolInventoryPanel } from './SpoolInventoryPanel'

afterEach(() => {
  resetBambuddyLinks()
})

describe('SpoolInventoryPanel (#2351)', () => {
  it('lists every spool, loaded first, with what is left, its price and where it is', async () => {
    setBambuddyLinks({ bambuddy_url: 'http://bambuddy.local', bambuddy_web_urls: null })
    renderPage(<SpoolInventoryPanel />)

    const table = await screen.findByRole('table')
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((row) => row.dataset.testid)).toEqual(['spool-15', 'spool-2', 'spool-1'])
    const clear = screen.getByTestId('spool-15')
    expect(clear).toHaveTextContent('Insignia PETG — Clear')
    expect(clear).toHaveTextContent('400 / 1000 g')
    expect(clear).toHaveTextContent('15.96')
    expect(clear).toHaveTextContent('3DP-31B-598 · AMS-A · slot 4')
    const green = screen.getByTestId('spool-1')
    // No price in Bambuddy: none shown, never estimated.
    expect(green).toHaveTextContent('—')
    expect(green).toHaveTextContent('Dry box 2')
    expect(screen.getByTestId('spool-2')).toHaveTextContent('On the shelf')
    expect(screen.getByTestId('spool-inventory-total')).toHaveTextContent('3 spools, 2.33 kg left')
    expect(screen.getByRole('link', { name: 'Manage in Bambuddy' })).toHaveAttribute(
      'href',
      'http://bambuddy.local/inventory',
    )
  })

  it('lists archived spools only when asked, without counting them', async () => {
    const { user } = renderPage(<SpoolInventoryPanel />)
    await screen.findByTestId('spool-15')
    expect(screen.queryByTestId('spool-30')).toBeNull()

    await user.click(screen.getByRole('checkbox', { name: 'Include archived spools' }))
    expect(await screen.findByTestId('spool-30')).toHaveTextContent('(archived)')
    expect(screen.getByTestId('spool-inventory-total')).toHaveTextContent('3 spools, 2.33 kg left')
  })

  it('says a failed read', async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () =>
        HttpResponse.json({ detail: "Bambuddy refused the API key: it needs 'Read Status'" }, { status: 409 }),
      ),
    )
    renderPage(<SpoolInventoryPanel />)
    expect(await screen.findByTestId('spool-inventory-error')).toHaveTextContent('Read Status')
  })
})
