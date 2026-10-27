import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { NOT_A_TOOL } from '../src/tools/coverage.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #3823 (plan 2026-10-16, Task 11): the printer registry's tools, over
// backend/scadbuddy/api/printers.py. No tool takes the LAN access code (it is typed only
// in Settings > Printers), and no result carries the serial.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
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
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

const ID = '00000000-0000-4000-8000-0000000000aa'
const ROW = {
  id: ID,
  serial: '01P00A0000000AA',
  name: 'Garage',
  model: 'P1S',
  bambuddy_printer_id: null,
  bambuddy_unlinked_id: null,
  lan_host: '10.0.0.9',
  lan_cert_sha256: 'ab'.repeat(32),
  has_access_code: true,
  preferred_path: 'auto',
  bambuddy_active: true,
  created_at: '2026-10-18T12:00:00Z',
  updated_at: '2026-10-18T12:00:00Z',
}

const TOOLS = {
  list_printers: ['read', 'GET /api/v1/printers'],
  get_printer: ['read', 'GET /api/v1/printers/{printer_id}'],
  discover_printers: ['read', 'GET /api/v1/printers/discover'],
  create_printer: ['write', 'POST /api/v1/printers'],
  update_printer: ['write', 'PATCH /api/v1/printers/{printer_id}'],
  link_printer_bambuddy: ['write', 'PUT /api/v1/printers/{printer_id}/bambuddy'],
  unlink_printer_bambuddy: ['write', 'DELETE /api/v1/printers/{printer_id}/bambuddy'],
  unlink_printer_lan: ['outward', 'DELETE /api/v1/printers/{printer_id}/lan'],
  delete_printer: ['outward', 'DELETE /api/v1/printers/{printer_id}'],
  sync_printers_from_bambuddy: ['outward', 'POST /api/v1/printers/sync-bambuddy'],
} as const

