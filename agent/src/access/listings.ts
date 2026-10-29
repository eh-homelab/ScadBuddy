import type { Sql, TransactionSql } from 'postgres'
import { TEST_LOCK_CONNECTIONS } from '../db.js'
import { z } from 'zod'
import { remoteSource } from './model.js'

// The tools a remote source listed (#2171, #778), kept in `ai_tool_listings`: each
// `tools/list` entry once, as the server sent it, with only what a cap removed taken
// out and named. Its parts (title, schemas) are derived when read, never stored twice.
// Every field is untrusted server text: the caps bound what one server can make us store.

/**
 * Characters of JSON text. `name` and `title` are string lengths; `other`: an entry's fields
 * besides the known ones, together; `annotations` is held to `other` too; `total`: the whole listing.
 */
export const LISTING_CAPS = {
  tools: 512,
  name: 128,
  title: 1_024,
  description: 8_192,
  schema: 65_536,
  other: 16_384,
  total: 2_097_152,
} as const

/** `keys`: two keys of the entry became one once made storable; the first was kept. */
export const OMITTED = ['description', 'inputSchema', 'outputSchema', 'other', 'title', 'annotations', 'keys'] as const
export type Omitted = (typeof OMITTED)[number]

export type StoredTool = { raw: Record<string, unknown>; omitted: Omitted[] }

export type ListedTool = {
  name: string
  title: string | null
  description: string | null
  annotations: Record<string, unknown> | null
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  raw: Record<string, unknown>
  omitted: Omitted[]
}

const KNOWN = new Set(['name', 'title', 'description', 'inputSchema', 'outputSchema', 'annotations'])

