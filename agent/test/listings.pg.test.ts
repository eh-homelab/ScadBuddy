import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { LISTING_CAPS, listedTool, type StoredTool, toStoredTools, ToolListings } from '../src/access/listings.js'
import { type LockClient, TEST_LOCK_CONNECTIONS } from '../src/db.js'
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

describe('ToolListings.exclusive when Postgres is down (#4994)', () => {
  const sql = {} as never
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('answers unreachable, without running the test, when the lock client cannot connect', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    const fn = vi.fn(async () => 'tested')
    const down: LockClient = { take: () => Promise.reject(new Error('connect ECONNREFUSED')), end: async () => {} }
    expect(await new ToolListings(sql, down).exclusive('memory', fn)).toBe('unreachable')
    expect(fn).not.toHaveBeenCalled()
    expect(logged.mock.calls).toEqual([['plugin test lock: no lock connection: connect ECONNREFUSED']])
  })

  it('answers unreachable, and retires the connection, when the lock query fails', async () => {
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {})
    const fn = vi.fn(async () => 'tested')
    const done = vi.fn()
    const broken: LockClient = {
      take: async () => ({ sql: (() => Promise.reject(new Error('connection ended'))) as never, done }),
      end: async () => {},
    }
    expect(await new ToolListings(sql, broken).exclusive('memory', fn)).toBe('unreachable')
    expect(fn).not.toHaveBeenCalled()
    expect(done).toHaveBeenCalledWith(false)
    expect(logged.mock.calls).toEqual([['plugin test lock: the lock query failed, retiring its connection: connection ended']])
  })
})

