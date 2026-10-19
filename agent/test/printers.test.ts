import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { summariseInput } from '../src/approvals/service.js'
import { scrubForLog } from '../src/sessions/sdkEvents.js'
import { event } from '../src/sessions/protocol.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #3823 (plan 2026-10-16, Task 11): the printer registry's tools, over
// backend/scadbuddy/api/printers.py. The LAN access code goes to the backend once and
// never comes back: not in a result, a title, a pending action's summary or the log.

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
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...overrides,
  }
}

const ID = '00000000-0000-4000-8000-0000000000aa'
const CODE = '12345678'
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
  link_printer_lan: ['outward', 'PUT /api/v1/printers/{printer_id}/lan'],
  unlink_printer_lan: ['write', 'DELETE /api/v1/printers/{printer_id}/lan'],
  delete_printer: ['outward', 'DELETE /api/v1/printers/{printer_id}'],
} as const

describe('printer registry tools (#3823)', () => {
  it.each(Object.entries(TOOLS))('%s has its tier and route', (name, [risk, route]) => {
    const t = tool(name)
    expect(t.risk).toBe(risk)
    expect(t.routes).toEqual([route])
    expect(t.gated).toBe(risk === 'outward')
  })

  it('link_printer_lan is outward and never echoes the access code', async () => {
    let sent: unknown
    server.use(
      http.put(`${BACKEND}/api/v1/printers/${ID}/lan`, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json(ROW)
      }),
    )
    const t = tool('link_printer_lan')
    expect(t.risk).toBe('outward')
    const args = { printer: ID, host: '10.0.0.9', access_code: CODE }

    const result = await t.execute(args, ctx())

    expect(result.isError).toBeFalsy()
    expect(sent).toEqual({ host: '10.0.0.9', access_code: CODE })
    expect(JSON.stringify(result)).not.toContain(CODE)
    expect(firstText(result)).toMatchObject({ id: ID, has_access_code: true, lan_host: '10.0.0.9' })
    expect(t.summarize(args)).not.toContain(CODE)
    expect(t.title(args)).not.toContain(CODE)
    // The pending action's summary and the session log blank it by its name.
    expect(summariseInput(t.name, args, [])).not.toContain(CODE)
    const logged = scrubForLog(
      event({ type: 'tool.call', sessionId: 's', id: 'c', name: t.name, input: args, risk: 'outward' }),
      [],
    )
    expect(JSON.stringify(logged)).not.toContain(CODE)
  })

  it("link_printer_lan passes the probe's failure through, without the code", async () => {
    server.use(
      http.put(`${BACKEND}/api/v1/printers/${ID}/lan`, () =>
        HttpResponse.json({ detail: 'the printer did not answer at that address' }, { status: 502 }),
      ),
    )
    // `execute` is the handler behind the approval gate; runTool turns this into the error result.
    const failed = await tool('link_printer_lan')
      .execute({ printer: ID, host: '10.0.0.99', access_code: CODE }, ctx())
      .then(
        () => null,
        (error: unknown) => error as Error,
      )
    expect(failed?.message).toContain('the printer did not answer at that address')
    expect(failed?.message).not.toContain(CODE)
  })

  it('link_printer_lan refuses a code that is not 8 digits before calling the backend', async () => {
    const result = await runTool(
      tool('link_printer_lan'),
      { printer: ID, host: '10.0.0.9', access_code: 'abc' },
      ctx({ principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') } }),
    )
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result)).not.toContain('"abc"')
  })

  it('lists printers without their serials', async () => {
    server.use(http.get(`${BACKEND}/api/v1/printers`, () => HttpResponse.json([ROW])))
    const result = await runTool(tool('list_printers'), {}, ctx())
    expect(result.isError).toBeFalsy()
    const listed = firstText(result) as Record<string, unknown>[]
    expect(listed[0]).toMatchObject({ id: ID, name: 'Garage', has_access_code: true })
    expect(JSON.stringify(result)).not.toContain('01P00A0000000AA')
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
})
