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

/**
 * What `GET /models` takes (`api/models.py` `SearchWords`, `MAX_TAG_FILTERS`,
 * `MAX_TAG_FILTER_CHARS`). A hand-edited URL past them must not answer 422 (#5286
 * review). Its tags are kept: every tag is an AND filter, and the route takes four
 * times a model's own caps (`MAX_TAGS`, `MAX_TAG_CHARS`) and answers no model past
 * those, so tags past the route's bounds select none either (`unmatchable`), and the
 * page asks for none. A search past its cap is cut to it, which may select more than
 * the whole search would.
 */
export const MAX_SEARCH_CHARS = 256
export const MAX_TAGS = 50
export const MAX_TAG_CHARS = 100
export const MAX_TAG_FILTERS = 4 * MAX_TAGS
export const MAX_TAG_FILTER_CHARS = 4 * MAX_TAG_CHARS

/**
 * Whether `GET /models` would refuse `tags` as sent: more of them than it takes, or one
 * longer (in code points, as the backend counts). No model can carry them all.
 */
export function unmatchable(tags: readonly string[]): boolean {
  return tags.length > MAX_TAG_FILTERS || tags.some((tag) => [...tag].length > MAX_TAG_FILTER_CHARS)
}

export function parseQuery(params: URLSearchParams): CatalogueQuery {
  return {
    q: (params.get('q') ?? '').slice(0, MAX_SEARCH_CHARS),
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

/**
 * Case- and accent-insensitive form of `text`: "Crème" and "creme" fold alike. The
 * browser's own matching only (the mocks, a tag chip): it is not the backend's `fold`
 * (`api/models.py`, combining class), which no regex matches exactly, so tags go to the
 * route as typed and the route folds them (#5305 review).
 */
export function fold(text: string): string {
  return text.normalize('NFD').replace(/\p{Diacritic}/gu, '').toLowerCase()
}

/**
 * The folded texts in UTF-16 code-unit order: the backend's order (`api/models.py`
 * `fold`, code points) for text both folds agree on. They differ on what `casefold`
 * expands (`ß`, ligatures) and on characters outside the Basic Multilingual Plane.
 */
const byFolded = (a: string, b: string) => {
  const [x, y] = [fold(a), fold(b)]
  return x < y ? -1 : x > y ? 1 : 0
}

const byName = (a: ModelSummary, b: ModelSummary) => byFolded(a.name, b.name)

/**
 * The models `query` selects, sorted. Every search word must appear in some field. The
 * backend's `GET /models` selects the same (#2290); the mocks answer with this.
 */
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
 * Tags that fold alike count as one, spelled as the first model that carries it spells
 * it. A `selected` tag none of them carry is listed at zero, so its chip can still be
 * unselected. The backend's `GET /model-facets` counts the same (#2290).
 */
export function tagCounts(
  models: ModelSummary[],
  selected: string[] = [],
): Array<{ tag: string; count: number }> {
  const spelling = new Map<string, string>()
  const counts = new Map<string, number>()
  for (const model of models) {
    for (const tag of model.tags ?? []) if (!spelling.has(fold(tag))) spelling.set(fold(tag), tag)
    for (const key of new Set((model.tags ?? []).map(fold))) counts.set(key, (counts.get(key) ?? 0) + 1)
  }
  for (const tag of selected) {
    if (!spelling.has(fold(tag))) spelling.set(fold(tag), tag)
    if (!counts.has(fold(tag))) counts.set(fold(tag), 0)
  }
  return [...counts]
    .map(([key, count]) => ({ tag: spelling.get(key) ?? key, count }))
    .sort(
      (a, b) => b.count - a.count || byFolded(a.tag, b.tag),
    )
}
