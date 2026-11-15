/**
 * #6157 — the Storefront list's search, filters, sort and page, kept in the URL
 * (`?q=&status=&model=&material=&sort=&dir=&page=`), so back, reload and a shared link
 * show the same rows. `model` is the API's `slug`; `page` counts from 1.
 */

export const PAGE_SIZE = 50

export type ListingsSort = 'position' | 'title' | 'slug' | 'updated'
export type ListingsStatus = '' | 'published' | 'draft'

export interface ListingsQuery {
  q: string
  status: ListingsStatus
  model: string
  material: string
  sort: ListingsSort
  dir: 'asc' | 'desc'
  page: number
}

export const DEFAULT_LISTINGS_QUERY: ListingsQuery = { q: '', status: '', model: '', material: '', sort: 'position', dir: 'asc', page: 1 }

const SORTS: readonly ListingsSort[] = ['position', 'title', 'slug', 'updated']

export function parseListingsQuery(params: URLSearchParams): ListingsQuery {
  const status = params.get('status')
  const sort = params.get('sort') as ListingsSort | null
  const page = Number.parseInt(params.get('page') ?? '', 10)
  return {
    q: params.get('q') ?? '',
    status: status === 'published' || status === 'draft' ? status : '',
    model: params.get('model') ?? '',
    material: params.get('material') ?? '',
    sort: sort && SORTS.includes(sort) ? sort : 'position',
    dir: params.get('dir') === 'desc' ? 'desc' : 'asc',
    page: Number.isFinite(page) && page > 0 ? page : 1,
  }
}

/** The query as the page's URL parameters, leaving out every default. */
export function toListingsParams(query: ListingsQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.q) params.set('q', query.q)
  if (query.status) params.set('status', query.status)
  if (query.model) params.set('model', query.model)
  if (query.material) params.set('material', query.material)
  if (query.sort !== 'position') params.set('sort', query.sort)
  if (query.dir !== 'asc') params.set('dir', query.dir)
  if (query.page !== 1) params.set('page', String(query.page))
  return params
}

/** The query as `GET /storefront/listings`'s parameters. */
export function toApiParams(query: ListingsQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.q.trim()) params.set('q', query.q.trim())
  if (query.status) params.set('status', query.status)
  if (query.model) params.set('slug', query.model)
  if (query.material) params.set('material', query.material)
  params.set('sort', query.sort)
  params.set('dir', query.dir)
  params.set('limit', String(PAGE_SIZE))
  params.set('offset', String((query.page - 1) * PAGE_SIZE))
  return params
}

/** Whether the query narrows the list: an empty answer then means "no match", not "no listings". */
export function isFiltered(query: ListingsQuery): boolean {
  return Boolean(query.q.trim() || query.status || query.model || query.material)
}

/** A listing's editor address (#6157); the list passes its own query in the history state. */
export function listingPath(id: string): string {
  return `/storefront/listings/${encodeURIComponent(id)}`
}
