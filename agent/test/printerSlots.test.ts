import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { idempotencyKey, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #7153, #7276: a printer's AMS slots over ScadBuddy's LAN link (backend/scadbuddy/api/printer_slots.py).
// A write that reaches the printer is outward and approved first (R-7153-10); the slot presets and
// the unit labels are ScadBuddy's own, so they are write (R-7276-13).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const PRINTER = '8d6f3c1e-0000-4000-8000-000000000001'
const BASE = `${BACKEND}/api/v1/printers/${PRINTER}`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(tier: 'read' | 'outward' = 'outward'): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo(tier) },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

const COMMANDS = ['printer_slot_configure', 'printer_slot_reset', 'printer_slot_refresh', 'printer_slot_load', 'printer_slot_unload']
const OWN = ['printer_slot_preset_set', 'printer_slot_preset_clear', 'printer_ams_label_set', 'printer_ams_label_clear']
const COMMAND = `lan-command-${PRINTER}-ams_change_filament-${'b'.repeat(64)}`

describe('AMS slot tools (#7153)', () => {
  it.each(COMMANDS)('%s is outward and approved first', (name) => {
    expect(tool(name).risk).toBe('outward')
    expect(tool(name).gated).toBe(true)
    expect(tool(name).description).toMatch(/not_lan_linked/)
  })

  it.each(OWN)('%s writes ScadBuddy only', (name) => {
    expect(tool(name).risk).toBe('write')
    expect(tool(name).gated).toBe(false)
  })

  it('sends a slot command under the call key and follows its 202 (P51)', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post(`${BASE}/trays/0/1/unload`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })
      }),
      http.get(`${BASE}/commands/:command_id`, () =>
        HttpResponse.json({ ams_id: 0, tray_id: 1, action: 'unload', message: 'sent' }),
      ),
    )
    const t = tool('printer_slot_unload')
    const call = { ...ctx(), callKey: 'call-7' }
    const result = await t.execute(t.parse({ printer_id: PRINTER, ams_id: 0, tray_id: 1 }), call)
    expect(firstText(result)).toEqual({ ams_id: 0, tray_id: 1, action: 'unload', message: 'sent' })
    expect(keys).toEqual([idempotencyKey(call)])
  })

  it('sends a configure under the call key', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post(`${BASE}/trays/0/2/configure`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })
      }),
      http.get(`${BASE}/commands/:command_id`, () => HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })),
    )
    const t = tool('printer_slot_configure')
    const call = { ...ctx(), callKey: 'call-8', commandFollowMs: 0 }
    const result = firstText(await t.execute(t.parse({ printer_id: PRINTER, ams_id: 0, tray_id: 2, material: 'PLA' }), call))
    expect(result).toMatchObject({ status: 'running', command_id: COMMAND })
    expect(keys).toEqual([idempotencyKey(call)])
  })

  it('reads the AMS at read tier', async () => {
    server.use(http.get(`${BASE}/ams`, () => HttpResponse.json({ printer_id: PRINTER, units: [] })))
    const t = tool('printer_ams_get')
    expect(t.risk).toBe('read')
    const result = await t.execute(t.parse({ printer_id: PRINTER }), ctx('read'))
    expect(firstText(result)).toEqual({ printer_id: PRINTER, units: [] })
  })

  it('loads a slot by its unit and slot', async () => {
    const posted: string[] = []
    server.use(
      http.post(`${BASE}/trays/:ams/:tray/load`, ({ params }) => {
        posted.push(`${params.ams}/${params.tray}`)
        return HttpResponse.json({ ams_id: 0, tray_id: 1, action: 'load', message: 'Loading' })
      }),
    )
    const t = tool('printer_slot_load')
    const result = await t.execute(t.parse({ printer_id: PRINTER, ams_id: 0, tray_id: 1 }), ctx())
    expect(firstText(result)).toMatchObject({ action: 'load' })
    expect(posted).toEqual(['0/1'])
  })

  it('sends a configure body without the path fields', async () => {
    let body: unknown
    server.use(
      http.post(`${BASE}/trays/0/2/configure`, async ({ request }) => {
        body = await request.json()
        return HttpResponse.json({ ams_id: 0, tray_id: 2 })
      }),
    )
    const t = tool('printer_slot_configure')
    await t.execute(t.parse({ printer_id: PRINTER, ams_id: 0, tray_id: 2, material: 'PLA', colour: '#FFFFFF' }), ctx())
    expect(body).toEqual({ material: 'PLA', colour: '#FFFFFF' })
  })

  it('says why a printing printer is refused', async () => {
    server.use(
      http.post(`${BASE}/trays/0/1/reset`, () =>
        HttpResponse.json(
          { title: 'Conflict', status: 409, detail: 'H2C is printing (RUNNING)', code: 'printer_busy' },
          { status: 409, headers: { 'content-type': 'application/problem+json' } },
        ),
      ),
    )
    const t = tool('printer_slot_reset')
    await expect(t.execute(t.parse({ printer_id: PRINTER, ams_id: 0, tray_id: 1 }), ctx())).rejects.toThrow(
      /HTTP 409.*printing/,
    )
  })

  it('refuses a label that is too long and puts a good one', async () => {
    const t = tool('printer_ams_label_set')
    expect(() => t.parse({ printer_id: PRINTER, ams_id: 0, label: 'x'.repeat(101) })).toThrow()
    let sent: unknown
    server.use(
      http.put(`${BASE}/ams/0/label`, async ({ request }) => {
        sent = await request.json()
        return HttpResponse.json({ ams_id: 0, label: 'Left rack', place: null, place_problem: null })
      }),
    )
    await t.execute(t.parse({ printer_id: PRINTER, ams_id: 0, label: 'Left rack' }), ctx())
    expect(sent).toEqual({ label: 'Left rack' })
  })
})
