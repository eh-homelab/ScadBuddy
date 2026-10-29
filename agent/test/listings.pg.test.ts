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

  it('caps name, title and annotations per entry (#2171 review I2)', () => {
    const tools = toStoredTools([
      { name: 'big', title: 't'.repeat(2_100_000), annotations: { x: 'a'.repeat(20_000) } },
      { name: 'n'.repeat(129) },
      { name: 'ok', title: 'Fine' },
    ])
    expect(tools.map((t) => t.raw.name)).toEqual(['big', 'ok'])
    expect((tools[0]!.raw.title as string).length).toBe(LISTING_CAPS.title)
    expect(tools[0]!.raw).not.toHaveProperty('annotations')
    expect(tools[0]!.omitted).toEqual(['title', 'annotations'])
    expect(tools[1]).toEqual({ raw: { name: 'ok', title: 'Fine' }, omitted: [] })
  })

  it('past the schemas, drops annotations, then descriptions, then cuts the last tools to their names', () => {
    // 512 tools of 1,024 escaped title characters (6 each in JSON) and big descriptions: over the total even bare.
    const tools = toStoredTools(
      Array.from({ length: 512 }, (_, i) => ({
        name: `t${i}`,
        title: '\u0001'.repeat(LISTING_CAPS.title),
        description: '\u0001'.repeat(LISTING_CAPS.description),
        annotations: { readOnlyHint: true, x: 'a'.repeat(15_000) },
      })),
    )
    expect(JSON.stringify(tools).length).toBeLessThanOrEqual(LISTING_CAPS.total)
    expect(tools).toHaveLength(512)
    expect(tools[0]!.omitted).toEqual(['annotations', 'description'])
    expect(tools[0]!.raw.title).toBeDefined()
    const last = tools.at(-1)!
    expect(last.raw).toEqual({ name: 't511' })
    expect(last.omitted).toEqual(['annotations', 'description', 'title'])
  })

  it('keeps the first of two keys that collide once storable, and names the loss (#2171 re-review N2)', () => {
    const [tool] = toStoredTools([{ name: 'k', 'a\u0000': 1, 'a\uFFFD': 2, inputSchema: { type: 'object', properties: { 'p\u0000': {}, 'p\uD800': { type: 'string' } } } }])
    expect(tool!.raw['a\uFFFD']).toBe(1)
    expect(tool!.raw.inputSchema).toEqual({ type: 'object', properties: { 'p\uFFFD': {} } })
    expect(tool!.omitted).toEqual(['keys'])
    expect(toStoredTools([{ name: 'clean', x: { y: 1 } }])[0]!.omitted).toEqual([])
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
    expect(got!.truncated).toBe(false)
  })

  it('stores whether the listing is incomplete (#2171 re-review N1)', async () => {
    await listings.put(SRC, toStoredTools([{ name: 'a' }]), { truncated: true })
    expect((await listings.get(SRC))!.truncated).toBe(true)
    expect((await listings.getMany([SRC])).get(SRC)!.truncated).toBe(true)
    await listings.put(SRC, toStoredTools([{ name: 'a' }]))
    expect((await listings.get(SRC))!.truncated).toBe(false)
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

  it('stores a description cut inside a surrogate pair, and NULs in any string or key (#2171 review I1)', async () => {
    const cut = `${'a'.repeat(LISTING_CAPS.description - 1)}😀`
    await listings.put(
      SRC,
      toStoredTools([
        { name: 'emoji', description: cut },
        { name: 'nul', description: 'a\u0000b', inputSchema: { type: 'object', default: 'x\u0000' }, 'k\u0000': 1 },
      ]),
    )
    const got = (await listings.get(SRC))!.tools
    expect(got[0]!.description).toHaveLength(LISTING_CAPS.description)
    expect(got[0]!.description!.isWellFormed()).toBe(true)
    expect(got[1]!.description).toBe('a\uFFFDb')
    expect(got[1]!.input_schema).toEqual({ type: 'object', default: 'x\uFFFD' })
    expect(got[1]!.raw['k\uFFFD']).toBe(1)
  })

  it('reads a row whose tools do not parse as no listing', async () => {
    await db.sql`INSERT INTO ai_tool_listings (source, tools) VALUES (${SRC}, ${db.sql.json([{ nope: true }])})`
    expect(await listings.get(SRC)).toBeUndefined()
  })

  describe("a plugin's connection test (#4994)", () => {
    const URL_A = 'https://hs.example/mcp'
    const plugin = (url = URL_A) => db.sql`INSERT INTO ai_plugins (name, kind, url) VALUES ('memory', 'remote_mcp', ${url})`
    const stored = () => listings.get(SRC)

    it("stores the listing while the plugin stands at the URL tested", async () => {
      await plugin()
      const held = await listings.exclusive('memory', (put) => put(URL_A, toStoredTools([{ name: 'a' }]), { truncated: true }))
      expect(held).toEqual({ value: true })
      expect((await stored())!.tools.map((t) => t.name)).toEqual(['a'])
      expect((await stored())!.truncated).toBe(true)
    })

    it('stores nothing once the plugin is deleted or its URL changed', async () => {
      expect(await listings.exclusive('memory', (put) => put(URL_A, toStoredTools([{ name: 'a' }]), {}))).toEqual({ value: false })
      await plugin('https://new.example/mcp')
      expect(await listings.exclusive('memory', (put) => put(URL_A, toStoredTools([{ name: 'a' }]), {}))).toEqual({ value: false })
      expect(await stored()).toBeUndefined()
    })

    it('waits for a URL change that holds the row, then stores nothing', async () => {
      await plugin()
      let release!: () => void
      const locked = new Promise<void>((resolve) => (release = resolve))
      let ready!: () => void
      const holding = new Promise<void>((resolve) => (ready = resolve))
      const change = db.sql.begin(async (tx) => {
        await tx`SELECT 1 FROM ai_plugins WHERE name = 'memory' FOR UPDATE`
        ready()
        await locked
        await tx`UPDATE ai_plugins SET url = 'https://new.example/mcp' WHERE name = 'memory'`
      })
      await holding
      const put = listings.exclusive('memory', (p) => p(URL_A, toStoredTools([{ name: 'a' }]), {}))
      release()
      await change
      expect(await put).toEqual({ value: false })
      expect(await stored()).toBeUndefined()
    })

    it("holds one test per plugin across connections, as two replicas' would be", async () => {
      const other = new ToolListings(db.sql)
      let finish!: () => void
      const running = new Promise<void>((resolve) => (finish = resolve))
      let started!: () => void
      const holding = new Promise<void>((resolve) => (started = resolve))
      const first = listings.exclusive('memory', async () => {
        started()
        await running
        return 'first'
      })
      await holding
      expect(await other.exclusive('memory', async () => 'second')).toBeUndefined()
      expect(await other.exclusive('another', async () => 'another')).toEqual({ value: 'another' })
      finish()
      expect(await first).toEqual({ value: 'first' })
      expect(await other.exclusive('memory', async () => 'again')).toEqual({ value: 'again' })
    })

    it('frees the lock when the test throws', async () => {
      await expect(listings.exclusive('memory', () => Promise.reject(new Error('boom')))).rejects.toThrow('boom')
      expect(await new ToolListings(db.sql).exclusive('memory', async () => 1)).toEqual({ value: 1 })
    })
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
