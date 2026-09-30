import { describe, expect, it } from 'vitest'
import { z } from 'zod'
import {
  allPages,
  compositeKey,
  cursorPosition,
  DEFAULT_PAGE_SIZE,
  isPaged,
  MAX_PAGE_SIZE,
  page,
  pageInput,
  type Page,
} from '../src/tools/pagination.js'
import { json, ToolError } from '../src/tools/registry.js'
import { ALL_TOOLS as TOOLS } from '../src/tools/index.js'

// The list_* tools page in the agent (#837): keyset cursors over the backend's order.

const rows = (n: number) => Array.from({ length: n }, (_, i) => ({ id: `r${i}` }))
const byId = (r: { id: string }) => r.id

/** Every page, following next_cursor, as page() would serve them. */
function walk<T>(items: readonly T[], key: (t: T) => string, limit?: number): Page<T>[] {
  const pages: Page<T>[] = []
  let cursor: string | undefined
  do {
    const p = page(items, { limit, cursor }, key, 'list_test')
    pages.push(p)
    cursor = p.next_cursor ?? undefined
  } while (cursor)
  return pages
}

describe('page', () => {
  it('serves the default page size first, and counts every item', () => {
    const first = page(rows(60), {}, byId, 'list_test')
    expect(first.items).toHaveLength(DEFAULT_PAGE_SIZE)
    expect(first.total).toBe(60)
    expect(first.next_cursor).toEqual(expect.any(String))
  })

  it('walks to the end without a gap or a repeat, and ends with a null cursor', () => {
    const items = rows(53)
    const pages = walk(items, byId, 10)
    expect(pages.map((p) => p.items.length)).toEqual([10, 10, 10, 10, 10, 3])
    expect(pages.flatMap((p) => p.items)).toEqual(items)
    expect(pages.at(-1)!.next_cursor).toBeNull()
  })

  it('has no next page when the last page is exactly full', () => {
    expect(walk(rows(20), byId, 10).map((p) => p.next_cursor === null)).toEqual([false, true])
  })

  it('answers an empty list with an empty last page', () => {
    expect(page([], {}, byId, 'list_test')).toEqual({ items: [], next_cursor: null, total: 0 })
  })

  it('neither repeats nor skips when an item is inserted before the cursor between pages', () => {
    const before = rows(6)
    const first = page(before, { limit: 3 }, byId, 'list_test')
    const after = [{ id: 'new' }, ...before]
    const second = page(after, { limit: 3, cursor: first.next_cursor! }, byId, 'list_test')
    expect(second.items.map(byId)).toEqual(['r3', 'r4', 'r5'])
  })

  it('refuses a cursor whose item is gone, rather than guessing where to resume', () => {
    const first = page(rows(6), { limit: 3 }, byId, 'list_test')
    const gone = rows(6).filter((r) => r.id !== 'r2')
    expect(() => page(gone, { cursor: first.next_cursor! }, byId, 'list_test')).toThrow(ToolError)
    expect(() => page(gone, { cursor: first.next_cursor! }, byId, 'list_test')).toThrow(/stale/)
  })

  it('refuses a well-formed but foreign cursor as not its own', () => {
    const foreign = Buffer.from('not json', 'utf8').toString('base64url')
    expect(() => page(rows(3), { cursor: foreign }, byId, 'list_test')).toThrow(/not one this tool returned/)
  })

  it("refuses a cursor from another scope, even when that list starts with the same key (every model's model.scad)", () => {
    const files = [{ id: 'model.scad' }, { id: 'lib.scad' }, { id: 'parts.scad' }]
    const first = page(files, { slug: 'a', limit: 1 }, byId, 'list_source_files')
    expect(() => page(files, { slug: 'b', cursor: first.next_cursor! }, byId, 'list_source_files')).toThrow(/another listing/)
    expect(() => page(files, { slug: 'a', cursor: first.next_cursor! }, byId, 'list_presets')).toThrow(/another listing/)
    // The page size is not scope: a cursor serves the same listing at any limit.
    expect(page(files, { slug: 'a', limit: 2, cursor: first.next_cursor! }, byId, 'list_source_files').items).toEqual(files.slice(1))
  })

  it('reports a null total for a list that may be only the start of the collection', () => {
    expect(page(rows(5), {}, byId, 'list_test', { complete: false }).total).toBeNull()
  })

  it('records where the cursor item sat, so a limited backend read can stop there', () => {
    const first = page(rows(60), { limit: 10 }, byId, 'list_test')
    expect(cursorPosition(undefined, 'list_test')).toBe(0)
    expect(cursorPosition(first.next_cursor!, 'list_test')).toBe(10)
    const second = page(rows(60), { limit: 10, cursor: first.next_cursor! }, byId, 'list_test')
    expect(cursorPosition(second.next_cursor!, 'list_test')).toBe(20)
  })

  it('refuses a malformed cursor and an out-of-range limit at the input schema', () => {
    const input = z.object(pageInput)
    expect(input.safeParse({ cursor: 'not a cursor!' }).success).toBe(false)
    expect(input.safeParse({ limit: MAX_PAGE_SIZE + 1 }).success).toBe(false)
    expect(input.safeParse({ limit: 0 }).success).toBe(false)
  })
})