function jsonLength(value: unknown): number {
  return JSON.stringify(value)?.length ?? 0
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** What jsonb accepts: no lone surrogate, no NUL (each becomes U+FFFD). */
function storable(text: string): string {
  return text.toWellFormed().replaceAll('\u0000', '\uFFFD')
}

/** `value` with every string and key `storable`; of keys that become equal, the first is kept and `lost.keys` set. */
function clean(value: unknown, lost: { keys: boolean }): unknown {
  if (typeof value === 'string') return storable(value)
  if (Array.isArray(value)) return value.map((v) => clean(v, lost))
  if (!isRecord(value)) return value
  const out = new Map<string, unknown>()
  for (const [k, v] of Object.entries(value)) {
    const key = storable(k)
    if (out.has(key)) lost.keys = true
    else out.set(key, clean(v, lost))
  }
  return Object.fromEntries(out)
}

/** The first `max` code units, not ending in half a surrogate pair. */
function cut(text: string, max: number): string {
  return text.slice(0, max).toWellFormed()
}

function otherKeys(raw: Record<string, unknown>): string[] {
  return Object.keys(raw).filter((k) => !KNOWN.has(k))
}

function omit(tool: StoredTool, keys: readonly string[], as: Omitted): void {
  if (!keys.some((k) => Object.hasOwn(tool.raw, k))) return
  for (const k of keys) delete tool.raw[k]
  if (!tool.omitted.includes(as)) tool.omitted.push(as)
}

/** Cuts a string field to `max`, or removes a non-string one whose JSON is over it. */
function capText(tool: StoredTool, key: 'description' | 'title', max: number): void {
  const value = tool.raw[key]
  if (value === undefined) return
  if (typeof value !== 'string') {
    if (jsonLength(value) > max) omit(tool, [key], key)
  } else if (value.length > max) {
    tool.raw[key] = cut(value, max)
    tool.omitted.push(key)
  }
}

function capEntry(entry: Record<string, unknown>): StoredTool {
  const lost = { keys: false }
  const tool: StoredTool = { raw: clean(entry, lost) as Record<string, unknown>, omitted: lost.keys ? ['keys'] : [] }
  capText(tool, 'description', LISTING_CAPS.description)
  capText(tool, 'title', LISTING_CAPS.title)
  for (const key of ['inputSchema', 'outputSchema'] as const) {
    if (tool.raw[key] !== undefined && jsonLength(tool.raw[key]) > LISTING_CAPS.schema) omit(tool, [key], key)
  }
  const others = otherKeys(tool.raw)
  if (others.length > 0 && jsonLength(Object.fromEntries(others.map((k) => [k, tool.raw[k]]))) > LISTING_CAPS.other) {
    omit(tool, others, 'other')
  }
  if (tool.raw.annotations !== undefined && jsonLength(tool.raw.annotations) > LISTING_CAPS.other) {
    omit(tool, ['annotations'], 'annotations')
  }
  return tool
}

/** The listing's JSON length: each tool measured once, plus the brackets and commas. */
function listingLength(tools: readonly StoredTool[]): number {
  let total = 2 + Math.max(tools.length - 1, 0)
  for (const t of tools) total += jsonLength(t)
  return total
}

/**
 * The entries as stored: those without a string name (or with one over `LISTING_CAPS.name`,
 * which cannot take an access) dropped, the first `LISTING_CAPS.tools` kept, each capped,
 * then the listing capped as a whole, each removal named in the tool's `omitted`.
 */
export function toStoredTools(entries: readonly unknown[]): StoredTool[] {
  const tools = entries
    .filter((e): e is Record<string, unknown> => isRecord(e) && typeof e.name === 'string' && e.name.length <= LISTING_CAPS.name)
    .slice(0, LISTING_CAPS.tools)
    .map(capEntry)
  const steps: [(t: StoredTool) => string[], Omitted][] = [
    [(t) => otherKeys(t.raw), 'other'],
    [() => ['outputSchema'], 'outputSchema'],
    [() => ['inputSchema'], 'inputSchema'],
    [() => ['annotations'], 'annotations'],
    [() => ['description'], 'description'],
  ]
  for (const [keys, as] of steps) {
    if (listingLength(tools) <= LISTING_CAPS.total) return tools
    for (const t of tools) omit(t, keys(t), as)
  }
  // Names and titles only, and still over: from the first tool whose title does not fit beside
  // the names after it, each keeps only its name. 512 names of 128 characters fit whatever they hold.
  const full = tools.map(jsonLength)
  const bare = tools.map((t) => jsonLength({ raw: { name: t.raw.name }, omitted: [...OMITTED] }))
  let rest = bare.reduce((sum, n) => sum + n, 2 + Math.max(tools.length - 1, 0))
  let kept = 0
  for (const i of tools.keys()) {
    rest -= bare[i]!
    kept += full[i]!
    if (kept + rest > LISTING_CAPS.total) {
      for (const stub of tools.slice(i)) omit(stub, ['title'], 'title')
      break
    }
  }
  return tools
}

export function listedTool(stored: StoredTool): ListedTool {
  const { raw } = stored
  const annotations = isRecord(raw.annotations) ? raw.annotations : null
  const title = typeof raw.title === 'string' ? raw.title : typeof annotations?.title === 'string' ? annotations.title : null
  return {
    name: String(raw.name),
    title,
    description: typeof raw.description === 'string' ? raw.description : null,
    annotations,
    input_schema: isRecord(raw.inputSchema) ? raw.inputSchema : null,
    output_schema: isRecord(raw.outputSchema) ? raw.outputSchema : null,
    raw,
    omitted: stored.omitted,
  }
}

const StoredTools = z.array(
  z.object({
    raw: z.looseObject({ name: z.string() }),
    omitted: z.array(z.enum(OMITTED)),
  }),
)

/** `truncated`: not every tool the source has (ToolListings.put's option). */
type Listing = { tools: ListedTool[]; listed_at: string; truncated: boolean }
type Row = { tools: unknown; listed_at: Date; truncated: boolean }

function parseRow(row: Row): Listing | undefined {
  const parsed = StoredTools.safeParse(row.tools)
  if (!parsed.success) return undefined
  return { tools: parsed.data.map(listedTool), listed_at: row.listed_at.toISOString(), truncated: row.truncated }
}

export class ToolListings {
  readonly #sql: Sql
  readonly #locks: () => Sql
  #held = 0

  /** `locks`: the lock client (db.ts `Database.lockClient`), asked for on the first test. */
  constructor(sql: Sql, locks: () => Sql) {
    this.#sql = sql
    this.#locks = locks
  }

  async get(source: string): Promise<Listing | undefined> {
    const [row] = await this.#sql<Row[]>`
      SELECT tools, listed_at, truncated FROM ai_tool_listings WHERE source = ${source}`
    return row ? parseRow(row) : undefined
  }

  async getMany(sources: readonly string[]): Promise<Map<string, Listing>> {
    const out = new Map<string, Listing>()
    if (sources.length === 0) return out
    const rows = await this.#sql<(Row & { source: string })[]>`
      SELECT source, tools, listed_at, truncated FROM ai_tool_listings WHERE source = ANY(${[...sources]}::text[])`
    for (const row of rows) {
      const listing = parseRow(row)
      if (listing) out.set(row.source, listing)
    }
    return out
  }

  /**
   * Runs `fn` holding remote plugin `name`'s test lock, a session advisory lock, so one test
   * runs at a time across replicas (#4994). `busy`: another session holds it; `full`: this
   * replica already runs TEST_LOCK_CONNECTIONS tests. The lock is held on the lock client
   * (db.ts `lockClient`), never on the shared pool, which a test therefore never waits on.
   */
  async exclusive<T>(name: string, fn: (put: PluginListingPut) => Promise<T>): Promise<{ value: T } | 'busy' | 'full'> {
    // Every reserve below is counted here, so none waits for another test's connection.
    if (this.#held >= TEST_LOCK_CONNECTIONS) return 'full'
    this.#held++
    try {
      const conn = await this.#locks().reserve()
      try {
        const key = `scadbuddy:ai_tool_listings:test:${name}`
        const [row] = await conn<{ held: boolean }[]>`SELECT pg_try_advisory_lock(hashtextextended(${key}, 0)) AS held`
        if (!row?.held) return 'busy'
        try {
          return { value: await fn((url, tools, options) => this.#putIfCurrent(name, url, tools, options.truncated ?? false)) }
        } finally {
          await conn`SELECT pg_advisory_unlock(hashtextextended(${key}, 0))`
        }
      } finally {
        conn.release()
      }
    } finally {
      this.#held--
    }
  }

  // One short transaction on the shared pool, after the test and still under its lock.
  async #putIfCurrent(name: string, url: string, tools: readonly StoredTool[], truncated: boolean): Promise<boolean> {
    return this.#sql.begin(async (tx) => {
      const [plugin] = await tx`SELECT 1 FROM ai_plugins WHERE name = ${name} AND url = ${url} FOR UPDATE`
      if (!plugin) return false
      await upsert(tx, remoteSource(name), tools, truncated)
      return true
    })
  }
}

/**
 * Stores remote plugin `name`'s listing only while it still exists at `url`, the one tested
 * (#4994): under its ai_plugins row lock, so a delete or a URL change after the test wins.
 * False: nothing stored. `truncated`: not every tool the source has (a `nextCursor`, or entries dropped).
 */
export type PluginListingPut = (url: string, tools: readonly StoredTool[], options: { truncated?: boolean }) => Promise<boolean>

async function upsert(sql: TransactionSql, source: string, tools: readonly StoredTool[], truncated: boolean): Promise<void> {
  await sql`
    INSERT INTO ai_tool_listings (source, tools, truncated)
    VALUES (${source}, ${sql.json(tools as never)}, ${truncated})
    ON CONFLICT (source) DO UPDATE SET tools = EXCLUDED.tools, truncated = EXCLUDED.truncated, listed_at = now()`
}
