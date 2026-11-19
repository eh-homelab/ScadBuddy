import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  METRICS_VIEW_KEY,
  metricsParams,
  metricsRange,
  parseMetricsQuery,
  rememberedMetricsQuery,
  rememberingMetrics,
  rememberMetrics,
  setRememberingMetrics,
} from './metricsQuery'

beforeEach(() => window.localStorage.clear())
afterEach(() => vi.restoreAllMocks())

describe('the Metrics tab view (#6269)', () => {
  it('defaults to 180 days, by month, by material', () => {
    expect(parseMetricsQuery(new URLSearchParams(''))).toEqual({ range: '180d', bucket: 'month', dimension: 'material' })
    expect(metricsParams({ range: '180d', bucket: 'month', dimension: 'material' }).toString()).toBe('')
  })

  it('reads every value it knows and writes only what is not the default', () => {
    const q = parseMetricsQuery(new URLSearchParams('range=90d&bucket=week&dimension=supplier'))
    expect(q).toEqual({ range: '90d', bucket: 'week', dimension: 'supplier' })
    expect(metricsParams(q).toString()).toBe('range=90d&bucket=week&dimension=supplier')
  })

  it('falls back to the default for an unknown value', () => {
    expect(parseMetricsQuery(new URLSearchParams('range=7d&bucket=day&dimension=printer'))).toEqual({ range: '180d', bucket: 'month', dimension: 'material' })
  })

  it("gives the routes the range's UTC days, both included", () => {
    expect(metricsRange({ range: '90d', bucket: 'month', dimension: 'material' }, new Date('2026-11-18T23:30:00Z'))).toEqual({ since: '2026-08-20', until: '2026-11-18' })
  })

  it('remembers the view only while "Remember my view" is on', () => {
    expect(rememberingMetrics()).toBe(false)
    rememberMetrics({ range: '90d', bucket: 'week', dimension: 'brand' })
    expect(rememberedMetricsQuery()).toBeNull()
    setRememberingMetrics(true, { range: '365d', bucket: 'month', dimension: 'type' })
    expect(rememberedMetricsQuery()).toEqual({ range: '365d', bucket: 'month', dimension: 'type' })
    rememberMetrics({ range: '90d', bucket: 'week', dimension: 'brand' })
    expect(JSON.parse(window.localStorage.getItem(METRICS_VIEW_KEY)!)).toEqual({ on: true, query: { range: '90d', bucket: 'week', dimension: 'brand' } })
    setRememberingMetrics(false, { range: '90d', bucket: 'week', dimension: 'brand' })
    expect(rememberedMetricsQuery()).toBeNull()
  })

  it('reads a stored view with an unknown value as the default for that value', () => {
    window.localStorage.setItem(METRICS_VIEW_KEY, JSON.stringify({ on: true, query: { range: 'forever', bucket: 'week', dimension: 7 } }))
    expect(rememberedMetricsQuery()).toEqual({ range: '180d', bucket: 'week', dimension: 'material' })
  })

  it('ignores a storage that throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('denied')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('denied')
    })
    expect(rememberingMetrics()).toBe(false)
    expect(rememberedMetricsQuery()).toBeNull()
    expect(() => rememberMetrics({ range: '90d', bucket: 'week', dimension: 'brand' })).not.toThrow()
    expect(() => setRememberingMetrics(true, { range: '90d', bucket: 'week', dimension: 'brand' })).not.toThrow()
  })
})
