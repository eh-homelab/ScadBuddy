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

  it('names the part not yet accepted, and calls the balance provisional', () => {
    render(<OrderSummary currency="USD" exponent={2} totals={totals({ pending_minor: 1250 })} charges={[]} />)
    expect(lines()).toContainEqual(['of which not accepted', '$12.50'])
    expect(lines().at(-1)?.[0]).toBe('Balance due (provisional)')
  })

  it('says when there are no charges', () => {
    render(<OrderSummary currency="USD" exponent={2} totals={totals({ charges_minor: 0 })} charges={[]} />)
    expect(lines().map(([term]) => term)).toEqual(['Subtotal', 'Total', 'Paid', 'Balance due'])
  })
})
