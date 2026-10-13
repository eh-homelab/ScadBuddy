import { describe, expect, it } from 'vitest'
import { apiParams, parseActivityQuery, toActivityParams } from './activityQuery'

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
