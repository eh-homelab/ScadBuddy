import { z } from 'zod'
import type { CallToolResult } from '@modelcontextprotocol/sdk/types.js'
import { json, ToolError } from './registry.js'

// Paging for the list_* tools (#837). Most backend list endpoints return the
// whole collection, and a large library returned in one tool result fills the
// turn's context. These tools page in the agent instead, in the backend's own
// order, the way list_prints pages in the backend. list_models and list_outputs,
// which grow with use, have the backend page for them (#843, backendPage): it
// builds only the page asked for, not the whole collection on every page.
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
    .regex(/^[A-Za-z0-9_-]{1,1024}$/, 'must be a next_cursor from the same tool')
    .optional()
    .describe('The previous page\'s next_cursor, for the page after it'),
}

/**
 * The paging arguments, plus any others the listing takes (a `slug`): those scope
 * the cursor, so one issued for one model's list is refused on another's.
 */
export type PageArgs = { limit?: number | undefined; cursor?: string | undefined; [scope: string]: unknown }

export type Page<T> = { items: T[]; next_cursor: string | null; total: number | null }

/** Appended to a list tool's description, so the model knows to follow next_cursor. */
export const PAGED = ` Pages: ${DEFAULT_PAGE_SIZE} items by default (\`limit\` up to ${MAX_PAGE_SIZE}); pass \`next_cursor\` back as \`cursor\` until it is null. \`total\` counts every item, or is null when unknown.`

// The cursor is JSON `[key, position, scope]`: the key is what makes it keyset; the
// position is only a hint, so a cursor can be checked in O(1) when nothing moved,
// and so a tool whose backend takes a `limit` can fetch no further than the page
// it needs (cursorPosition, list_versions). The scope is the tool and its other
// arguments: every model's source files start with `model.scad`, so without it a
// cursor from one model's list would resume another's, silently (#841 review).
const encode = (key: string, position: number, scope: string) =>
  Buffer.from(JSON.stringify([key, position, scope]), 'utf8').toString('base64url')

/** The tool and its non-paging arguments, in a stable order. */
function scopeOf(tool: string, { limit: _limit, cursor: _cursor, ...rest }: PageArgs): string {
  const entries = Object.entries(rest)
    .filter(([, v]) => v !== undefined)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  return JSON.stringify([tool, entries])
}

function decode(cursor: string, tool: string): { key: string; position: number; scope: string } {
  try {
    const value: unknown = JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'))
    if (
      Array.isArray(value) &&
      value.length === 3 &&
      typeof value[0] === 'string' &&
      Number.isInteger(value[1]) &&
      value[1] >= 0 &&
      typeof value[2] === 'string'
    ) {
      return { key: value[0], position: value[1], scope: value[2] }
    }
  } catch {
    // Reported below.
  }
  throw new ToolError(`${tool}: the cursor is not one this tool returned; list again without \`cursor\``, 400)
}

/**
 * A cursor whose item is no longer in the list. Its own class so a tool that read
 * only part of the collection (list_versions) can read further before giving up.
 */
export class StaleCursorError extends ToolError {
  constructor(tool: string) {
    super(`${tool}: the cursor is stale (the item it points after is gone); list again without \`cursor\``, 400)
  }
}

/** Where a cursor's item sat when it was issued: how far into the list the next page starts, at least. */
export const cursorPosition = (cursor: string | undefined, tool: string): number =>
  cursor === undefined ? 0 : decode(cursor, tool).position + 1

/**
 * A cursor key built from several fields, unambiguous whatever the fields hold
 * (a delimiter-joined string would let `a@b` + `c` collide with `a` + `b@c`).
 */
export const compositeKey = (...parts: readonly (string | number | null | undefined)[]): string => JSON.stringify(parts)

export type PageOptions = {
  /**
   * False when `items` may be only the start of the collection (the backend was
   * asked for a limited number): `total` is then null, as the true count is unknown.
   */
  complete?: boolean
}

/** The cursor's item, checked to belong to this listing; undefined for a first page. */
function resume(args: PageArgs, tool: string, scope: string): { key: string; position: number } | undefined {
  if (args.cursor === undefined) return undefined
  const after = decode(args.cursor, tool)
  if (after.scope !== scope) {
    throw new ToolError(
      `${tool}: the cursor belongs to another listing (another tool, or other arguments than this call's); list again without \`cursor\``,
      400,
    )
  }
  return after
}