describe.skipIf(!TEST_DATABASE_URL)(`ToolListings${TEST_DATABASE_URL ? '' : ` (skipped: ${TEST_DATABASE_URL_ENV} is not set)`}`, () => {
  let db: Database
  let drop: () => Promise<void>
  let listings: ToolListings

  beforeEach(async () => {
    ;({ db, drop } = await throwawayDatabase())
    expect(await db.ready()).toBe(true)
    listings = new ToolListings(db.sql, db.locks)
  })
  afterEach(async () => {
    await drop()
  })

  /** A good test's write: the plugin `source` names, stood up at a URL, and its listing stored. */
  async function put(source: string, tools: readonly StoredTool[], options: { truncated?: boolean } = {}): Promise<void> {
    const name = source.slice('remote:'.length)
    const url = `https://${name}.example/mcp`
    await db.sql`INSERT INTO ai_plugins (name, kind, url) VALUES (${name}, 'remote_mcp', ${url}) ON CONFLICT (name) DO NOTHING`
    expect(await listings.exclusive(name, (p) => p(url, tools, options))).toEqual({ value: true })
  }

  it('stores a listing and reads it back with listed_at', async () => {
    expect(await listings.get(SRC)).toBeUndefined()
    await put(SRC, toStoredTools([{ name: 'recall', description: 'Search' }]))
    const got = await listings.get(SRC)
    expect(got!.tools.map((t) => [t.name, t.description])).toEqual([['recall', 'Search']])
    expect(Number.isNaN(Date.parse(got!.listed_at))).toBe(false)
    expect(got!.truncated).toBe(false)
  })

  it('stores whether the listing is incomplete (#2171 re-review N1)', async () => {
    await put(SRC, toStoredTools([{ name: 'a' }]), { truncated: true })
    expect((await listings.get(SRC))!.truncated).toBe(true)
    expect((await listings.getMany([SRC])).get(SRC)!.truncated).toBe(true)
    await put(SRC, toStoredTools([{ name: 'a' }]))
    expect((await listings.get(SRC))!.truncated).toBe(false)
  })

  it('put replaces the last listing', async () => {
    await put(SRC, toStoredTools([{ name: 'a' }]))
    await put(SRC, toStoredTools([{ name: 'b' }, { name: 'c' }]))
    expect((await listings.get(SRC))!.tools.map((t) => t.name)).toEqual(['b', 'c'])
  })

  it('stores each entry once and derives its parts', async () => {
    await put(
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
    await put(
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
      const other = new ToolListings(db.sql, db.locks)
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
      expect(await other.exclusive('memory', async () => 'second')).toBe('busy')
      expect(await other.exclusive('another', async () => 'another')).toEqual({ value: 'another' })
      finish()
      expect(await first).toEqual({ value: 'first' })
      expect(await other.exclusive('memory', async () => 'again')).toEqual({ value: 'again' })
    })

    it("holds no shared-pool connection while tests run, and answers full past this replica's cap, shared by every ToolListings over it", async () => {
      const second = new ToolListings(db.sql, db.locks)
      let finish!: () => void
      const running = new Promise<void>((resolve) => (finish = resolve))
      let count = 0
      let all!: () => void
      const holding = new Promise<void>((resolve) => (all = resolve))
      const tests = Array.from({ length: TEST_LOCK_CONNECTIONS }, (_, i) =>
        (i % 2 === 0 ? listings : second).exclusive(`p${i}`, async () => {
          if (++count === TEST_LOCK_CONNECTIONS) all()
          await running
          return i
        }),
      )
      await holding
      expect(await listings.exclusive('one-more', async () => 'x')).toBe('full')
      expect(await second.exclusive('one-more', async () => 'x')).toBe('full')
      // Every connection of the shared pool at once, while every test holds its lock.
      const reserved = await Promise.all(Array.from({ length: 4 }, () => db.sql.reserve()))
      expect((await Promise.all(reserved.map((c) => c`SELECT 1 AS one`))).map(([r]) => r!.one)).toEqual([1, 1, 1, 1])
      for (const c of reserved) c.release()
      finish()
      expect(await Promise.all(tests)).toEqual([0, 1, 2, 3].map((value) => ({ value })))
      expect(await listings.exclusive('one-more', async () => 'x')).toEqual({ value: 'x' })
    })

    /** db.locks, with every unlock failing as a statement timeout would; `done` records each lease's health. */
    function failingUnlocks() {
      const healthy: boolean[] = []
      const locks: LockClient = {
        async take() {
          const lease = await db.locks.take()
          if (!lease) return undefined
          const sql = ((strings: TemplateStringsArray, ...values: unknown[]) =>
            strings.join('').includes('pg_advisory_unlock')
              ? Promise.reject(new Error('canceling statement due to statement timeout'))
              : lease.sql(strings, ...(values as never[]))) as unknown as typeof lease.sql
          return {
            sql,
            done: (ok) => {
              healthy.push(ok)
              lease.done(ok)
            },
          }
        },
        end: () => db.locks.end(),
      }
      return { listings: new ToolListings(db.sql, locks), healthy }
    }

    it("a failed unlock retires its connection and keeps the test's own answer", async () => {
      const failing = failingUnlocks()
      vi.spyOn(console, 'error').mockImplementation(() => {})
      expect(await failing.listings.exclusive('memory', async () => 'tested')).toEqual({ value: 'tested' })
      expect(failing.healthy).toEqual([false])
      // The retired connection is never reused: once its client ends, the lock it kept is free.
      const free = () => vi.waitFor(async () => expect(await listings.exclusive('memory', async () => 'later')).toEqual({ value: 'later' }))
      await free()
      await expect(failing.listings.exclusive('memory', () => Promise.reject(new Error('the test failed')))).rejects.toThrow('the test failed')
      expect(failing.healthy).toEqual([false, false])
      await free()
    })

    it('frees the lock when the test throws', async () => {
      await expect(listings.exclusive('memory', () => Promise.reject(new Error('boom')))).rejects.toThrow('boom')
      expect(await new ToolListings(db.sql, db.locks).exclusive('memory', async () => 1)).toEqual({ value: 1 })
    })
  })

  describe("a package server's listing (#4757)", () => {
    const H1 = `sha256:${'1'.repeat(64)}`
    const H2 = `sha256:${'2'.repeat(64)}`
    const pkg = (hash: string) => db.sql`
      INSERT INTO ai_plugin_packages (name, source_kind, source_url, source_ref, fetch_url, fetch_path, commit_sha, content_hash, files, review)
      VALUES ('notes', 'git', 'https://git.example/notes.git', 'main', 'https://git.example/notes.git', '', ${'a'.repeat(40)}, ${hash}, '{}', '{}')`

    it('is stored under its server source while the package is pinned to the hash listed, else nothing', async () => {
      expect(await listings.exclusiveServer('notes', 'api', (put) => put(H1, toStoredTools([{ name: 'a' }]), {}))).toEqual({ value: false })
      await pkg(H1)
      expect(await listings.exclusiveServer('notes', 'api', (put) => put(H2, toStoredTools([{ name: 'a' }]), {}))).toEqual({ value: false })
      expect(await listings.get('package:notes/mcp:api')).toBeUndefined()
      expect(await listings.exclusiveServer('notes', 'api', (put) => put(H1, toStoredTools([{ name: 'a' }]), { truncated: true }))).toEqual({
        value: true,
      })
      expect(await listings.get('package:notes/mcp:api')).toMatchObject({ tools: [{ name: 'a' }], truncated: true })
    })

    it('holds one listing per server at a time, apart from a remote plugin of a like name', async () => {
      let finish!: () => void
      const running = new Promise<void>((resolve) => (finish = resolve))
      let started!: () => void
      const holding = new Promise<void>((resolve) => (started = resolve))
      const first = listings.exclusiveServer('notes', 'api', async () => {
        started()
        await running
        return 'first'
      })
      await holding
      const other = new ToolListings(db.sql, db.locks)
      expect(await other.exclusiveServer('notes', 'api', async () => 'second')).toBe('busy')
      expect(await other.exclusiveServer('notes', 'db', async () => 'db')).toEqual({ value: 'db' })
      expect(await other.exclusive('notes', async () => 'remote')).toEqual({ value: 'remote' })
      finish()
      expect(await first).toEqual({ value: 'first' })
    })
  })

  it('getMany reads several sources in one query', async () => {
    await put('remote:aa', toStoredTools([{ name: 'x' }]))
    await put('remote:bb', toStoredTools([{ name: 'y' }]))
    const many = await listings.getMany(['remote:aa', 'remote:bb', 'remote:none'])
    expect([...many.keys()].sort()).toEqual(['remote:aa', 'remote:bb'])
    expect(many.get('remote:bb')!.tools[0]!.name).toBe('y')
    expect((await listings.getMany([])).size).toBe(0)
  })
})