describe('printer registry tools (#3823)', () => {
  it.each(Object.entries(TOOLS))('%s has its tier and route', (name, [risk, route]) => {
    const t = tool(name)
    expect(t.risk).toBe(risk)
    expect(t.routes).toEqual([route])
    expect(t.gated).toBe(risk === 'outward')
  })

  it('takes no access code: the LAN link is made in Settings only (#4340 review I1)', () => {
    expect(ALL_TOOLS.find((t) => t.name === 'link_printer_lan')).toBeUndefined()
    expect(ALL_TOOLS.some((t) => t.routes.includes('PUT /api/v1/printers/{printer_id}/lan'))).toBe(false)
    expect(ALL_TOOLS.some((t) => Object.keys(t.shape).some((key) => /access[-_]?code/i.test(key)))).toBe(false)
    const lan = NOT_A_TOOL.find((entry) => entry.operation === 'PUT /api/v1/printers/{printer_id}/lan')
    expect(lan?.reason).toMatch(/Settings > Printers/)
    for (const name of ['list_printers', 'get_printer']) {
      expect(tool(name).description).toMatch(/Settings > Printers/)
      expect(tool(name).description).toMatch(/browser_navigate/)
    }
  })

  it('unlink_printer_lan is outward: it deletes the stored code (#4340 review I2)', async () => {
    const t = tool('unlink_printer_lan')
    expect(t.gated).toBe(true)
    expect(t.summarize({ printer: ID })).toContain(ID)
    const pending = await runTool(t, { printer: ID }, ctx({ principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') } }))
    // A write-tier caller cannot reach it; nothing was sent to the backend (unhandled requests fail).
    expect(pending.isError).toBe(true)
  })

  it('refuses a serial the backend would, and sends it uppercased (#4340 review M1)', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/printers`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(ROW, { status: 201 })
      }),
    )
    for (const serial of ['short', '01P00A00000000AA-X', 'A'.repeat(33)]) {
      expect((await runTool(tool('create_printer'), { serial, name: 'Garage' }, ctx())).isError).toBe(true)
    }
    expect((await runTool(tool('create_printer'), { serial: 'x'.repeat(8), name: 'n'.repeat(81) }, ctx())).isError).toBe(true)
    await runTool(tool('create_printer'), { serial: ' 01p00a0000000aa ', name: 'Garage' }, ctx())
    expect(bodies).toEqual([{ serial: '01P00A0000000AA', name: 'Garage', preferred_path: 'auto' }])
  })

  it('lists printers without their serials', async () => {
    server.use(http.get(`${BACKEND}/api/v1/printers`, () => HttpResponse.json([ROW])))
    const result = await runTool(tool('list_printers'), {}, ctx())
    expect(result.isError).toBeFalsy()
    const listed = firstText(result) as { items: Record<string, unknown>[]; next_cursor: string | null; total: number }
    expect(listed).toMatchObject({ next_cursor: null, total: 1 })
    expect(listed.items[0]).toMatchObject({ id: ID, name: 'Garage', has_access_code: true })
    expect(JSON.stringify(result)).not.toContain('01P00A0000000AA')
  })

  it('pages a long registry in the agent, keyed by id', async () => {
    const rows = Array.from({ length: 30 }, (_, i) => ({ ...ROW, id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}` }))
    server.use(http.get(`${BACKEND}/api/v1/printers`, () => HttpResponse.json(rows)))
    const first = firstText(await runTool(tool('list_printers'), {}, ctx())) as { items: { id: string }[]; next_cursor: string }
    expect(first.items).toHaveLength(25)
    const second = firstText(await runTool(tool('list_printers'), { cursor: first.next_cursor }, ctx())) as {
      items: { id: string }[]
      next_cursor: string | null
    }
    expect(second.items.map((row) => row.id)).toEqual(rows.slice(25).map((row) => row.id))
    expect(second.next_cursor).toBeNull()
  })

  it('discovers Bambuddy printers without their serials', async () => {
    server.use(
      http.get(`${BACKEND}/api/v1/printers/discover`, () =>
        HttpResponse.json([
          { bambuddy_printer_id: 2, name: 'Workshop', model: 'P1S', serial: '01P00A0000000AA', linked_to: null, linkable: true, reason: null },
        ]),
      ),
    )
    const result = await runTool(tool('discover_printers'), {}, ctx())
    expect(firstText(result)).toEqual([
      { bambuddy_printer_id: 2, name: 'Workshop', model: 'P1S', linked_to: null, linkable: true, reason: null },
    ])
  })

  it('creates a printer by serial and sets its path', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/printers`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json(ROW, { status: 201 })
      }),
      http.patch(`${BACKEND}/api/v1/printers/${ID}`, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ...ROW, preferred_path: 'native' })
      }),
    )
    await runTool(tool('create_printer'), { serial: '01P00A0000000AA', name: 'Garage' }, ctx())
    const updated = await runTool(tool('update_printer'), { printer: ID, preferred_path: 'native' }, ctx())
    expect(bodies).toEqual([
      { serial: '01P00A0000000AA', name: 'Garage', preferred_path: 'auto' },
      { preferred_path: 'native' },
    ])
    expect(firstText(updated)).toMatchObject({ preferred_path: 'native' })
  })

  it('links and unlinks a Bambuddy printer', async () => {
    let linked: unknown
    server.use(
      http.put(`${BACKEND}/api/v1/printers/${ID}/bambuddy`, async ({ request }) => {
        linked = await request.json()
        return HttpResponse.json({ ...ROW, bambuddy_printer_id: 2 })
      }),
      http.delete(`${BACKEND}/api/v1/printers/${ID}/bambuddy`, () =>
        HttpResponse.json({ ...ROW, bambuddy_unlinked_id: 2 }),
      ),
    )
    await runTool(tool('link_printer_bambuddy'), { printer: ID, bambuddy_printer_id: 2 }, ctx())
    expect(linked).toEqual({ bambuddy_printer_id: 2 })
    const unlinked = await runTool(tool('unlink_printer_bambuddy'), { printer: ID }, ctx())
    expect(firstText(unlinked)).toMatchObject({ bambuddy_unlinked_id: 2 })
  })

  it('names why a printer linked to Bambuddy cannot be deleted', async () => {
    server.use(
      http.delete(`${BACKEND}/api/v1/printers/${ID}`, () =>
        HttpResponse.json({ detail: `printer ${ID} is linked to Bambuddy printer 2; unlink it first` }, { status: 409 }),
      ),
    )
    await expect(tool('delete_printer').execute({ printer: ID }, ctx())).rejects.toMatchObject({
      status: 409,
      untrusted: expect.stringContaining('unlink it first'),
    })
  })

  it('syncs from Bambuddy, a dry run first, and passes the summary through (#4439, #4747)', async () => {
    const queries: (string | null)[] = []
    const summary = {
      dry_run: true,
      entries: [{ kind: 'physical', name: 'Garage', bambuddy_printer_id: 1, action: 'created', lan: 'would_try' }],
      virtual_printers_error: null,
      created: 1,
      linked: 0,
      already_linked: 0,
      skipped: 0,
      conflicts: 0,
      lan_linked: 0,
      lan_failed: 0,
    }
    server.use(
      http.post(`${BACKEND}/api/v1/printers/sync-bambuddy`, ({ request }) => {
        queries.push(new URL(request.url).searchParams.get('dry_run'))
        return HttpResponse.json(summary)
      }),
    )
    const t = tool('sync_printers_from_bambuddy')
    expect(t.summarize({ dry_run: true })).toMatch(/nothing written/)
    expect(t.summarize({ dry_run: false })).toMatch(/LAN links/)
    const planned = await t.execute({ dry_run: true }, ctx())
    expect(firstText(planned)).toMatchObject({ created: 1, dry_run: true })
    await t.execute({ dry_run: false }, ctx())
    expect(queries).toEqual(['true', 'false'])
  })
})
