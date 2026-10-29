import type { Sql } from 'postgres'
import { z } from 'zod'

// The tools a remote source listed (#2171, #778), kept in `ai_tool_listings`: each
// `tools/list` entry once, as the server sent it, with only what a cap removed taken
// out and named. Its parts (title, schemas) are derived when read, never stored twice.
// Every field is untrusted server text: the caps bound what one server can make us store.

/** Characters of JSON text. `other`: an entry's fields besides the known ones; `total`: the whole listing. */
export const LISTING_CAPS = { tools: 512, description: 8_192, schema: 65_536, other: 16_384, total: 2_097_152 } as const

export type Omitted = 'description' | 'inputSchema' | 'outputSchema' | 'other'

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

function otherKeys(raw: Record<string, unknown>): string[] {
  return Object.keys(raw).filter((k) => !KNOWN.has(k))
}

function omit(tool: StoredTool, keys: readonly string[], as: Omitted): void {
  if (!keys.some((k) => Object.hasOwn(tool.raw, k))) return
  for (const k of keys) delete tool.raw[k]
  if (!tool.omitted.includes(as)) tool.omitted.push(as)
}

function capEntry(entry: Record<string, unknown>): StoredTool {
  const tool: StoredTool = { raw: { ...entry }, omitted: [] }
  const description = tool.raw.description
  if (typeof description === 'string') {
    if (description.length > LISTING_CAPS.description) {
      tool.raw.description = description.slice(0, LISTING_CAPS.description)
      tool.omitted.push('description')
    }
  } else if (description !== undefined && jsonLength(description) > LISTING_CAPS.description) {
    omit(tool, ['description'], 'description')
  }
  for (const key of ['inputSchema', 'outputSchema'] as const) {
    if (tool.raw[key] !== undefined && jsonLength(tool.raw[key]) > LISTING_CAPS.schema) omit(tool, [key], key)
  }
  const others = otherKeys(tool.raw)
  if (others.length > 0 && jsonLength(Object.fromEntries(others.map((k) => [k, tool.raw[k]]))) > LISTING_CAPS.other) {
    omit(tool, others, 'other')
  }
  return tool
}

/** The entries as stored: nameless ones dropped, each capped, then the listing capped as a whole. */
export function toStoredTools(entries: readonly Record<string, unknown>[]): StoredTool[] {
  const tools = entries
    .filter((e) => isRecord(e) && typeof e.name === 'string')
    .slice(0, LISTING_CAPS.tools)
    .map(capEntry)
  const steps: [(t: StoredTool) => string[], Omitted][] = [
    [(t) => otherKeys(t.raw), 'other'],
    [() => ['outputSchema'], 'outputSchema'],
    [() => ['inputSchema'], 'inputSchema'],
  ]
  for (const [keys, as] of steps) {
    if (jsonLength(tools) <= LISTING_CAPS.total) break
    for (const t of tools) omit(t, keys(t), as)
  }
  // Only names, titles, descriptions and annotations are left: past the total, the last tools go.
  while (tools.length > 0 && jsonLength(tools) > LISTING_CAPS.total) tools.pop()
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
    omitted: z.array(z.enum(['description', 'inputSchema', 'outputSchema', 'other'])),
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
