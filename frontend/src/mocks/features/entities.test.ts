import { beforeEach, describe, expect, it } from 'vitest'
import { ApiError } from '../../api/client'
import { entityApi } from '../../components/entities/api'
import { resetMockState } from '../handlers'
import { seedEntityProperties } from './entities'

const TYPE = 'spool_type:7a000000-0000-4000-8000-000000000001'

/** The schema route's 422 detail, or null when it took the schema. */
async function putSchema(schema: Record<string, unknown>): Promise<string | null> {
  const response = await fetch('/api/v1/entity-kinds/spool_type/property-schema', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ schema }),
  })
  return response.ok ? null : ((await response.json()) as { detail: string }).detail
}

async function refusal(set: Record<string, unknown>): Promise<string | null> {
  const error = await entityApi.setProperties(TYPE, set as never).then(
    () => null,
    (e: unknown) => e,
  )
  if (error === null) return null
  expect(error).toBeInstanceOf(ApiError)
  expect((error as ApiError).status).toBe(422)
  return (error as ApiError).problem.detail ?? ''
}

describe('entity properties mock (#7026)', () => {
  beforeEach(() => resetMockState())

  it("refuses an item's schema: it is its item kind's (#7067)", async () => {
    const url = '/api/v1/entity-kinds/item/property-schema'
    const read = await fetch(url)
    const write = await fetch(url, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ schema: { type: 'object' } }) })
    for (const response of [read, write]) {
      expect(response.status).toBe(409)
      expect(((await response.json()) as { code: string }).code).toBe('schema_owned_by_item_kind')
    }
  })

  it('takes only the Draft 2020-12 $schema', async () => {
    expect(await putSchema({ type: 'object', $schema: 'https://json-schema.org/draft/2020-12/schema' })).toBeNull()
    expect(await putSchema({ type: 'object', $schema: 'http://json-schema.org/draft-07/schema#' })).toMatch(/\$schema/)
  })

  it("takes only a key's value types, or a list of them with null", async () => {
    expect(await putSchema({ type: 'object', properties: { a: { type: ['string', 'null'] }, b: { type: 'integer' } } })).toBeNull()
    for (const type of ['object', 'null', ['null'], ['string', 'object']]) {
      expect(await putSchema({ type: 'object', properties: { a: { type } } })).toMatch(/the type in property 'a'/)
    }
  })

  it("limits `items` to the key keywords but `items`, and to strings", async () => {
    expect(await putSchema({ type: 'object', properties: { a: { type: 'array', items: { type: 'string', maxLength: 13 } } } })).toBeNull()
    expect(await putSchema({ type: 'object', properties: { a: { type: 'array', items: { items: {} } } } })).toMatch(
      /keyword 'items' is not allowed in the items of property 'a'/,
    )
    expect(await putSchema({ type: 'object', properties: { a: { type: 'array', items: { type: 'number' } } } })).toMatch(
      /the type in the items of property 'a'/,
    )
    expect(await putSchema({ type: 'object', properties: { a: { type: 'array', items: { pattern: 'x' } } } })).toMatch(/keyword 'pattern'/)
  })

  it('refuses a patch that leaves more than 200 keys, unless the entity had more before', async () => {
    seedEntityProperties(TYPE, Object.fromEntries(Array.from({ length: 200 }, (_, i) => [`k${i}`, 'x'])))
    expect(await refusal({ extra: 'x' })).toMatch(/at most 200 keys/)
    expect(await refusal({ k0: null, extra: 'x' })).toBeNull()
  })

  it('refuses a string that holds U+0000, in a value or a list', async () => {
    expect(await refusal({ a: 'x\u0000y' })).toMatch(/U\+0000/)
    expect(await refusal({ a: ['ok', '\u0000'] })).toMatch(/U\+0000/)
  })

  it('counts a string in code points, not UTF-16 units', async () => {
    // 2000 astral characters are 4000 UTF-16 units and still 2000 characters.
    expect(await refusal({ a: '😀'.repeat(2000) })).toBeNull()
    expect(await refusal({ a: '😀'.repeat(2001) })).toMatch(/2000/)
    expect(await refusal({ b: ['😀'.repeat(500)] })).toBeNull()
    expect(await refusal({ b: ['😀'.repeat(501)] })).toMatch(/500/)
  })
})
