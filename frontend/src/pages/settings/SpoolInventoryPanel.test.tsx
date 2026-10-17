import { screen, waitFor, within } from '@testing-library/react'
import { HttpResponse, http } from 'msw'
import { afterEach, describe, expect, it } from 'vitest'
import { resetBambuddyLinks, setBambuddyLinks } from '../../lib/bambuddyLinks'
import { SPOOL_INVENTORY } from '../../mocks/features/spoolInventory'
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

    await user.click(screen.getByRole('switch', { name: 'Include archived spools' }))
    expect(await screen.findByTestId('spool-30')).toHaveTextContent('(archived)')
    expect(screen.getByTestId('spool-inventory-total')).toHaveTextContent('3 spools, 2.33 kg left')
    // Greyed out, and sorted with the rest rather than put last (#2923).
    expect(screen.getByTestId('spool-30')).toHaveClass('text-faint')
    const rows = within(screen.getByRole('table')).getAllByRole('row').slice(1)
    expect(rows.map((row) => row.dataset.testid)).toEqual(['spool-15', 'spool-30', 'spool-2', 'spool-1'])
  })

  it('shows no old table under the error when a re-read fails (#2923)', async () => {
    const { user } = renderPage(<SpoolInventoryPanel />)
    await screen.findByTestId('spool-15')
    server.use(
      http.get('/api/v1/farm/inventory', () => HttpResponse.json({ detail: 'Bambuddy timed out' }, { status: 504 })),
    )

    await user.click(screen.getByRole('switch', { name: 'Include archived spools' }))
    expect(await screen.findByTestId('spool-inventory-error')).toHaveTextContent('Bambuddy timed out')
    expect(screen.queryByRole('table')).toBeNull()
  })

  it('says, without a warning, that Bambuddy is not connected yet (#2923)', async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/bambuddy-not-configured',
            title: 'Conflict',
            status: 409,
            detail: 'no Bambuddy URL is configured; set one in Settings',
          },
          { status: 409, headers: { 'Content-Type': 'application/problem+json' } },
        ),
      ),
    )
    renderPage(<SpoolInventoryPanel />)
    const note = await screen.findByTestId('spool-inventory-unconfigured')
    expect(note).toHaveClass('text-muted-foreground')
    expect(screen.queryByTestId('spool-inventory-error')).toBeNull()
  })

  it("draws a clear spool's chip as clear, never black, and names it as the print dialog does (#2924)", async () => {
    renderPage(<SpoolInventoryPanel />)
    const clear = await screen.findByTestId('spool-15')
    expect(within(clear).getByTestId('clear-swatch')).toBeInTheDocument()
    expect(clear.querySelector('[data-colour="#000000"]')).toBeNull()
    expect(screen.getByTestId('spool-2').querySelector('[data-colour="#F5547C"]')).not.toBeNull()
  })

  it('keeps the last table while a re-read is in flight, then shows the new one (#2971 review)', async () => {
    const { user } = renderPage(<SpoolInventoryPanel />)
    await screen.findByTestId('spool-15')
    let answer = () => {}
    server.use(
      http.get('/api/v1/farm/inventory', async () => {
        await new Promise<void>((resolve) => (answer = resolve))
        return HttpResponse.json({ spools: [], slots: [] })
      }),
    )

    await user.click(screen.getByRole('switch', { name: 'Include archived spools' }))
    expect(screen.getByTestId('spool-15')).toBeInTheDocument()
    expect(screen.queryByText("Reading Bambuddy's inventory…")).toBeNull()
    // Marked as the last selection's while the re-read is out (#3357), the total with the
    // table (#3486 review).
    const result = screen.getByTestId('spool-inventory-result')
    expect(result).toHaveAttribute('aria-busy', 'true')
    expect(result).toContainElement(screen.getByTestId('spool-inventory-total'))
    expect(result).toContainElement(screen.getByRole('table'))

    answer()
    expect(await screen.findByText('Bambuddy has no spools.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).toBeNull()
    expect(screen.getByTestId('spool-inventory-result')).toHaveAttribute('aria-busy', 'false')

    // An empty answer is the last selection's too while the next re-read is out.
    server.use(
      http.get('/api/v1/farm/inventory', async () => {
        await new Promise<void>((resolve) => (answer = resolve))
        return HttpResponse.json({ spools: [], slots: [] })
      }),
    )
    await user.click(screen.getByRole('switch', { name: 'Include archived spools' }))
    expect(screen.getByTestId('spool-inventory-result')).toContainElement(screen.getByText('Bambuddy has no spools.'))
    expect(screen.getByTestId('spool-inventory-result')).toHaveAttribute('aria-busy', 'true')
    answer()
    await waitFor(() => expect(screen.getByTestId('spool-inventory-result')).toHaveAttribute('aria-busy', 'false'))
  })

  it("clears a failed read's error as soon as a toggle reads again (#3357)", async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () => HttpResponse.json({ detail: 'Bambuddy timed out' }, { status: 504 })),
    )
    const { user } = renderPage(<SpoolInventoryPanel />)
    expect(await screen.findByTestId('spool-inventory-error')).toHaveTextContent('Bambuddy timed out')
    let answer = () => {}
    server.use(
      http.get('/api/v1/farm/inventory', async () => {
        await new Promise<void>((resolve) => (answer = resolve))
        return HttpResponse.json({ spools: [], slots: [] })
      }),
    )

    await user.click(screen.getByRole('switch', { name: 'Include archived spools' }))
    // The old error is not this read's, so it goes while the new read is out.
    expect(screen.queryByTestId('spool-inventory-error')).toBeNull()
    expect(screen.getByText("Reading Bambuddy's inventory…")).toBeInTheDocument()

    answer()
    expect(await screen.findByText('Bambuddy has no spools.')).toBeInTheDocument()
  })

  it('draws a spool with no colour recorded as unknown, not clear and not black (#2971 review)', async () => {
    server.use(
      http.get('/api/v1/farm/inventory', () =>
        HttpResponse.json({
          spools: [
            { ...SPOOL_INVENTORY[0], id: 40, rgba: null },
            { ...SPOOL_INVENTORY[0], id: 41, rgba: 'not a colour' },
          ],
          slots: [],
        }),
      ),
    )
    renderPage(<SpoolInventoryPanel />)
    for (const id of [40, 41]) {
      const row = await screen.findByTestId(`spool-${id}`)
      expect(within(row).getByTestId('unknown-swatch')).toHaveAttribute('data-colour', 'colour not recorded')
      expect(within(row).queryByTestId('clear-swatch')).toBeNull()
      expect(row.querySelector('[data-colour="#000000"]')).toBeNull()
    }
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