/**
 * One page of `items`, in their given order. `key` must be unique within the
 * list (a slug, an id; compositeKey for several fields): it is what the cursor
 * records.
 */
export function page<T>(
  items: readonly T[],
  args: PageArgs,
  key: (item: T) => string,
  tool: string,
  { complete = true }: PageOptions = {},
): Page<T> {
  const { limit } = args
  const scope = scopeOf(tool, args)
  let start = 0
  const after = resume(args, tool, scope)
  if (after !== undefined) {
    const hinted = items[after.position]
    const at = hinted !== undefined && key(hinted) === after.key ? after.position : items.findIndex((item) => key(item) === after.key)
    if (at < 0) throw new StaleCursorError(tool)
    start = at + 1
  }
  const size = limit ?? DEFAULT_PAGE_SIZE
  const slice = items.slice(start, start + size)
  const last = slice.at(-1)
  const more = start + size < items.length
  return {
    items: slice,
    next_cursor: more && last !== undefined ? encode(key(last), start + slice.length - 1, scope) : null,
    total: complete ? items.length : null,
  }
}

/** A window of a backend list route that pages itself (#843): its items, and the whole list's length. */
export type BackendWindow<T> = { items: T[]; total: number | null }

/**
 * One page of a backend list route that takes `limit` and `after` (the key of the
 * last item before the window; backend api/models.py `page_window`) and answers the
 * whole list's length in `X-Total-Count`. It asks for one item more than the page,
 * to know whether another follows. The backend answers 409 for an `after` it no
 * longer lists, which is a stale cursor here, as in page().
 */
export async function backendPage<T>(
  args: PageArgs,
  key: (item: T) => string,
  tool: string,
  read: (query: { limit: number; after?: string }) => Promise<BackendWindow<T>>,
): Promise<Page<T>> {
  const scope = scopeOf(tool, args)
  const after = resume(args, tool, scope)
  const size = args.limit ?? DEFAULT_PAGE_SIZE
  let window: BackendWindow<T>
  try {
    window = await read({ limit: size + 1, ...(after === undefined ? {} : { after: after.key }) })
  } catch (err) {
    if (err instanceof ToolError && err.status === 409) throw new StaleCursorError(tool)
    throw err
  }
  const slice = window.items.slice(0, size)
  const last = slice.at(-1)
  const start = after === undefined ? 0 : after.position + 1
  return {
    items: slice,
    next_cursor: window.items.length > size && last !== undefined ? encode(key(last), start + slice.length - 1, scope) : null,
    total: window.total,
  }
}

/**
 * One page of a backend list route that takes `limit` and `offset` and gives no total
 * (farm/archives, #1912). A later page re-reads the cursor's own item at its old offset
 * first: if something else is there now, the list moved under the cursor (a new item
 * ahead of it) and the cursor is stale, as in page(), rather than a page that repeats
 * or skips items. One item more than the page says whether another follows.
 */
export async function offsetPage<T>(
  args: PageArgs,
  key: (item: T) => string,
  tool: string,
  read: (window: { limit: number; offset: number }) => Promise<T[]>,
): Promise<Page<T>> {
  const scope = scopeOf(tool, args)
  const after = resume(args, tool, scope)
  const size = args.limit ?? DEFAULT_PAGE_SIZE
  const window =
    after === undefined
      ? await read({ limit: size + 1, offset: 0 })
      : await read({ limit: size + 2, offset: after.position })
  if (after !== undefined) {
    const first = window.shift()
    if (first === undefined || key(first) !== after.key) throw new StaleCursorError(tool)
  }
  const start = after === undefined ? 0 : after.position + 1
  const slice = window.slice(0, size)
  const last = slice.at(-1)
  return {
    items: slice,
    next_cursor: window.length > size && last !== undefined ? encode(key(last), start + slice.length - 1, scope) : null,
    total: null,
  }
}

/** The whole list's length a paged backend route answered in `X-Total-Count`, or null. */
export function totalCount(response: Response): number | null {
  const header = response.headers.get('X-Total-Count')
  const total = header === null ? NaN : Number(header)
  return Number.isInteger(total) && total >= 0 ? total : null
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
