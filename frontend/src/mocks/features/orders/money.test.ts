import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../../api/client'
import { mockSettings, resetMockState, setMockSettings } from '../../handlers'
import { depositFor } from './money'
import { ORDER_DRAFT, state } from './state'

const autoTax = () => state.charges.filter((c) => c.order_id === ORDER_DRAFT && c.auto).map((c) => [c.label, c.amount_minor])

describe('the mock money rules (#5281, `orders/money.py`)', () => {
  beforeEach(() => resetMockState())

  it('rounds the auto tax half up once: 10 % of 10.05 is 1.01', async () => {
    setMockSettings({ ...mockSettings(), orders_tax_percent: '10.0', orders_tax_shipping: true })
    await api.updateOrder(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 1005 }] })
    expect(autoTax()).toEqual([['Tax 10%', 101]])
  })

  it('drops the auto row while a typed tax stands, and keeps it on a whole replace', async () => {
    setMockSettings({ ...mockSettings(), orders_tax_percent: '10', orders_tax_shipping: true })
    await api.updateOrder(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 1000 }] })
    const auto = state.charges.find((c) => c.order_id === ORDER_DRAFT && c.auto)!
    // Sent back as read, with the rest: still the auto row, recomputed.
    await api.updateOrder(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 2000 }, { kind: 'tax', label: auto.label, amount_minor: 1, id: auto.id, auto: true }] })
    expect(autoTax()).toEqual([['Tax 10%', 200]])
    await api.updateOrder(ORDER_DRAFT, { charges: [{ kind: 'shipping', label: 'Post', amount_minor: 2000 }, { kind: 'tax', label: 'VAT', amount_minor: 300 }] })
    expect(autoTax()).toEqual([])
  })

  it('takes the largest rule that applies', () => {
    const at = { total: 10000, hasRequest: true, firstOrder: true, currency: 'USD', shopCurrency: 'USD' }
    expect(depositFor([{ criterion: 'has_request', amount_minor: 2000 }, { criterion: 'first_order', percent: '25' }, { criterion: 'total_above', above_minor: 5000, percent: '50' }], at)).toBe(5000)
    // In another currency only a percent applies.
    expect(depositFor([{ criterion: 'has_request', amount_minor: 2000 }, { criterion: 'first_order', percent: '25' }], { ...at, currency: 'EUR' })).toBe(2500)
    expect(depositFor([], at)).toBeNull()
  })
})
