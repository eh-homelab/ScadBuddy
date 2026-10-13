import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { principalHeader } from '../src/tools/authorship.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, services } from './helpers/mcp.js'

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(overrides: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

const reader = (id: string) => ({ id, kind: 'bearer' as const, tiers: ['read' as const] })

function listed(): { asked: () => URL | undefined } {
  let asked: URL | undefined
  server.use(http.get(`${BACKEND}/api/v1/activity`, ({ request }) => {
    asked = new URL(request.url)
    return HttpResponse.json({ items: [], next_cursor: null })
  }))
  return { asked: () => asked }
}

it('activity_list pins a read-tier caller to itself', async () => {
  const l = listed()
  await tool('activity_list').execute({ principal: 'browser' }, ctx({ principal: reader('token:t1') }))
  expect(l.asked()?.searchParams.get('principal')).toBe('token:t1')
})

it('activity_list lets an outward caller filter anyone', async () => {
  const l = listed()
  await tool('activity_list').execute({ principal: 'browser' }, ctx())
  expect(l.asked()?.searchParams.get('principal')).toBe('browser')
})

it('activity_list pins the encoded principal', async () => {
  const l = listed()
  const id = 'oidc:https://idp.example/#sub one'
  await tool('activity_list').execute({}, ctx({ principal: { id, kind: 'oidc', tiers: ['read'] } }))
  expect(l.asked()?.searchParams.get('principal')).toBe(principalHeader(id))
})

it('activity_get refuses another principal’s row to a read-tier caller', async () => {
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () =>
    HttpResponse.json({ row: { id: 'render:a', principal: 'browser' }, children: [], links: [], detail: {} })))
  const result = await runTool(tool('activity_get'), { activity_id: 'render:a' }, ctx({ principal: reader('token:t1') }))
  expect(result.isError).toBe(true)
})

it('activity_get gives a read-tier caller its own row', async () => {
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () =>
    HttpResponse.json({ row: { id: 'render:a', principal: 'token:t1' }, children: [], links: [], detail: {} })))
  const result = await runTool(tool('activity_get'), { activity_id: 'render:a' }, ctx({ principal: reader('token:t1') }))
  expect(result.isError).toBeFalsy()
})

it('activity_watch refuses another principal’s row to a read-tier caller', async () => {
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () =>
    HttpResponse.json({ row: { id: 'render:a', principal: 'browser', status: 'running' }, children: [], links: [], detail: {} })))
  const result = await runTool(tool('activity_watch'), { activity_id: 'render:a', wait_seconds: 1 },
    ctx({ principal: reader('token:t1'), pollIntervalMs: 10 }))
  expect(result.isError).toBe(true)
})

it('activity_watch returns when the row finishes', async () => {
  let reads = 0
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () => {
    reads += 1
    const status = reads < 3 ? 'running' : 'succeeded'
    return HttpResponse.json({ row: { id: 'render:a', principal: 'test', status }, children: [], links: [], detail: {} })
  }))
  const result = await tool('activity_watch').execute({ activity_id: 'render:a', wait_seconds: 5 },
    ctx({ pollIntervalMs: 10 }))
  expect(JSON.stringify(result.content)).toContain('succeeded')
  expect(reads).toBe(3)
})

it('activity_get leaves out another principal’s child rows for a read-tier caller', async () => {
  server.use(http.get(`${BACKEND}/api/v1/activity/render%3Aa`, () =>
    HttpResponse.json({
      row: { id: 'render:a', principal: 'token:t1' },
      children: [{ id: 'render:b', principal: 'token:t1' }, { id: 'render:c', principal: 'browser' }],
      links: [],
      detail: {},
    })))
  const result = await tool('activity_get').execute({ activity_id: 'render:a' }, ctx({ principal: reader('token:t1') }))
  const text = JSON.stringify(result.content)
  expect(text).toContain('render:b')
  expect(text).not.toContain('render:c')
})
