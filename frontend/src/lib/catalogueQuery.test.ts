import { describe, expect, it } from 'vitest'
import type { ModelSummary } from '../api/types'
import { models } from '../mocks/fixtures'
import {
  DEFAULT_QUERY,
  MAX_SEARCH_CHARS,
  MAX_TAG_CHARS,
  MAX_TAGS,
  type CatalogueQuery,
  clearFilters,
  filterModels,
  parseQuery,
  tagCounts,
  toParams,
  unmatchable,
} from './catalogueQuery'

function query(patch: Partial<CatalogueQuery>): CatalogueQuery {
  return { ...DEFAULT_QUERY, ...patch }
}

function slugs(list: ModelSummary[]): string[] {
  return list.map((model) => model.slug)
}

describe('parseQuery / toParams', () => {
  it('reads the defaults from an empty query', () => {
    expect(parseQuery(new URLSearchParams())).toEqual({
      q: '',
      tags: [],
      origin: 'all',
      sort: 'updated',
      view: 'cards',
    })
  })

  it('omits every default but the view, which is always named', () => {
    expect(toParams(DEFAULT_QUERY).toString()).toBe('view=cards')
    expect(toParams(query({ q: '  ' })).toString()).toBe('view=cards')
  })

  it('round-trips every field, including a tag with a space and `&`', () => {
    const full = query({
      q: 'Crème brûlée',
      tags: ['Tea & Coffee', 'kitchen'],
      origin: 'mine',
      sort: 'name',
      view: 'list',
    })
    const search = toParams(full).toString()
    expect(search).toContain('tag=Tea+%26+Coffee')
    expect(parseQuery(new URLSearchParams(search))).toEqual(full)
    expect(parseQuery(new URLSearchParams(`?${search}`))).toEqual(full)
  })

  it('keeps every tag of a hand-edited URL, and cuts only the search to what GET /models takes (#5288 review)', () => {
    const many = Array.from({ length: MAX_TAGS + 5 }, (_, n) => `tag=t${n}`).join('&')
    const long = 'x'.repeat(MAX_TAG_CHARS + 1)
    const parsed = parseQuery(new URLSearchParams(`${many}&tag=${long}&q=${'y'.repeat(MAX_SEARCH_CHARS + 9)}`))
    expect(parsed.tags).toHaveLength(MAX_TAGS + 6)
    expect(parsed.tags).toContain(long)
    expect(parsed.q).toHaveLength(MAX_SEARCH_CHARS)
  })

  it('ignores unknown values and repeated or empty tags', () => {
    expect(
      parseQuery(new URLSearchParams('origin=theirs&sort=size&view=grid&tag=a&tag=a&tag=')),
    ).toEqual(query({ tags: ['a'] }))
  })
})

