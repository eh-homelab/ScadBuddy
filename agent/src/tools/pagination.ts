import { z } from 'zod'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { json, ToolError } from './registry.js'

// Paging for the list_* tools (#837). Most backend list endpoints return the
// whole collection, and a large library returned in one tool result fills the
// turn's context. These tools page in the agent instead, in the backend's own
// order, the way list_prints pages in the backend.
//
// The cursor is keyset, not an offset: it names the last item a page returned
// (base64url, so the model treats it as opaque), and the next page starts after
// that item wherever it now sits. An insert elsewhere in the list therefore
// neither repeats nor skips an item; only the removal of that exact item makes
// the cursor stale, and that is refused with a message rather than guessed at.

export const DEFAULT_PAGE_SIZE = 25
export const MAX_PAGE_SIZE = 100

export const pageInput = {
  limit: z
    .number()
    .int()
    .min(1)
    .max(MAX_PAGE_SIZE)
    .optional()
    .describe(`Items per page, ${DEFAULT_PAGE_SIZE} by default`),
  cursor: z
    .string()
    .regex(/^[A-Za-z0-9_-]{1,512}$/, 'must be a next_cursor from the same tool')
    .optional()
    .describe('The previous page\'s next_cursor, for the page after it'),
}

export type PageArgs = { limit?: number | undefined; cursor?: string | undefined }

export type Page<T> = { items: T[]; next_cursor: string | null; total: number }

/** Appended to a list tool's description, so the model knows to follow next_cursor. */
export const PAGED = ` Pages: ${DEFAULT_PAGE_SIZE} items by default (\`limit\` up to ${MAX_PAGE_SIZE}); pass \`next_cursor\` back as \`cursor\` until it is null. \`total\` counts every item.`

const encode = (key: string) => Buffer.from(key, 'utf8').toString('base64url')
const decode = (cursor: string) => Buffer.from(cursor, 'base64url').toString('utf8')

/**
 * One page of `items`, in their given order. `key` must be unique within the
 * list (a slug, an id, a name): it is what the cursor records.
 */
export function page<T>(items: readonly T[], { limit, cursor }: PageArgs, key: (item: T) => string, tool: string): Page<T> {
  let start = 0
  if (cursor !== undefined) {
    const after = decode(cursor)
    const at = items.findIndex((item) => key(item) === after)
    if (at < 0) {
      throw new ToolError(
        `${tool}: the cursor is stale (the item it points after is gone); list again without \`cursor\``,
        400,
      )
    }
    start = at + 1
  }
  const size = limit ?? DEFAULT_PAGE_SIZE
  const slice = items.slice(start, start + size)
  const last = slice.at(-1)
  const more = start + size < items.length
  return { items: slice, next_cursor: more && last !== undefined ? encode(key(last)) : null, total: items.length }
}

/**
 * Every page of a paged tool, merged. A page holds its items in one array field
 * (`items`, or a name the tool kept, like list_plates' `plates`) beside
 * `next_cursor` and `total`; the merged result is that array with every item,
 * as the bare array when it is a plain list, so a resource's contents did not
 * change shape with paging (#837). For callers that need the whole collection:
 * an MCP resource read, argument completion (resources/server.ts). The model's
 * own calls stay paged. A result that is not a page is returned as it came.
 */
export async function allPages(
  execute: (args: Record<string, unknown>) => Promise<CallToolResult>,
  args: Record<string, unknown>,
): Promise<CallToolResult> {
  const first = await execute({ ...args, limit: MAX_PAGE_SIZE })
  const opened = pageBody(first)
  if (!opened) return first
  const { field } = opened
  let body = opened.body
  const items = [...(body[field] as unknown[])]
  while (typeof body.next_cursor === 'string') {
    const next = await execute({ ...args, limit: MAX_PAGE_SIZE, cursor: body.next_cursor })
    const more = pageBody(next)
    if (!more || more.field !== field) return next
    items.push(...(more.body[field] as unknown[]))
    body = more.body
  }
  const { next_cursor: _done, total: _total, [field]: _page, ...rest } = body
  return json(field === 'items' && Object.keys(rest).length === 0 ? items : { ...rest, [field]: items })
}

function pageBody(result: CallToolResult): { body: Record<string, unknown>; field: string } | undefined {
  if (result.isError) return undefined
  const first = result.content[0]
  if (result.content.length !== 1 || first?.type !== 'text') return undefined
  let body: unknown
  try {
    body = JSON.parse(first.text)
  } catch {
    return undefined
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body) || !('next_cursor' in body)) return undefined
  const arrays = Object.entries(body).filter(([, v]) => Array.isArray(v))
  return arrays.length === 1 ? { body: body as Record<string, unknown>, field: arrays[0]![0] } : undefined
}

/** Whether a tool takes this module's paging arguments (and so answers a Page). */
export const isPaged = (tool: { shape: z.ZodRawShape }): boolean => tool.shape.cursor === pageInput.cursor
