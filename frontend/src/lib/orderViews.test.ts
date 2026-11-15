import { afterEach, describe, expect, it } from 'vitest'
import { DEFAULT_ORDERS_QUERY, ordersApiParams, parseOrdersQuery, toOrdersParams } from './ordersQuery'
import { applyView, DEFAULT_HIDE_DAYS, endedSince, ORDER_VIEWS, ORDERS_MEMORY_KEY, readOrdersMemory, resetOrdersMemory, storeOrdersMemory, viewOf } from './orderViews'

const view = (key: string) => {
  const found = ORDER_VIEWS.find((v) => v.key === key)
  if (!found) throw new Error(key)
  return found
}
const apiOf = (key: string) => ordersApiParams(applyView(DEFAULT_ORDERS_QUERY, view(key))).toString()

describe('order views (#6334)', () => {
  it('asks the API for each view by its rule', () => {
    expect(apiOf('all')).toBe('')
    expect(apiOf('needs_action')).toBe('status=draft&status=accepted&status=ready')
    expect(apiOf('in_progress')).toBe('status=accepted&status=in_production')
    expect(apiOf('ready')).toBe('status=ready')
    expect(apiOf('owing')).toBe('owing=true')
    expect(apiOf('closed')).toBe('status=closed&status=cancelled')
  })

  it('keeps the search and old across views, and clears every other filter', () => {
    const query = { ...DEFAULT_ORDERS_QUERY, q: 'ada', old: true, customer: 'c1', margin: 'loss_thin' }
    const next = applyView(query, view('ready'))
    expect(toOrdersParams(next).toString()).toBe('status=ready&q=ada&old=1')
  })

  it('names the view the URL holds, and none for a query of its own', () => {
    expect(viewOf(DEFAULT_ORDERS_QUERY)?.key).toBe('all')
    expect(viewOf(parseOrdersQuery(new URLSearchParams('status=draft,accepted,ready&q=x&old=1')))?.key).toBe('needs_action')
    expect(viewOf(parseOrdersQuery(new URLSearchParams('owing=1')))?.key).toBe('owing')
    expect(viewOf(parseOrdersQuery(new URLSearchParams('status=ready&customer=c1')))).toBeNull()
    expect(viewOf(parseOrdersQuery(new URLSearchParams('status=quoted')))).toBeNull()
  })

  it('hides ended orders from local midnight N days back', () => {
    const now = new Date(2026, 10, 15, 17, 30)
    expect(endedSince(30, now)).toBe(new Date(2026, 9, 16).toISOString())
    // The same all day, so the list is not read again on every render.
    expect(endedSince(30, new Date(2026, 10, 15, 0, 1))).toBe(endedSince(30, now))
    expect(ordersApiParams({ ...DEFAULT_ORDERS_QUERY, ended_since: endedSince(7, now) }).get('ended_since')).toBe(new Date(2026, 10, 8).toISOString())
  })
})

describe('orders memory (#6334)', () => {
  afterEach(() => {
    window.localStorage.clear()
    resetOrdersMemory()
  })

  it('defaults to 30 days, remembering, All, cancelled items closed', () => {
    expect(readOrdersMemory()).toEqual({ hideDays: DEFAULT_HIDE_DAYS, remember: true, query: '', itemGroups: { fulfilled: true, cancelled: false } })
  })

  it('keeps a change and reads back a bad value as the default', () => {
    storeOrdersMemory({ hideDays: 7, query: 'status=ready' })
    expect(readOrdersMemory()).toMatchObject({ hideDays: 7, query: 'status=ready', remember: true })
    window.localStorage.setItem(ORDERS_MEMORY_KEY, JSON.stringify({ hideDays: 3, remember: false }))
    expect(readOrdersMemory()).toMatchObject({ hideDays: DEFAULT_HIDE_DAYS, remember: false })
    window.localStorage.setItem(ORDERS_MEMORY_KEY, '{nope')
    expect(readOrdersMemory().hideDays).toBe(DEFAULT_HIDE_DAYS)
  })
})
