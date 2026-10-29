import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { LISTING_CAPS, listedTool, toStoredTools, ToolListings } from '../src/access/listings.js'
import type { Database } from '../src/db.js'
import { TEST_DATABASE_URL, TEST_DATABASE_URL_ENV, throwawayDatabase } from './support/postgres.js'

// A remote plugin's tools/list (#2171, #778): each entry stored once, as listed, with only
// what a cap removed taken out and named.

const SRC = 'remote:memory'

describe('toStoredTools', () => {
  it('caps an oversized tool before it is stored', () => {
    const [cut] = toStoredTools([{ name: 'big', description: 'd'.repeat(9_000), inputSchema: { type: 'object', x: 's'.repeat(70_000) } }])
    expect((cut!.raw.description as string).length).toBe(LISTING_CAPS.description)
    expect(cut!.raw).not.toHaveProperty('inputSchema')
    expect(cut!.omitted).toEqual(['description', 'inputSchema'])
    const many = toStoredTools(Array.from({ length: 600 }, (_, i) => ({ name: `t${i}` })))
    expect(many).toHaveLength(LISTING_CAPS.tools)
    expect(many.at(-1)!.raw.name).toBe('t511')
  })

  it("caps a listing's total size, dropping raw first", () => {
    const schema = { type: 'object', x: 's'.repeat(60_000) }
    // Removing the other fields is enough: the schemas stay.
    const some = toStoredTools(Array.from({ length: 30 }, (_, i) => ({ name: `t${i}`, inputSchema: schema, x_extra: 'e'.repeat(15_000) })))
    expect(JSON.stringify(some).length).toBeLessThanOrEqual(LISTING_CAPS.total)
    expect(some.every((t) => t.raw.inputSchema !== undefined && t.raw.x_extra === undefined)).toBe(true)
    expect(some.every((t) => t.omitted.join() === 'other')).toBe(true)
    // It is not: the schemas go too, output before input, each removal named.
    const all = toStoredTools(
      Array.from({ length: 300 }, (_, i) => ({ name: `t${i}`, inputSchema: schema, outputSchema: { type: 'object' }, x_extra: 1 })),
    )
    expect(JSON.stringify(all).length).toBeLessThanOrEqual(LISTING_CAPS.total)
    for (const t of all) {
      expect(Object.keys(t.raw)).toEqual(['name'])
      expect(t.omitted).toEqual(['other', 'outputSchema', 'inputSchema'])
    }
  })

  it('drops an entry without a name', () => {
    expect(toStoredTools([{ title: 'no name' }, { name: 7 }, { name: 'ok' }]).map((t) => t.raw.name)).toEqual(['ok'])
  })

  it('derives a listed tool from the stored entry', () => {
    const tool = listedTool({ raw: { name: 'a', annotations: { title: 'From annotations' }, inputSchema: { type: 'object' } }, omitted: [] })
    expect(tool).toMatchObject({ name: 'a', title: 'From annotations', description: null, input_schema: { type: 'object' }, output_schema: null })
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`ToolListings${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let listings: ToolListings

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    listings = new ToolListings(db.sql)
  })
  afterEach(async () => {
    await drop()
  })

  it('stores a listing and reads it back with listed_at', async () => {
    expect(await listings.get(SRC)).toBeUndefined()
    await listings.put(SRC, toStoredTools([{ name: 'recall', description: 'Search' }]))
    const got = await listings.get(SRC)
    expect(got!.tools.map((t) => [t.name, t.description])).toEqual([['recall', 'Search']])
    expect(Number.isNaN(Date.parse(got!.listed_at))).toBe(false)
  })

  it('put replaces the last listing', async () => {
    await listings.put(SRC, toStoredTools([{ name: 'a' }]))
    await listings.put(SRC, toStoredTools([{ name: 'b' }, { name: 'c' }]))
    expect((await listings.get(SRC))!.tools.map((t) => t.name)).toEqual(['b', 'c'])
  })

  it('stores each entry once and derives its parts', async () => {
    await listings.put(
      SRC,
      toStoredTools([{ name: 'recall', title: 'Recall', description: 'Search', inputSchema: { type: 'object' }, x_new: 1 }]),
    )
    const [row] = await db.sql<{ tools: unknown[] }[]>`SELECT tools FROM ai_tool_listings WHERE source = ${SRC}`
    expect(Object.keys(row!.tools[0] as object).sort()).toEqual(['omitted', 'raw'])
    expect((await listings.get(SRC))!.tools[0]).toMatchObject({
      name: 'recall',
      title: 'Recall',
      description: 'Search',
      input_schema: { type: 'object' },
      raw: { x_new: 1 },
      omitted: [],
    })
  })

  it('reads a row whose tools do not parse as no listing', async () => {
    await db.sql`INSERT INTO ai_tool_listings (source, tools) VALUES (${SRC}, ${db.sql.json([{ nope: true }])})`
    expect(await listings.get(SRC)).toBeUndefined()
  })

  it('getMany reads several sources in one query', async () => {
    await listings.put('remote:a', toStoredTools([{ name: 'x' }]))
    await listings.put('remote:b', toStoredTools([{ name: 'y' }]))
    const many = await listings.getMany(['remote:a', 'remote:b', 'remote:none'])
    expect([...many.keys()].sort()).toEqual(['remote:a', 'remote:b'])
    expect(many.get('remote:b')!.tools[0]!.name).toBe('y')
    expect((await listings.getMany([])).size).toBe(0)
  })
})
