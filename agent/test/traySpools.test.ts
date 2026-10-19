import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #3818 tier A Task 6c, #4336 review I2: the native path's manual tray link
// (backend/scadbuddy/api/tray_spools.py), as write-tier tools: nothing leaves ScadBuddy.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const PRINTER = '8d6f3c1e-0000-4000-8000-000000000001'
const SPOOL = '8d6f3c1e-0000-4000-8000-0000000000aa'
const PATH = `${BACKEND}/api/v1/printers/${PRINTER}/trays/0/1/spool`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(tier: 'read' | 'write' = 'write'): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo(tier) },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

describe('tray spool tools', () => {
  it.each([
    ['link_tray_spool', 'PUT /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool'],
    ['unlink_tray_spool', 'DELETE /api/v1/printers/{printer_id}/trays/{ams_id}/{tray_id}/spool'],
  ])('%s is write tier, never outward', (name, route) => {
    const t = tool(name)
    expect(t.risk).toBe('write')
    expect(t.readOnly).toBe(false)
    expect(t.gated).toBe(false)
    expect(t.routes).toEqual([route])
  })

  it('links a provider record, sending only the spool fields', async () => {
    const bodies: unknown[] = []
    server.use(
      http.put(PATH, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({
          ams_id: 0,
          tray_id: 1,
          physical_spool_id: SPOOL,
          linked_by: 'manual',
          records: { bambuddy: { record_id: '16', brand: 'Inland' } },
        })
      }),
    )
    const result = await runTool(
      tool('link_tray_spool'),
      { printer_id: PRINTER, ams_id: 0, tray_id: 1, provider: 'bambuddy', record_id: '16' },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ provider: 'bambuddy', record_id: '16' }])
    expect(firstText(result)).toMatchObject({ physical_spool_id: SPOOL, linked_by: 'manual' })
  })

  it('links a physical spool', async () => {
    const bodies: unknown[] = []
    server.use(
      http.put(PATH, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ ams_id: 0, tray_id: 1, physical_spool_id: SPOOL, linked_by: 'manual', records: {} })
      }),
    )
    const result = await runTool(
      tool('link_tray_spool'),
      { printer_id: PRINTER, ams_id: 0, tray_id: 1, physical_spool_id: SPOOL },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ physical_spool_id: SPOOL }])
  })

  it("passes the backend's refusal back", async () => {
    server.use(
      http.put(PATH, () =>
        HttpResponse.json({ detail: "Garage's last report does not show AMS-A slot 2 filled" }, { status: 409 }),
      ),
    )
    const result = await runTool(
      tool('link_tray_spool'),
      { printer_id: PRINTER, ams_id: 0, tray_id: 1, provider: 'bambuddy', record_id: '16' },
      ctx(),
    )
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toContain('HTTP 409')
  })

  it('unlinks a tray', async () => {
    let called = 0
    server.use(
      http.delete(PATH, () => {
        called += 1
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const result = await runTool(tool('unlink_tray_spool'), { printer_id: PRINTER, ams_id: 0, tray_id: 1 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(called).toBe(1)
    expect(firstText(result)).toEqual({ unlinked: { printer_id: PRINTER, ams_id: 0, tray_id: 1 } })
  })

  it('is refused to a read-only principal', async () => {
    const result = await runTool(tool('unlink_tray_spool'), { printer_id: PRINTER, ams_id: 0, tray_id: 1 }, ctx('read'))
    expect(result.isError).toBe(true)
  })

  it('refuses a printer id that is not a uuid before calling the backend', async () => {
    const result = await runTool(tool('unlink_tray_spool'), { printer_id: '7', ams_id: 0, tray_id: 1 }, ctx())
    expect(result.isError).toBe(true)
  })
})