describe('compositeKey', () => {
  it('cannot collide the way a delimiter-joined key can', () => {
    expect(compositeKey('a@b', 'c')).not.toBe(compositeKey('a', 'b@c'))
    expect(compositeKey(null, 'x')).not.toBe(compositeKey('', 'x'))
    // Two installed checkouts that a `${name}@${commit}` key would merge stay distinct pages apart.
    const libs = [
      { name: 'a@b', commit: 'c' },
      { name: 'a', commit: 'b@c' },
      { name: 'z', commit: '1' },
    ]
    const key = (l: { name: string; commit: string }) => compositeKey(l.name, l.commit)
    expect(walk(libs, key, 1).flatMap((p) => p.items)).toEqual(libs)
  })
})

describe('allPages', () => {
  const lister = (items: { id: string }[], extra: Record<string, unknown> = {}) => {
    const calls: Record<string, unknown>[] = []
    const execute = async (args: Record<string, unknown>) => {
      calls.push(args)
      return json({ ...extra, ...page(items, args as { limit?: number; cursor?: string }, byId, 'list_test') })
    }
    return { execute, calls }
  }
  const body = (r: { content: { type: string; text?: string }[] }) => JSON.parse(r.content[0]!.text!)

  it('reads a plain list to its last page and returns the bare array', async () => {
    const items = rows(250)
    const { execute, calls } = lister(items)
    expect(body(await allPages(execute, {}))).toEqual(items)
    expect(calls).toHaveLength(3)
    expect(calls.every((c) => c.limit === MAX_PAGE_SIZE)).toBe(true)
  })

  it("keeps a page's other fields and its array's own name (list_plates' plates)", async () => {
    const items = rows(120)
    const calls: Record<string, unknown>[] = []
    const execute = async (args: Record<string, unknown>) => {
      calls.push(args)
      const { items: plates, ...rest } = page(items, args as { limit?: number; cursor?: string }, byId, 'list_test')
      return json({ default: { name: 'Textured PEI' }, plates, ...rest })
    }
    expect(body(await allPages(execute, { slug: 'box' }))).toEqual({ default: { name: 'Textured PEI' }, plates: items })
    expect(calls).toHaveLength(2)
  })

  it('passes an error through as it came', async () => {
    const failed = { isError: true, content: [{ type: 'text' as const, text: 'list failed (HTTP 502)' }] }
    expect(await allPages(async () => failed, {})).toBe(failed)
  })
})

describe('the paged list tools', () => {
  const PAGED = [
    'list_models',
    'list_outputs',
    'list_plates',
    'list_presets',
    'list_libraries',
    'list_installed_libraries',
    'list_fonts',
    'list_source_files',
    'list_versions',
    'list_print_projects',
    'list_pending_actions',
  ]

  it.each(PAGED)('%s takes limit and cursor and says how to page', (name) => {
    const tool = TOOLS.find((t) => t.name === name)
    expect(tool, name).toBeDefined()
    expect(isPaged(tool!)).toBe(true)
    expect(tool!.description).toContain('next_cursor')
  })

  it('leaves no list_* tool unpaged (list_prints pages in the backend)', () => {
    const unpaged = TOOLS.filter((t) => t.name.startsWith('list_') && !isPaged(t) && !('cursor' in t.shape)).map((t) => t.name)
    expect(unpaged).toEqual([])
  })
})
