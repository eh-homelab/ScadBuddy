import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// #5355 — a copy between Bambuddy's library and ScadBuddy's own: started as a job
// (not followed as an operation), read back by its id, and Bambuddy's library browsed
// as the source while the native library serves the page.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

const ctx = (): ToolContext => ({
  ...services(),
  principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
  progress: async () => {},
  signal: new AbortController().signal,
})

const body = (result: unknown): unknown => JSON.parse((result as { content: { text: string }[] }).content[0]!.text)

it('copy_library_files is outward and get_library_copy is read', () => {
  expect(tool('copy_library_files').risk).toBe('outward')
  expect(tool('get_library_copy').risk).toBe('read')
})

it('copy_library_files POSTs the selection and returns the copy id, not followed as an operation', async () => {
  let seen: { path: string; body: unknown; headers: Headers } | undefined
  server.use(
    http.post(`${BACKEND}/api/v1/print/library-copies`, async ({ request }) => {
      seen = { path: new URL(request.url).pathname, body: await request.json(), headers: request.headers }
      return HttpResponse.json({ copy_id: 'library-copy-abc' }, { status: 202 })
    }),
  )
  const t = tool('copy_library_files')
  const result = await t.execute({ direction: 'bb_to_sb', file_ids: [7], folder_ids: [3], target_folder_id: 1000000002 }, ctx())
  expect(seen?.path).toBe('/api/v1/print/library-copies')
  expect(seen?.body).toEqual({ direction: 'bb_to_sb', file_ids: [7], folder_ids: [3], target_folder_id: 1000000002 })
  expect(seen?.headers.get('Idempotency-Key')).toBeNull()
  expect(body(result)).toEqual({ copy_id: 'library-copy-abc' })
  expect(t.summarize?.({ direction: 'bb_to_sb', file_ids: [7], folder_ids: [3] })).toBe(
    "Copy 1 file and 1 folder from Bambuddy's library into ScadBuddy's",
  )
})

it('copy_library_files sends empty lists for what is not named, and the backend refuses an empty selection', async () => {
  let seen: unknown
  server.use(
    http.post(`${BACKEND}/api/v1/print/library-copies`, async ({ request }) => {
      seen = await request.json()
      return HttpResponse.json({ detail: 'name at least one file or folder to copy' }, { status: 422 })
    }),
  )
  await expect(tool('copy_library_files').execute({ direction: 'sb_to_bb' }, ctx())).rejects.toThrow(/at least one/)
  expect(seen).toEqual({ direction: 'sb_to_bb', file_ids: [], folder_ids: [], target_folder_id: null })
})

it('copy_library_files reports the 409 when the native library does not serve', async () => {
  server.use(
    http.post(`${BACKEND}/api/v1/print/library-copies`, () =>
      HttpResponse.json({ detail: 'copying between the libraries needs ScadBuddy\'s own library: set library_backend to native first' }, { status: 409 }),
    ),
  )
  await expect(tool('copy_library_files').execute({ direction: 'bb_to_sb', file_ids: [7] }, ctx())).rejects.toThrow(/library_backend/)
})

it('get_library_copy GETs the job by id and returns its status', async () => {
  const status = {
    copy_id: 'library-copy-abc',
    direction: 'bb_to_sb',
    request: { direction: 'bb_to_sb', file_ids: [7], folder_ids: [], target_folder_id: null },
    status: 'done',
    running: false,
    error: null,
    started_at: '2026-11-06T00:00:00Z',
    finished_at: '2026-11-06T00:00:05Z',
    total: 1,
    pending: 0,
    copied: 1,
    skipped_same_hash: 0,
    failed: 0,
    files: [{ source_id: 7, path: '', name: 'rex.3mf', size: 3, status: 'copied', reason: null, attempts: 1, reason_try: null, target_id: 1000000001, target_folder_id: null }],
  }
  let asked: string | undefined
  server.use(
    http.get(`${BACKEND}/api/v1/print/library-copies/:id`, ({ params }) => {
      asked = String(params.id)
      return HttpResponse.json(status)
    }),
  )
  const result = await tool('get_library_copy').execute({ copy_id: 'library-copy-abc' }, ctx())
  expect(asked).toBe('library-copy-abc')
  expect(body(result)).toEqual(status)
})

it('get_library_copy surfaces the 404 for an id no copy has', async () => {
  server.use(http.get(`${BACKEND}/api/v1/print/library-copies/:id`, () => HttpResponse.json({ detail: 'no library copy nope' }, { status: 404 })))
  await expect(tool('get_library_copy').execute({ copy_id: 'nope' }, ctx())).rejects.toThrow(/no library copy/)
})

it("list_library with source bambuddy browses Bambuddy's library route, unpaged", async () => {
  let asked: URL | undefined
  const listing = { folder_id: 3, all: true, folders: [{ id: 3, name: 'Dinos', parent_id: null, depth: 0 }], files: [], hidden: 0 }
  server.use(
    http.get(`${BACKEND}/api/v1/print/bambuddy-library`, ({ request }) => {
      asked = new URL(request.url)
      return HttpResponse.json(listing)
    }),
  )
  const result = await tool('list_library').execute({ folder_id: 3, source: 'bambuddy' }, ctx())
  expect(asked?.searchParams.get('folder_id')).toBe('3')
  expect(body(result)).toEqual(listing)
})
