import { screen, within } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { T } from '../../../mocks/features/filament/seed'
import { INVOICE, LINE, PRODUCT, SUPPLIER } from '../../../mocks/features/filamentPurchasingSeed'
import { renderPage } from '../../../test/utils'
import { PricePanel } from './PricePanel'

describe('the price panel', () => {
  it('offers "Use as default cost" for a price in the shop currency', async () => {
    const use = vi.fn()
    const { user } = renderPage(<PricePanel typeId={T.JADE} currency="USD" exponent={2} onUseAsCost={use} />)
    const button = await screen.findByRole('button', { name: /Use as default cost from/ })
    expect(button).toBeEnabled()
    await user.click(button)
    expect(use).toHaveBeenCalledTimes(1)
  })

  it('does not offer a price in another currency as the default cost', async () => {
    const use = vi.fn()
    renderPage(<PricePanel typeId={T.JADE} currency="EUR" exponent={2} onUseAsCost={use} />)
    const button = await screen.findByRole('button', { name: /Use as default cost from/ })
    expect(button).toBeDisabled()
    expect(button).toHaveAttribute('title', expect.stringContaining('USD'))
  })

  it("links the supplier, the product and an invoice's price to their pages (#6869)", async () => {
    renderPage(<PricePanel typeId={T.JADE} currency="USD" exponent={2} onUseAsCost={vi.fn()} />)
    const history = await screen.findByRole('table', { name: /Price history of .* A00-W1/ })
    const product = history.closest('article')!
    expect(within(product).getAllByRole('link')[0]).toHaveAttribute('href', `/filament/suppliers/${SUPPLIER.BAMBU}`)
    expect(within(product).getByRole('link', { name: 'A00-W1' })).toHaveAttribute('href', `/filament/suppliers/${SUPPLIER.BAMBU}#product-${PRODUCT.JADE}`)
    expect(within(history).getByRole('link', { name: 'Invoice' })).toHaveAttribute('href', `/filament/invoices/${INVOICE.CONFIRMED_A}#line-${LINE.CONFIRMED_A1}`)
    expect(within(history).getAllByText('Added by hand').length).toBeGreaterThan(0)
  })
})
