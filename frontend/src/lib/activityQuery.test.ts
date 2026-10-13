import { describe, expect, it } from 'vitest'
import { apiParams, joinList, parseActivityQuery, splitList, toActivityParams } from './activityQuery'

describe('activityQuery', () => {
  it('round-trips through the URL, facets included, defaults left out', () => {
    const q = parseActivityQuery(new URLSearchParams('kind=render&status=failed&facet.slug=box'))
    expect(q.facets).toEqual({ slug: 'box' })
    expect(toActivityParams(q).toString()).toBe('kind=render&status=failed&facet.slug=box')
  })
  it('maps to the API names', () => {
    const q = parseActivityQuery(new URLSearchParams('tool=render_model&facet.slug=box'))
    expect(apiParams(q)).toEqual({ via_tool: 'render_model', 'facet.slug': 'box' })
  })
})

describe('activityQuery lists and trimming', () => {
  it('splits and joins the comma convention', () => {
    expect(splitList('a,b,')).toEqual(['a', 'b'])
    expect(joinList(['a', 'b'])).toBe('a,b')
  })
  it('trims q the same in the URL and the API', () => {
    const q = parseActivityQuery(new URLSearchParams('q=%20a%20'))
    expect(toActivityParams(q).get('q')).toBe('a')
    expect(apiParams(q).q).toBe('a')
  })
})
