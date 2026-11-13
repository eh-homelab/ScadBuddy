import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

// #6119 — MakerWorld through ScadBuddy's API, which calls Bambuddy with its own key: the
// agent reaches only the backend's /makerworld routes, never Bambuddy.

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
  pollIntervalMs: 1,
  commandFollowMs: 50,
})

const body = (result: unknown): unknown => JSON.parse((result as { content: { text: string }[] }).content[0]!.text)

const IMPORTED = {
  library: 'native',
  library_file_id: 1000000007,
  folder_id: 1000000002,
  filename: 'Benchy.3mf',
  profile_id: 77,
  bambuddy_library_file_id: 9,
  bambuddy_folder_id: 31,
  was_existing: false,
}

it('status and resolve are read, import is outward', () => {
  expect(tool('get_makerworld_status').risk).toBe('read')
  expect(tool('resolve_makerworld_url').risk).toBe('read')
  expect(tool('import_makerworld_model').risk).toBe('outward')
})

it('the import tells a Bambuddy folder from a native one', () => {
  const description = tool('import_makerworld_model').description
  expect(description).toMatch(/print project's `folder_id`/)
  expect(description).toMatch(/never a native folder/)
})

it('get_makerworld_status reads the backend route', async () => {
  server.use(
    http.get(`${BACKEND}/api/v1/makerworld/status`, () =>
      HttpResponse.json({ has_cloud_token: true, can_download: true, sign_in_expired: false }),
    ),
  )
  expect(body(await tool('get_makerworld_status').execute({}, ctx()))).toEqual({
    has_cloud_token: true,
    can_download: true,
    sign_in_expired: false,
  })
})

it('resolve_makerworld_url posts the link', async () => {
  let seen: unknown
  const resolved = {
    model_id: 123,
    profile_id: 456,
    design: { title: 'Benchy' },
    instances: [{ profileId: 456 }],
    already_imported_bambuddy_ids: [9],
    already_imported_library_ids: [1000000007],
    library: 'native',
  }
  server.use(
    http.post(`${BACKEND}/api/v1/makerworld/resolve`, async ({ request }) => {
      seen = await request.json()
      return HttpResponse.json(resolved)
    }),
  )
  const url = 'https://makerworld.com/en/models/123#profileId-456'
  expect(body(await tool('resolve_makerworld_url').execute({ url }, ctx()))).toEqual(resolved)
  expect(seen).toEqual({ url })
})

it('import_makerworld_model sends a key and answers the library file', async () => {
  let seen: { body: unknown; key: string | null } | undefined
  server.use(
    http.post(`${BACKEND}/api/v1/makerworld/import`, async ({ request }) => {
      seen = { body: await request.json(), key: request.headers.get('Idempotency-Key') }
      return HttpResponse.json(IMPORTED)
    }),
  )
  const t = tool('import_makerworld_model')
  const result = await t.execute({ model_id: 123, profile_id: 77, bambuddy_folder_id: 31 }, ctx())
  expect(body(result)).toEqual(IMPORTED)
  expect(seen?.body).toEqual({ model_id: 123, profile_id: 77, folder_id: null, bambuddy_folder_id: 31 })
  expect(seen?.key).toMatch(/^[0-9a-f]{32}$/)
  expect(t.summarize?.({ model_id: 123, profile_id: 77, bambuddy_folder_id: 31 })).toBe(
    'Import MakerWorld model 123 (profile 77) into the library, Bambuddy folder 31',
  )
})

it('a slow import answers the running operation to follow with get_operation', async () => {
  const operation = { id: 'a'.repeat(32), kind: 'makerworld_import', subject: 'makerworld:123:default', status: 'running' }
  server.use(
    http.post(`${BACKEND}/api/v1/makerworld/import`, () => HttpResponse.json(operation, { status: 202 })),
    http.get(`${BACKEND}/api/v1/operations/${operation.id}`, () => HttpResponse.json(operation)),
  )
  const answer = body(await tool('import_makerworld_model').execute({ model_id: 123 }, ctx())) as Record<string, unknown>
  expect(answer.status).toBe('running')
  expect(answer.operation_id).toBe(operation.id)
})

it('import_makerworld_model reports the backend refusal', async () => {
  server.use(
    http.post(`${BACKEND}/api/v1/makerworld/import`, () =>
      HttpResponse.json({ detail: '`folder_id` names a folder of ScadBuddy\'s own library, and library_backend is bambuddy' }, { status: 422 }),
    ),
  )
  await expect(tool('import_makerworld_model').execute({ model_id: 123, folder_id: 5 }, ctx())).rejects.toThrow(/bambuddy_folder_id|library_backend is bambuddy/)
})
