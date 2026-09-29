import type { ModelSummary } from '../api/types'

/**
 * #276 — the catalogue's search, filters, sort and view mode, kept in the URL
 * (`?q=&tag=a&tag=b&origin=&sort=&view=`) so back/forward and deep links work.
 * `view` is parsed and carried through here; the list mode (#278) acts on it.
 */
export type CatalogueOrigin = 'all' | 'builtin' | 'mine'
export type CatalogueSort = 'updated' | 'name'
export type CatalogueView = 'cards' | 'list'

export type CatalogueQuery = {
  q: string
  tags: string[]
  origin: CatalogueOrigin
  sort: CatalogueSort
  view: CatalogueView
}

export const DEFAULT_QUERY: CatalogueQuery = {
  q: '',
  tags: [],
  origin: 'all',
  sort: 'updated',
  view: 'cards',
}

const ORIGINS: readonly CatalogueOrigin[] = ['all', 'builtin', 'mine']
const SORTS: readonly CatalogueSort[] = ['updated', 'name']
const VIEWS: readonly CatalogueView[] = ['cards', 'list']

function oneOf<T extends string>(value: string | null, allowed: readonly T[], fallback: T): T {
  return allowed.find((option) => option === value) ?? fallback
}

export function parseQuery(params: URLSearchParams): CatalogueQuery {
  return {
    q: params.get('q') ?? '',
    tags: [...new Set(params.getAll('tag').filter((tag) => tag !== ''))],
    origin: oneOf(params.get('origin'), ORIGINS, DEFAULT_QUERY.origin),
    sort: oneOf(params.get('sort'), SORTS, DEFAULT_QUERY.sort),
    view: oneOf(params.get('view'), VIEWS, DEFAULT_QUERY.view),
  }
}

/** The query as URL parameters, leaving out every default but `view`, which is always named (#278). */
export function toParams(query: CatalogueQuery): URLSearchParams {
  const params = new URLSearchParams()
  if (query.q.trim()) params.set('q', query.q)
  for (const tag of query.tags) params.append('tag', tag)
  if (query.origin !== DEFAULT_QUERY.origin) params.set('origin', query.origin)
  if (query.sort !== DEFAULT_QUERY.sort) params.set('sort', query.sort)
  params.set('view', query.view)
  return params
}

/** `query` with no search or filter, keeping its sort and view. */
export function clearFilters(query: CatalogueQuery): CatalogueQuery {
  return { ...DEFAULT_QUERY, sort: query.sort, view: query.view }
}

/** Case- and accent-insensitive form of `text`: "Crème" and "creme" fold alike. */
export function fold(text: string): string {
  return text.normalize('NFD').replace(/\p{Diacritic}/gu, '').toLowerCase()
}

const byName = (a: ModelSummary, b: ModelSummary) =>
  a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })

/** The models `query` selects, sorted. Every search word must appear in some field. */
export function filterModels(models: ModelSummary[], query: CatalogueQuery): ModelSummary[] {
  const words = fold(query.q).split(/\s+/).filter(Boolean)
  const tags = query.tags.map(fold)
  const selected = models.filter((model) => {
    if (query.origin !== 'all' && model.origin !== query.origin) return false
    const own = (model.tags ?? []).map(fold)
    if (!tags.every((tag) => own.includes(tag))) return false
    const text = fold([model.name, model.description ?? '', ...(model.tags ?? [])].join('\n'))
    return words.every((word) => text.includes(word))
  })
  return selected.sort(
    query.sort === 'name'
      ? byName
      : (a, b) => Date.parse(b.updated_at) - Date.parse(a.updated_at) || byName(a, b),
  )
}

/**
 * Every tag in `models` with how many models carry it: most used first, then by name.
 * A `selected` tag none of them carry is listed at zero, so its chip can still be
 * unselected.
 */
export function tagCounts(
  models: ModelSummary[],
  selected: string[] = [],
): Array<{ tag: string; count: number }> {
  const counts = new Map<string, number>()
  for (const model of models) {
    for (const tag of new Set(model.tags ?? [])) counts.set(tag, (counts.get(tag) ?? 0) + 1)
  }
  const present = new Set([...counts.keys()].map(fold))
  for (const tag of selected) if (!present.has(fold(tag))) counts.set(tag, 0)
  return [...counts]
    .map(([tag, count]) => ({ tag, count }))
    .sort(
      (a, b) => b.count - a.count || a.tag.localeCompare(b.tag, undefined, { sensitivity: 'base' }),
    )
}
