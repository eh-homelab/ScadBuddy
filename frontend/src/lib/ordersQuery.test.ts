import { describe, expect, it } from 'vitest'
import { DEFAULT_ORDERS_QUERY, isOrdersFiltered, isReportFiltered, ordersApiParams, parseOrdersQuery, toOrdersParams } from './ordersQuery'

describe('ordersQuery (#4249)', () => {
  it('round-trips through the URL, defaults left out', () => {
    const q = parseOrdersQuery(new URLSearchParams('status=quoted&customer=c1&from=2026-10-01'))
    expect(q).toEqual({ ...DEFAULT_ORDERS_QUERY, status: 'quoted', customer: 'c1', from: '2026-10-01' })
    expect(toOrdersParams(q).toString()).toBe('status=quoted&customer=c1&from=2026-10-01')
    expect(isOrdersFiltered(q)).toBe(true)
    expect(isOrdersFiltered(parseOrdersQuery(new URLSearchParams('')))).toBe(false)
  })
  it('maps to the API names and trims q', () => {
    const q = parseOrdersQuery(new URLSearchParams('customer=c1&q=%20ada%20'))
    expect(ordersApiParams(q).toString()).toBe('customer_id=c1&q=ada')
    expect(toOrdersParams(q).get('q')).toBe('ada')
  })
  it('asks for loss and thin margins for "Loss or thin" (#5327)', () => {
    const q = parseOrdersQuery(new URLSearchParams('margin=loss_thin'))
    expect(isOrdersFiltered(q)).toBe(true)
    expect(ordersApiParams(q).toString()).toBe('margin_state=loss&margin_state=thin')
    expect(ordersApiParams({ ...q, margin: 'nope' }).toString()).toBe('')
  })
  it('passes a profit report row\'s filters as they are (#5329)', () => {
    const q = parseOrdersQuery(new URLSearchParams('product=model%3Ademo&accepted_from=2026-10-01T00%3A00%3A00Z&accepted_to=2026-11-01T00%3A00%3A00Z&currency=USD'))
    expect(isReportFiltered(q)).toBe(true)
    expect(isReportFiltered(DEFAULT_ORDERS_QUERY)).toBe(false)
    const api = ordersApiParams(q)
    expect([api.get('product'), api.get('accepted_from'), api.get('accepted_to'), api.get('currency')]).toEqual(['model:demo', '2026-10-01T00:00:00Z', '2026-11-01T00:00:00Z', 'USD'])
  })
})
