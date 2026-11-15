import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import type { OrderCharge, OrderTotals } from '../../api/types'
import { OrderSummary } from './OrderSummary'

// #4955 step 4, spec 2026-11-07 §4.1 — the summary card: subtotal, each charge on its own line
// (shipping, discount, fee, tax), total, paid and balance.

const totals = (fields: Partial<OrderTotals> = {}) =>
  ({ subtotal_minor: 4800, pending_minor: 0, charges_minor: 884, total_minor: 5684, paid_minor: 2000, balance_minor: 3684, unpriced_items: [], ...fields }) as OrderTotals
const charge = (kind: OrderCharge['kind'], label: string, amount: number, position: number) =>
  ({ id: `c${position}`, order_id: 'o1', kind, label, amount_minor: amount, position }) as OrderCharge

function lines() {
  const summary = screen.getByTestId('order-totals')
  return within(summary)
    .getAllByRole('term')
    .map((term) => [term.textContent, term.nextElementSibling?.textContent])
}

describe('OrderSummary (#4955)', () => {
  it('lists the subtotal, each charge, the total, paid and the balance', () => {
    render(
      <OrderSummary
        currency="USD"
        exponent={2}
        totals={totals()}
        charges={[charge('shipping', 'Post', 500, 0), charge('discount', 'Repeat customer', -100, 1), charge('tax', 'Sales tax', 484, 2)]}
      />,
    )
    expect(lines()).toEqual([
      ['Subtotal', '$48.00'],
      ['Shipping · Post', '$5.00'],
      ['Discount · Repeat customer', '-$1.00'],
      ['Tax · Sales tax', '$4.84'],
      ['Total', '$56.84'],
      ['Paid', '$20.00'],
      ['Balance due', '$36.84'],
    ])
  })

  it('lists the charges as store.charges does: typed, purchases, handling, auto tax (#6133)', () => {
    const auto = { ...charge('tax', 'Sales tax', 100, 0), id: 'auto', auto: true }
    const handling = { ...charge('handling', 'Handling for bought items', 300, 0), id: 'h' }
    const bought = { ...charge('purchase', 'Bought for your project: magnets', 899, 0), id: 'p' }
    render(
      <OrderSummary
        currency="USD"
        exponent={2}
        totals={totals()}
        charges={[auto, handling, bought, { ...charge('shipping', 'Post', 500, 1), auto: false }, { ...charge('fee', 'Rush', 200, 0), auto: false }]}
      />,
    )
    expect(lines().slice(1, 6).map(([term]) => term)).toEqual([
      'Fee · Rush',
      'Shipping · Post',
      'Bought for the customer · Bought for your project: magnets',
      'Handling · Handling for bought items',
      'Tax · Sales tax',
    ])
  })

  it('names the part not yet accepted, and calls the balance provisional', () => {
    render(<OrderSummary currency="USD" exponent={2} totals={totals({ pending_minor: 1250 })} charges={[]} />)
    expect(lines()).toContainEqual(['of which not accepted', '$12.50'])
    expect(lines().at(-1)?.[0]).toBe('Balance due (provisional)')
  })

  it('shows a badge beside a charge label when the caller gives one', () => {
    render(
      <OrderSummary
        currency="USD"
        exponent={2}
        totals={totals()}
        charges={[charge('shipping', 'Post', 500, 0), charge('tax', 'Sales tax', 484, 1)]}
        chargeBadge={(c) => (c.kind === 'tax' ? <span data-testid="badge">auto</span> : null)}
      />,
    )
    const term = screen.getByText('Tax · Sales tax').closest('dt')!
    expect(within(term).getByTestId('badge')).toHaveTextContent('auto')
    expect(screen.getAllByTestId('badge')).toHaveLength(1)
  })

  it('says when there are no charges', () => {
    render(<OrderSummary currency="USD" exponent={2} totals={totals({ charges_minor: 0 })} charges={[]} />)
    expect(lines().map(([term]) => term)).toEqual(['Subtotal', 'Total', 'Paid', 'Balance due'])
  })
})
