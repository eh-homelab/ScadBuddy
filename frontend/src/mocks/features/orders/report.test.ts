import { beforeEach, describe, expect, it } from 'vitest'
import { api } from '../../../api/client'
import { DEFAULT_ORDERS_QUERY } from '../../../lib/ordersQuery'
import { resetMockState } from '../../handlers'
import { ITEM_APPROVED, ORDER_ACCEPTED, state } from './state'

const PERIOD = { from: '2026-10-01T00:00:00Z', to: '2026-11-01T00:00:00Z' }

describe('profit report mock (#5329)', () => {
  beforeEach(() => resetMockState())

  it('puts an empty SKU in the custom row, and the custom filter lists its order', async () => {
    const item = state.items.find((i) => i.id === ITEM_APPROVED)
    if (!item) throw new Error('the mocks hold no approved item')
    Object.assign(item, { kind: 'custom', reference: null, sku: '' })

    const report = await api.profitReport(PERIOD)
    const custom = report.rows.find((r) => r.group === 'custom')
    expect(custom?.product).toBe('custom')
    expect(custom?.orders).toBe(1)

    const listed = await api.orders({ ...DEFAULT_ORDERS_QUERY, product: 'custom', accepted_from: report.period_from, accepted_to: report.period_to, currency: 'USD' })
    expect(listed.items.map((o) => o.id)).toEqual([ORDER_ACCEPTED])
  })

  it('counts in the order row only the orders with figures of their own', async () => {
    // The accepted order's shipping charge goes: only the closed order's shipment cost is left.
    state.charges = state.charges.filter((c) => c.order_id !== ORDER_ACCEPTED)
    const report = await api.profitReport(PERIOD)
    const own = report.rows.find((r) => r.group === 'order')
    const total = report.rows.find((r) => r.group === 'total')
    expect(own?.orders).toBe(1)
    expect(total?.orders).toBe(2)
  })
})
