import type { Sql } from 'postgres'
import { z } from 'zod'

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

export const OMITTED = ['description', 'inputSchema', 'outputSchema', 'other', 'title', 'annotations'] as const
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

/** `value` with every string and key `storable`. */
function clean(value: unknown): unknown {
  if (typeof value === 'string') return storable(value)
  if (Array.isArray(value)) return value.map(clean)
  if (isRecord(value)) return Object.fromEntries(Object.entries(value).map(([k, v]) => [storable(k), clean(v)]))
  return value
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
  const tool: StoredTool = { raw: clean(entry) as Record<string, unknown>, omitted: [] }
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

type Listing = { tools: ListedTool[]; listed_at: string }

function parseRow(row: { tools: unknown; listed_at: Date }): Listing | undefined {
  const parsed = StoredTools.safeParse(row.tools)
  if (!parsed.success) return undefined
  return { tools: parsed.data.map(listedTool), listed_at: row.listed_at.toISOString() }
}

export class ToolListings {
  readonly #sql: Sql

  constructor(sql: Sql) {
    this.#sql = sql
  }

  async get(source: string): Promise<Listing | undefined> {
    const [row] = await this.#sql<{ tools: unknown; listed_at: Date }[]>`
      SELECT tools, listed_at FROM ai_tool_listings WHERE source = ${source}`
    return row ? parseRow(row) : undefined
  }

  async getMany(sources: readonly string[]): Promise<Map<string, Listing>> {
    const out = new Map<string, Listing>()
    if (sources.length === 0) return out
    const rows = await this.#sql<{ source: string; tools: unknown; listed_at: Date }[]>`
      SELECT source, tools, listed_at FROM ai_tool_listings WHERE source = ANY(${[...sources]}::text[])`
    for (const row of rows) {
      const listing = parseRow(row)
      if (listing) out.set(row.source, listing)
    }
    return out
  }

  async put(source: string, tools: readonly StoredTool[]): Promise<void> {
    await this.#sql`
      INSERT INTO ai_tool_listings (source, tools) VALUES (${source}, ${this.#sql.json(tools as never)})
      ON CONFLICT (source) DO UPDATE SET tools = EXCLUDED.tools, listed_at = now()`
  }
}
