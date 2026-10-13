import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #1912 (#251's farm-context tier): the queue, aggregate stats, every Bambuddy archive
// with its outcome, and the spool inventory, read through backend/scadbuddy/api/farm.py.

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
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('read') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

const FARM_TOOLS = {
  list_print_queue: 'GET /api/v1/farm/queue',
  get_print_stats: 'GET /api/v1/farm/stats',
  list_print_archives: 'GET /api/v1/farm/archives',
  get_spool_inventory: 'GET /api/v1/farm/inventory',
} as const

/** The query string each call sent, by path. */
function capture(path: string, body: unknown): URLSearchParams[] {
  const seen: URLSearchParams[] = []
  server.use(
    http.get(`${BACKEND}${path}`, ({ request }) => {
      seen.push(new URL(request.url).searchParams)
      return HttpResponse.json(body as never)
    }),
  )
  return seen
}

describe('farm-context tools (#1912)', () => {
  it.each(Object.entries(FARM_TOOLS))('%s reads only, with the Read Status scope', (name, route) => {
    const t = tool(name)
    expect(t.risk).toBe('read')
    expect(t.readOnly).toBe(true)
    expect(t.gated).toBe(false)
    expect(t.bambuddyScope).toEqual(['Read Status'])
    expect(t.routes).toEqual([route])
    expect(t.source).toMatch(/Bambuddy/)
  })

  it('lists the queue with its filters, paging in the agent', async () => {
    const seen = capture('/api/v1/farm/queue', [{ id: 257, status: 'printing' }])
    const result = await runTool(tool('list_print_queue'), { printer_id: 1, status: 'pending', limit: 10 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(Object.fromEntries(seen[0]!)).toEqual({ printer_id: '1', status: 'pending' })
    expect(firstText(result)).toEqual({ items: [{ id: 257, status: 'printing' }], next_cursor: null, total: 1 })
  })

  it('follows next_cursor through a long queue', async () => {
    const rows = Array.from({ length: 30 }, (_, i) => ({ id: i + 1 }))
    const seen = capture('/api/v1/farm/queue', rows)
    const first = firstText(await runTool(tool('list_print_queue'), {}, ctx())) as { items: unknown[]; next_cursor: string }
    expect(first.items).toHaveLength(25)
    const second = firstText(await runTool(tool('list_print_queue'), { cursor: first.next_cursor }, ctx()))
    expect(second).toMatchObject({ items: rows.slice(25), next_cursor: null, total: 30 })
    expect(Object.fromEntries(seen[0]!)).toEqual({})
  })

  it('refuses a queue status Bambuddy does not have', () => {
    expect(() => tool('list_print_queue').parse({ status: 'queued' })).toThrow()
  })

  it('reads the stats over a window', async () => {
    const seen = capture('/api/v1/farm/stats', { total_prints: 104 })
    const result = await runTool(tool('get_print_stats'), { from: '2026-09-01', to: '2026-09-30' }, ctx())
    expect(result.isError).toBeFalsy()
    expect(Object.fromEntries(seen[0]!)).toEqual({ date_from: '2026-09-01', date_to: '2026-09-30' })
    expect(firstText(result)).toMatchObject({ total_prints: 104 })
  })

  it('refuses a day that is not YYYY-MM-DD', () => {
    expect(() => tool('get_print_stats').parse({ from: '1 Sept' })).toThrow(/YYYY-MM-DD/)
  })

  it('lists archives a window at a time, one more than the page to know if another follows', async () => {
    const seen = capture('/api/v1/farm/archives', [{ id: 144 }, { id: 143 }, { id: 142 }, { id: 141 }])
    const result = await runTool(
      tool('list_print_archives'),
      { printer_id: 1, project_id: 4, from: '2026-10-01', to: '2026-10-09', limit: 3 },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(Object.fromEntries(seen[0]!)).toEqual({
      printer_id: '1',
      project_id: '4',
      date_from: '2026-10-01',
      date_to: '2026-10-09',
      limit: '4',
      offset: '0',
    })
    expect(firstText(result)).toMatchObject({ items: [{ id: 144 }, { id: 143 }, { id: 142 }], total: null })
  })

  it("resumes after the cursor's own archive, re-read to check nothing moved", async () => {
    const all = Array.from({ length: 5 }, (_, i) => ({ id: 100 - i }))
    const seen: URLSearchParams[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/farm/archives`, ({ request }) => {
        const q = new URL(request.url).searchParams
        seen.push(q)
        const offset = Number(q.get('offset'))
        return HttpResponse.json(all.slice(offset, offset + Number(q.get('limit'))))
      }),
    )
    const first = firstText(await runTool(tool('list_print_archives'), { limit: 2 }, ctx())) as { next_cursor: string }
    const second = firstText(await runTool(tool('list_print_archives'), { limit: 2, cursor: first.next_cursor }, ctx()))
    const third = firstText(
      await runTool(tool('list_print_archives'), { limit: 2, cursor: (second as { next_cursor: string }).next_cursor }, ctx()),
    )
    expect(second).toMatchObject({ items: [{ id: 98 }, { id: 97 }] })
    expect(third).toMatchObject({ items: [{ id: 96 }], next_cursor: null })
    expect(seen.map((q) => [q.get('offset'), q.get('limit')])).toEqual([
      ['0', '3'],
      ['1', '4'],
      ['3', '4'],
    ])
  })

  it('calls a cursor stale when a new archive moved the list', async () => {
    let rows = [{ id: 100 }, { id: 99 }, { id: 98 }]
    server.use(http.get(`${BACKEND}/api/v1/farm/archives`, () => HttpResponse.json(rows)))
    const first = firstText(await runTool(tool('list_print_archives'), { limit: 1 }, ctx())) as { next_cursor: string }
    rows = [{ id: 101 }, { id: 100 }, { id: 99 }]
    const result = await runTool(tool('list_print_archives'), { limit: 1, cursor: first.next_cursor }, ctx())
    expect(result.isError).toBe(true)
    expect(JSON.stringify(firstText(result))).toMatch(/stale/)
  })

  it('refuses a cursor from other filters', async () => {
    server.use(http.get(`${BACKEND}/api/v1/farm/archives`, () => HttpResponse.json([{ id: 2 }, { id: 1 }])))
    const first = firstText(await runTool(tool('list_print_archives'), { limit: 1 }, ctx())) as { next_cursor: string }
    const result = await runTool(tool('list_print_archives'), { limit: 1, printer_id: 3, cursor: first.next_cursor }, ctx())
    expect(result.isError).toBe(true)
    expect(JSON.stringify(firstText(result))).toMatch(/another listing/)
  })

  it('caps an archive page at a hundred', () => {
    expect(() => tool('list_print_archives').parse({ limit: 101 })).toThrow()
  })

  it('reads the inventory, archived spools only when asked', async () => {
    const seen = capture('/api/v1/farm/inventory', { spools: [], slots: [] })
    await runTool(tool('get_spool_inventory'), {}, ctx())
    await runTool(tool('get_spool_inventory'), { include_archived: true, printer_id: 1 }, ctx())
    expect(seen.map((q) => Object.fromEntries(q))).toEqual([{}, { include_archived: 'true', printer_id: '1' }])
  })

  it("passes the backend's scope problem through", async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/farm/stats`, () =>
        HttpResponse.json(
          {
            type: 'https://scadbuddy.dev/problems/bambuddy-scope',
            title: 'Bambuddy API key scope',
            status: 409,
            detail: "Bambuddy refused the API key when asked to read the print statistics. The key needs the 'Read Status' scope",
            required_scope: 'Read Status',
          },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const result = await runTool(tool('get_print_stats'), {}, ctx())
    expect(result.isError).toBe(true)
    expect(JSON.stringify(firstText(result))).toContain('Read Status')
  })
})
