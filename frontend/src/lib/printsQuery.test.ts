import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  DEFAULT_PRINTS_QUERY,
  PRINTS_VIEW_KEY,
  apiFilters,
  clearPrintFilters,
  isFiltered,
  parsePrintsQuery,
  readStoredPrintsView,
  resetStoredPrintsView,
  storePrintsView,
  toPrintsParams,
} from './printsQuery'

describe('print history URL state (#310)', () => {
  it('parses every filter, and ignores what it does not know', () => {
    const query = parsePrintsQuery(
      new URLSearchParams('slug=name-keychain&status=failed&printer=2&from=2026-09-01&to=2026-09-30&q=nova&view=list'),
    )
    expect(query).toEqual({
      slug: 'name-keychain',
      status: 'failed',
      printer: '2',
      from: '2026-09-01',
      to: '2026-09-30',
      q: 'nova',
      view: 'list',
    })
    expect(
      parsePrintsQuery(new URLSearchParams('printer=abc&from=yesterday&to=2026-13-40&view=grid')),
    ).toEqual(DEFAULT_PRINTS_QUERY)
  })

  it('takes only a printer id a JavaScript number carries exactly', () => {
    expect(parsePrintsQuery(new URLSearchParams('printer=999999999999999')).printer).toBe('999999999999999')
    expect(parsePrintsQuery(new URLSearchParams('printer=9999999999999999')).printer).toBe('')
    expect(apiFilters(parsePrintsQuery(new URLSearchParams('printer=999999999999999')))).toEqual({
      printer_id: 999999999999999,
    })
  })

  it('writes only what differs from the defaults, and round-trips', () => {
    expect(toPrintsParams(DEFAULT_PRINTS_QUERY).toString()).toBe('')
    const query = { ...DEFAULT_PRINTS_QUERY, status: 'completed', printer: '1', view: 'list' as const }
    const params = toPrintsParams(query)
    expect(params.toString()).toBe('status=completed&printer=1&view=list')
    expect(parsePrintsQuery(params)).toEqual(query)
  })

  it('clears the filters but keeps the view', () => {
    const query = { ...DEFAULT_PRINTS_QUERY, slug: 'x', status: 'failed', q: 'a', view: 'list' as const }
    expect(clearPrintFilters(query)).toEqual({ ...DEFAULT_PRINTS_QUERY, view: 'list' })
    expect(isFiltered(query)).toBe(true)
    expect(isFiltered(clearPrintFilters(query))).toBe(false)
  })

  it('maps the URL onto the API’s parameters, with the template fixed on its tab', () => {
    const query = { ...DEFAULT_PRINTS_QUERY, slug: 'other', printer: '2', from: '2026-09-01', q: '  ' }
    expect(apiFilters(query)).toEqual({ slug: 'other', printer_id: 2, from: '2026-09-01' })
    expect(apiFilters(query, 'name-keychain')).toEqual({
      slug: 'name-keychain',
      printer_id: 2,
      from: '2026-09-01',
    })
  })
})

describe('the remembered print view', () => {
  afterEach(() => {
    vi.restoreAllMocks()
    resetStoredPrintsView()
    localStorage.clear()
  })

  it('is kept in localStorage', () => {
    expect(readStoredPrintsView()).toBeNull()
    storePrintsView('list')
    expect(localStorage.getItem(PRINTS_VIEW_KEY)).toBe('list')
    expect(readStoredPrintsView()).toBe('list')
  })

  it('is kept in memory where storage throws, as in a sandboxed iframe', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError')
    })
    expect(readStoredPrintsView()).toBeNull()
    storePrintsView('list')
    expect(readStoredPrintsView()).toBe('list')
  })
})
