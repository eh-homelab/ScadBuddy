import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { printerKProfileTools } from '../src/tools/printerKProfiles.js'
import { printerSlotTools } from '../src/tools/printerSlots.js'
import { idempotencyKey, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #7162, #7276 Task 10: a printer's K-profiles over ScadBuddy's LAN link
// (backend/scadbuddy/api/printer_kprofiles.py). A write is a printer command keyed by the
// call's Idempotency-Key; a 202 `LanCommandPending` is followed at
// GET /printers/{id}/commands/{command_id} (P51), else handed back for printer_command_get.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const PRINTER = '8d6f3c1e-0000-4000-8000-000000000001'
const BASE = `${BACKEND}/api/v1/printers/${PRINTER}`
const COMMAND = `lan-command-${PRINTER}-extrusion_cali_set-${'a'.repeat(64)}`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(extra: Partial<ToolContext> = {}): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    ...extra,
  }
}

const PROFILE = {
  nozzle_id: 'HS00-0.4',
  nozzle_diameter: '0.4',
  filament_id: 'GFA00',
  name: 'Bambu PLA Basic',
  k_value: '0.020',
}

describe('K-profile tools (#7162)', () => {
  it('lists the K-profiles through the LAN route', async () => {
    const asked: (string | null)[] = []
    server.use(
      http.get(`${BASE}/kprofiles`, ({ request }) => {
        asked.push(new URL(request.url).searchParams.get('nozzle_diameter'))
        return HttpResponse.json({ profiles: [], nozzle_diameter: '0.6' })
      }),
    )
    const t = tool('printer_kprofiles_get')
    const result = await t.execute(t.parse({ printer_id: PRINTER, nozzle_diameter: '0.6' }), ctx())
    expect(firstText(result)).toEqual({ profiles: [], nozzle_diameter: '0.6' })
    await t.execute(t.parse({ printer_id: PRINTER }), ctx())
    expect(asked).toEqual(['0.6', '0.4'])
  })

  it('sends a set once under the call key', async () => {
    const keys: (string | null)[] = []
    let body: unknown
    server.use(
      http.post(`${BASE}/kprofiles`, async ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        body = await request.json()
        return HttpResponse.json({ success: true, confirmed: true, message: '' })
      }),
    )
    const t = tool('printer_kprofile_set')
    const call = ctx({ callKey: 'call-1' })
    await t.execute(t.parse({ printer_id: PRINTER, ...PROFILE }), call)
    await t.execute(t.parse({ printer_id: PRINTER, ...PROFILE }), call)
    expect(keys).toEqual([idempotencyKey(call), idempotencyKey(call)])
    expect(body).toEqual(PROFILE)
  })

  it('sends a lost answer again under the same key', async () => {
    const keys: (string | null)[] = []
    server.use(
      http.post(`${BASE}/kprofiles/delete`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        if (keys.length === 1) return new HttpResponse('bad gateway', { status: 502 })
        return HttpResponse.json({ success: true, confirmed: true, message: '' })
      }),
    )
    const t = tool('printer_kprofile_delete')
    const result = await t.execute(
      t.parse({ printer_id: PRINTER, slot_id: 3, nozzle_id: 'HS00-0.4', nozzle_diameter: '0.4', filament_id: 'GFA00' }),
      ctx(),
    )
    expect(firstText(result)).toEqual({ success: true, confirmed: true, message: '' })
    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })

  it('follows a 202 to the answer the route would have given (P51)', async () => {
    let follows = 0
    server.use(
      http.post(`${BASE}/kprofiles/batch`, () => HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })),
      http.get(`${BASE}/commands/:command_id`, ({ params }) => {
        expect(params.command_id).toBe(COMMAND)
        follows++
        if (follows < 2) return HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })
        return HttpResponse.json({ success: true, confirmed: false, message: 'the printer did not acknowledge it in time' })
      }),
    )
    const t = tool('printer_kprofile_batch_set')
    const result = await t.execute(t.parse({ printer_id: PRINTER, profiles: [PROFILE, { ...PROFILE, extruder_id: 1 }] }), ctx())
    expect(firstText(result)).toMatchObject({ success: true, confirmed: false })
    expect(follows).toBe(2)
  })

  it('hands back a command still running, to read with printer_command_get', async () => {
    let posts = 0
    server.use(
      http.post(`${BASE}/kprofiles`, () => {
        posts++
        return HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })
      }),
      http.get(`${BASE}/commands/:command_id`, () => HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })),
    )
    const t = tool('printer_kprofile_set')
    const result = firstText(await t.execute(t.parse({ printer_id: PRINTER, ...PROFILE }), ctx({ commandFollowMs: 0 }))) as {
      status: string
      command_id: string
      next: string
    }
    expect(result.status).toBe('running')
    expect(result.command_id).toBe(COMMAND)
    expect(result.next).toMatch(/printer_command_get/)
    expect(result.next).toMatch(/do not send it again/)
    expect(posts).toBe(1)
  })

  it('reads a command with printer_command_get', async () => {
    let answer: 'running' | 'done' = 'running'
    server.use(
      http.get(`${BASE}/commands/:command_id`, () =>
        answer === 'running'
          ? HttpResponse.json({ workflow_id: COMMAND }, { status: 202 })
          : HttpResponse.json({ profiles: [], nozzle_diameter: '0.4' }),
      ),
    )
    const t = tool('printer_command_get')
    expect(t.risk).toBe('read')
    const running = firstText(await t.execute(t.parse({ printer_id: PRINTER, command_id: COMMAND }), ctx({ commandFollowMs: 0 })))
    expect(running).toMatchObject({ status: 'running', command_id: COMMAND })
    answer = 'done'
    const done = firstText(await t.execute(t.parse({ printer_id: PRINTER, command_id: COMMAND }), ctx()))
    expect(done).toEqual({ profiles: [], nozzle_diameter: '0.4' })
    expect(() => t.parse({ printer_id: PRINTER, command_id: '../../models' })).toThrow()
  })

  it('tiers reads read, writes outward, notes write', () => {
    const tiers = Object.fromEntries(printerKProfileTools.map((t) => [t.name, t.risk]))
    expect(tiers).toEqual({
      printer_kprofiles_get: 'read',
      printer_kprofile_notes_get: 'read',
      printer_kprofile_set: 'outward',
      printer_kprofile_batch_set: 'outward',
      printer_kprofile_delete: 'outward',
      printer_kprofile_note_set: 'write',
      printer_kprofile_note_delete: 'write',
      printer_command_get: 'read',
    })
  })

  it('refuses a k_value with seven places', () => {
    const t = tool('printer_kprofile_set')
    expect(() => t.parse({ printer_id: PRINTER, ...PROFILE, k_value: '0.0200001' })).toThrow()
    expect(() => t.parse({ printer_id: PRINTER, ...PROFILE, k_value: '10' })).toThrow()
    expect(() => t.parse({ printer_id: PRINTER, ...PROFILE, k_value: '0.020001' })).not.toThrow()
  })

  it('refuses a batch of mixed nozzles or more than 16', () => {
    const t = tool('printer_kprofile_batch_set')
    expect(() => t.parse({ printer_id: PRINTER, profiles: [PROFILE, { ...PROFILE, nozzle_diameter: '0.6' }] })).toThrow()
    expect(() => t.parse({ printer_id: PRINTER, profiles: Array(17).fill(PROFILE) })).toThrow()
    expect(() => t.parse({ printer_id: PRINTER, profiles: [] })).toThrow()
  })

  it('sets and deletes a note in ScadBuddy only', async () => {
    let put: unknown
    const deleted: string[] = []
    server.use(
      http.put(`${BASE}/kprofiles/notes`, async ({ request }) => {
        put = await request.json()
        return HttpResponse.json({ success: true, confirmed: true, message: '' })
      }),
      http.delete(`${BASE}/kprofiles/notes/:setting_id`, ({ params }) => {
        deleted.push(String(params.setting_id))
        return HttpResponse.json({ success: true, confirmed: true, message: '' })
      }),
    )
    const set = tool('printer_kprofile_note_set')
    await set.execute(set.parse({ printer_id: PRINTER, setting_id: 'PF00000000000000001', note: 'tuned' }), ctx())
    expect(put).toEqual({ setting_id: 'PF00000000000000001', note: 'tuned' })
    expect(() => set.parse({ printer_id: PRINTER, setting_id: 'a/b', note: 'x' })).toThrow()
    const del = tool('printer_kprofile_note_delete')
    await del.execute(del.parse({ printer_id: PRINTER, setting_id: 'PF00000000000000001' }), ctx())
    expect(deleted).toEqual(['PF00000000000000001'])
  })

  it('no slot or K-profile tool says Bambuddy', () => {
    for (const t of [...printerKProfileTools, ...printerSlotTools]) {
      expect(`${t.name}: ${t.description} ${t.source}`).not.toMatch(/bambuddy/i)
    }
  })
})
