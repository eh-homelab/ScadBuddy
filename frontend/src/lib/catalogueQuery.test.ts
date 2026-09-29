import { describe, expect, it } from 'vitest'
import type { ModelSummary } from '../api/types'
import { models } from '../mocks/fixtures'
import {
  DEFAULT_QUERY,
  type CatalogueQuery,
  clearFilters,
  filterModels,
  parseQuery,
  tagCounts,
  toParams,
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

  it('omits every default', () => {
    expect(toParams(DEFAULT_QUERY).toString()).toBe('')
    expect(toParams(query({ q: '  ' })).toString()).toBe('')
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
      'ui-broken',
      'ui-demo',
      'builtin:keychain-template',
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
      'builtin:keychain-template',
    ])
    expect(slugs(filterModels(models, query({ origin: 'mine' })))).not.toContain(
      'builtin:keychain-template',
    )
  })

  it('sorts by name, in locale order', () => {
    expect(slugs(filterModels(models, query({ sort: 'name' })))).toEqual([
      'creme-coaster',
      'gridfinity-bin',
      'builtin:keychain-template',
      'name-keychain',
      'ui-broken',
      'ui-demo',
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
      { tag: 'custom-ui', count: 2 },
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

  it('keeps a selected tag no model carries, at zero, so it can be unselected', () => {
    expect(tagCounts([], ['template'])).toEqual([{ tag: 'template', count: 0 }])
    expect(tagCounts(models.slice(0, 1), ['KEYCHAIN'])).not.toContainEqual(
      expect.objectContaining({ tag: 'KEYCHAIN' }),
    )
  })
})

describe('clearFilters', () => {
  it('drops the search and filters, keeping sort and view', () => {
    expect(
      clearFilters(query({ q: 'x', tags: ['a'], origin: 'mine', sort: 'name', view: 'list' })),
    ).toEqual(query({ sort: 'name', view: 'list' }))
  })
})