describe('filterModels', () => {
  it('returns every model, most recently updated first, by default', () => {
    expect(slugs(filterModels(models, DEFAULT_QUERY))).toEqual([
      'name-keychain',
      'creme-coaster',
      'gridfinity-bin',
      'builtin:maze-puzzle',
      'ui-broken',
      'ui-demo',
      'builtin:keychain-template',
      'builtin:dollhouse-kit',
      'ui-unmigrated',
    ])
  })

  it.each([
    ['creme', 'name, accents folded'],
    ['CRÈME', 'name, case folded'],
    ['raised rim', 'description'],
    ['coffee', 'tags'],
    ['coaster rim', 'every word, across fields'],
  ])('matches %s (%s)', (q) => {
    expect(slugs(filterModels(models, query({ q })))).toEqual(['creme-coaster'])
  })

  it('matches nothing when one word is missing', () => {
    expect(filterModels(models, query({ q: 'coaster gridfinity' }))).toEqual([])
  })

  it('ANDs several tags, folding case', () => {
    expect(slugs(filterModels(models, query({ tags: ['keychain'] })))).toEqual([
      'name-keychain',
      'builtin:keychain-template',
    ])
    expect(slugs(filterModels(models, query({ tags: ['Keychain', 'template'] })))).toEqual([
      'builtin:keychain-template',
    ])
    expect(filterModels(models, query({ tags: ['keychain', 'storage'] }))).toEqual([])
  })

  it('filters by origin', () => {
    expect(slugs(filterModels(models, query({ origin: 'builtin' })))).toEqual([
      'builtin:maze-puzzle',
      'builtin:keychain-template',
      'builtin:dollhouse-kit',
    ])
    expect(slugs(filterModels(models, query({ origin: 'mine' })))).not.toContain(
      'builtin:keychain-template',
    )
  })

  it('sorts by name', () => {
    expect(slugs(filterModels(models, query({ sort: 'name' })))).toEqual([
      'builtin:maze-puzzle',
      'creme-coaster',
      'builtin:dollhouse-kit',
      'gridfinity-bin',
      'builtin:keychain-template',
      'name-keychain',
      'ui-broken',
      'ui-demo',
      'ui-unmigrated',
    ])
  })

  it('sorts names as the backend does: code points after folding (#5286 review)', () => {
    const named = ['Zed', 'a-b', '_under', '2 bolt', 'A b', '10 bolt', 'Éclair'].map((name, n) => ({
      ...(models[0] as ModelSummary),
      slug: `e${n}`,
      name,
    }))
    // backend/tests/api/test_list_paging.py::test_models_sort_names_by_folded_code_points
    expect(filterModels(named, query({ sort: 'name' })).map((m) => m.name)).toEqual([
      '10 bolt',
      '2 bolt',
      '_under',
      'A b',
      'a-b',
      'Éclair',
      'Zed',
    ])
  })

  it('does not reorder its input', () => {
    const input = [...models]
    filterModels(input, query({ sort: 'name' }))
    expect(input).toEqual(models)
  })
})

describe('tagCounts', () => {
  it('counts each tag once per model, most used first, then by name', () => {
    const extra = { ...(models[0] as ModelSummary), slug: 'x', tags: ['kitchen', 'kitchen'] }
    expect(tagCounts([...models, extra])).toEqual([
      { tag: 'custom-ui', count: 4 },
      { tag: 'keychain', count: 2 },
      { tag: 'kitchen', count: 2 },
      { tag: 'gridfinity', count: 1 },
      { tag: 'storage', count: 1 },
      { tag: 'Tea & Coffee', count: 1 },
      { tag: 'template', count: 1 },
      { tag: 'text', count: 1 },
      { tag: 'two-colour', count: 1 },
    ])
  })

  it('counts tags that fold alike as one, spelled as the first model spells it (#2290)', () => {
    const first = { ...(models[0] as ModelSummary), slug: 'a', tags: ['Crème'] }
    const second = { ...(models[0] as ModelSummary), slug: 'b', tags: ['creme', 'CREME'] }
    expect(tagCounts([first, second])).toEqual([{ tag: 'Crème', count: 2 }])
  })

  it('keeps a selected tag no model carries, at zero, so it can be unselected', () => {
    expect(tagCounts([], ['template'])).toEqual([{ tag: 'template', count: 0 }])
    expect(tagCounts(models.slice(0, 1), ['KEYCHAIN'])).not.toContainEqual(
      expect.objectContaining({ tag: 'KEYCHAIN' }),
    )
  })
})

describe('unmatchable (#5288 review)', () => {
  it('is true for tags no model can carry all of: too many, or one too long', () => {
    expect(unmatchable(Array.from({ length: MAX_TAGS + 1 }, (_, n) => `t${n}`))).toBe(true)
    expect(unmatchable(['x'.repeat(MAX_TAG_CHARS + 1)])).toBe(true)
  })

  it('counts tags that fold alike once, and a tag in code points as the backend does', () => {
    expect(unmatchable([...Array.from({ length: MAX_TAGS }, (_, n) => `t${n}`), 'T0'])).toBe(false)
    // 60 code points, 120 UTF-16 units: the backend takes it.
    expect(unmatchable(['😀'.repeat(60)])).toBe(false)
  })
})

describe('clearFilters', () => {
  it('drops the search and filters, keeping sort and view', () => {
    expect(
      clearFilters(query({ q: 'x', tags: ['a'], origin: 'mine', sort: 'name', view: 'list' })),
    ).toEqual(query({ sort: 'name', view: 'list' }))
  })
})
