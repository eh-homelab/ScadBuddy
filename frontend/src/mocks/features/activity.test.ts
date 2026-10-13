import { describe, expect, it } from 'vitest'
import { api, ApiError } from '../../api/client'
import { parseActivityQuery } from '../../lib/activityQuery'

const query = (search: string) => parseActivityQuery(new URLSearchParams(search))

describe('activity mock', () => {
  it('filters on a facet of a selected kind', async () => {
    const page = await api.listActivity(query('kind=render&facet.slug=box'))
    expect(page.items.map((r) => r.id)).toEqual(['render:r1'])
  })

  it('answers 400 for a facet no selected kind declares', async () => {
    const error = await api.listActivity(query('facet.slug=box')).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(400)
  })

  it('pages with an opaque cursor and answers 400 bad-cursor for garbage', async () => {
    const first = await api.listActivity(query(''), { limit: 2 })
    expect(first.items).toHaveLength(2)
    expect(first.next_cursor).not.toBeNull()
    const second = await api.listActivity(query(''), { cursor: first.next_cursor, limit: 2 })
    expect(second.items).toHaveLength(1)
    expect(second.next_cursor).toBeNull()
    const error = await api.listActivity(query(''), { cursor: '5' }).catch((e: unknown) => e)
    expect((error as ApiError).status).toBe(400)
    expect((error as ApiError).problem.type).toMatch(/bad-cursor$/)
  })

  it('answers 422 for a bad status or limit', async () => {
    const status = await api.listActivity(query('status=bogus')).catch((e: unknown) => e)
    expect((status as ApiError).status).toBe(422)
    const limit = await api.listActivity(query(''), { limit: 0 }).catch((e: unknown) => e)
    expect((limit as ApiError).status).toBe(422)
  })
})
