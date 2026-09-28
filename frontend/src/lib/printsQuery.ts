/**
 * #310 — the print history's filters and view, kept in the URL
 * (`?slug=&status=&printer=&from=&to=&q=&view=cards|list`, plan §2.7) so back/forward
 * and deep links work, as the catalogue's are (#276). On a template's Prints tab the
 * template comes from the route, and `slug` in the URL is ignored.
 */
export type PrintsView = 'cards' | 'list'

export type PrintsQuery = {
  slug: string
  status: string
  /** A Bambuddy printer id, as text; '' is every printer. */
  printer: string
  /** Inclusive days, `YYYY-MM-DD`; '' is open. */
  from: string
  to: string
  q: string
  view: PrintsView
}

export const DEFAULT_PRINTS_QUERY: PrintsQuery = {
  slug: '',
  status: '',
  printer: '',
  from: '',
  to: '',
  q: '',
  view: 'cards',
}

const DAY = /^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$/
/** At most 15 digits: every such id is a safe integer, so `Number()` keeps it exact. */
const PRINTER = /^[1-9]\d{0,14}$/

function day(value: string | null): string {
  return value !== null && DAY.test(value) ? value : ''
}

export function parsePrintsQuery(params: URLSearchParams): PrintsQuery {
  const printer = params.get('printer') ?? ''
  const view = params.get('view')
  return {
    slug: params.get('slug') ?? '',
    status: params.get('status') ?? '',
    printer: PRINTER.test(printer) ? printer : '',
    from: day(params.get('from')),
    to: day(params.get('to')),
    q: params.get('q') ?? '',
    view: view === 'list' || view === 'cards' ? view : DEFAULT_PRINTS_QUERY.view,
  }
}

/** The query as URL parameters, leaving out every default. */
export function toPrintsParams(query: PrintsQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.slug) params.set('slug', query.slug)
  if (query.status) params.set('status', query.status)
  if (query.printer) params.set('printer', query.printer)
  if (query.from) params.set('from', query.from)
  if (query.to) params.set('to', query.to)
  if (query.q.trim()) params.set('q', query.q)
  if (query.view !== DEFAULT_PRINTS_QUERY.view) params.set('view', query.view)
  return params
}

/** `query` with no filter, keeping its view. */
export function clearPrintFilters(query: PrintsQuery): PrintsQuery {
  return { ...DEFAULT_PRINTS_QUERY, view: query.view }
}

export function isFiltered(query: PrintsQuery): boolean {
  return toPrintsParams({ ...query, view: DEFAULT_PRINTS_QUERY.view }).size > 0
}

/** The filters of `GET /api/v1/prints`; `fixedSlug` is a template's Prints tab. */
export interface PrintFilters {
  slug?: string
  status?: string
  printer_id?: number
  from?: string
  to?: string
  q?: string
}

export function apiFilters(query: PrintsQuery, fixedSlug?: string): PrintFilters {
  const filters: PrintFilters = {}
  const slug = fixedSlug ?? query.slug
  if (slug) filters.slug = slug
  if (query.status) filters.status = query.status
  if (query.printer) filters.printer_id = Number(query.printer)
  if (query.from) filters.from = query.from
  if (query.to) filters.to = query.to
  if (query.q.trim()) filters.q = query.q.trim()
  return filters
}

/**
 * The view this browser last chose, used when the URL names none. Storage that throws
 * (private mode, blocked site data, Bambuddy's sandboxed iframe) keeps the choice in
 * memory for this page only, as the catalogue's view does (#278).
 */
export const PRINTS_VIEW_KEY = 'scadbuddy.prints.view'

let fallback: PrintsView | null = null

export function readStoredPrintsView(): PrintsView | null {
  if (fallback) return fallback
  try {
    const value = window.localStorage.getItem(PRINTS_VIEW_KEY)
    return value === 'cards' || value === 'list' ? value : null
  } catch {
    return null
  }
}

export function storePrintsView(view: PrintsView): void {
  try {
    window.localStorage.setItem(PRINTS_VIEW_KEY, view)
    fallback = null
  } catch {
    fallback = view
  }
}

/** For tests: forget the in-memory choice. */
export function resetStoredPrintsView(): void {
  fallback = null
}
