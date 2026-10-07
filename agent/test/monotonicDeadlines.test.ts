import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { PendingActionStore } from '../src/tools/pending.js'
import { RenderLimiter } from '../src/tools/renderLimits.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// The wall clock may step (NTP; on WSL it jumps about ±11 s every few seconds), so a
// deadline timed with Date.now() expires early or late (#1485). Each test here steps
// Date.now() by an hour on every read and checks the deadline holds anyway.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => {
  server.resetHandlers()
  vi.restoreAllMocks()
})
afterAll(() => server.close())

const HOUR = 3_600_000

/** Date.now() moves by `stepMs` more on every read. */
function steppingWallClock(stepMs: number): void {
  const real = Date.now.bind(Date)
  let offset = 0
  vi.spyOn(Date, 'now').mockImplementation(() => {
    offset += stepMs
    return real() + offset
  })
}

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
    pollIntervalMs: 1,
    ...overrides,
  }
}

describe('deadlines on a stepping wall clock (#1485)', () => {
  it('render_model waits for a render though the wall clock jumps forward', async () => {
    let reads = 0
    server.use(
      http.get(`${BACKEND}/api/v1/models/box/schema`, () => HttpResponse.json({ groups: [], parameters: [] })),
      http.post(`${BACKEND}/api/v1/models/box/render`, () => HttpResponse.json({ job_id: 'j', status_url: '' }, { status: 202 })),
      http.get(`${BACKEND}/api/v1/jobs/j`, () => {
        reads += 1
        return HttpResponse.json({ id: 'j', slug: 'box', created_at: '', status: reads < 3 ? 'running' : 'done' })
      }),
    )
    steppingWallClock(HOUR)
    const result = await runTool(tool('render_model'), { slug: 'box' }, ctx({ renderWaitMs: 10_000 }))
    expect(firstText(result)).toMatchObject({ status: 'done' })
  })

  const op = { id: 'op-1', kind: 'reprint', subject: 'archive:35', status: 'running', created_at: '2026-10-03T00:00:00Z' }

  it('a command follows its operation though the wall clock jumps forward', async () => {
    let reads = 0
    server.use(
      http.post(`${BACKEND}/api/v1/prints/35/reprint`, () => HttpResponse.json(op, { status: 202 })),
      http.get(`${BACKEND}/api/v1/operations/op-1`, () => {
        reads += 1
        return HttpResponse.json(reads < 3 ? op : { ...op, status: 'succeeded', result: { queue_item_id: 51 } })
      }),
    )
    steppingWallClock(HOUR)
    const result = await runTool({ ...tool('print_again'), gated: false }, { archive_id: 35 }, ctx({ commandFollowMs: 10_000 }))
    expect(result.isError).toBeFalsy()
    expect(JSON.stringify(result.content)).toContain('51')
  })

  it('a command stops following when its window ends though the wall clock jumps back', async () => {
    server.use(
      http.post(`${BACKEND}/api/v1/prints/35/reprint`, () => HttpResponse.json(op, { status: 202 })),
      http.get(`${BACKEND}/api/v1/operations/op-1`, () => HttpResponse.json(op)),
    )
    steppingWallClock(-HOUR)
    const result = await runTool({ ...tool('print_again'), gated: false }, { archive_id: 35 }, ctx({ commandFollowMs: 50 }))
    expect(firstText(result)).toMatchObject({ status: 'running', operation_id: 'op-1' })
  }, 5_000)

  it('the default render limiter counts its window on a monotonic clock', () => {
    steppingWallClock(HOUR)
    const limiter = new RenderLimiter({ concurrent: 10, perWindow: 1, windowMs: 60_000, holdMs: 60_000 })
    limiter.acquire('p')()
    expect(() => limiter.acquire('p')).toThrow(/most allowed/)
  })

  it('a pending action does not expire when the wall clock jumps forward', async () => {
    const store = new PendingActionStore({ ttlMs: 60_000 })
    const principal = { id: 'p', kind: 'browser', tiers: tiersUpTo('outward') } as const
    const action = await store.prepare(principal, { tool: 'send_to_bambuddy', input: {}, summary: 'send it' })
    steppingWallClock(HOUR)
    expect(await store.find(action.id, principal)).toBeDefined()
  })
})
