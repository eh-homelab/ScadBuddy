import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

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

it('store_migrate is outward', () => {
  expect(tool('store_migrate').risk).toBe('outward')
  expect(tool('store_migrate_status').risk).toBe('read')
})

it('store_migrate POSTs the pair and returns the workflow id, not followed as an operation', async () => {
  let seen: { method: string; path: string; body: unknown } | undefined
  server.use(
    http.post(`${BACKEND}/api/v1/store/migrate`, async ({ request }) => {
      seen = { method: request.method, path: new URL(request.url).pathname, body: await request.json() }
      return HttpResponse.json({ workflow_id: 'store-migrate-bambuddy-s3' }, { status: 202 })
    }),
  )
  const t = tool('store_migrate')
  const result = await t.execute({ source: 'bambuddy', target: 's3', batch: 20 }, ctx())
  expect(seen).toEqual({ method: 'POST', path: '/api/v1/store/migrate', body: { source: 'bambuddy', target: 's3', batch: 20 } })
  expect(body(result)).toEqual({ workflow_id: 'store-migrate-bambuddy-s3' })
})

it('store_migrate reports the 409 once the store is already on the target', async () => {
  server.use(
    http.post(`${BACKEND}/api/v1/store/migrate`, () => HttpResponse.json({ detail: 'store_backend is already s3' }, { status: 409 })),
  )
  const t = tool('store_migrate')
  await expect(t.execute({ source: 'bambuddy', target: 's3' }, ctx())).rejects.toThrow(/already s3/)
})

it('store_migrate_status GETs the pair and returns the progress', async () => {
  let asked: URL | undefined
  const status = { source: 'bambuddy', target: 's3', remaining: 3, copied: 7, failed: [{ key: 'k', error: 'boom' }], failed_count: 1, running: true }
  server.use(
    http.get(`${BACKEND}/api/v1/store/migrate`, ({ request }) => {
      asked = new URL(request.url)
      return HttpResponse.json(status)
    }),
  )
  const t = tool('store_migrate_status')
  const result = await t.execute({ source: 'bambuddy', target: 's3' }, ctx())
  expect(asked?.searchParams.get('source')).toBe('bambuddy')
  expect(asked?.searchParams.get('target')).toBe('s3')
  expect(body(result)).toEqual(status)
})

it('store_migrate surfaces a 422 for an invalid pair', async () => {
  server.use(
    http.post(`${BACKEND}/api/v1/store/migrate`, () => HttpResponse.json({ detail: 'source and target are the same backend' }, { status: 422 })),
  )
  await expect(tool('store_migrate').execute({ source: 's3', target: 's3' }, ctx())).rejects.toThrow(/same backend/)
})

it('store_migrate_status surfaces a 404 when no migration ran', async () => {
  server.use(http.get(`${BACKEND}/api/v1/store/migrate`, () => HttpResponse.json({ detail: 'no store migration ran' }, { status: 404 })))
  await expect(tool('store_migrate_status').execute({ source: 'bambuddy', target: 's3' }, ctx())).rejects.toThrow(/no store migration ran/)
})
