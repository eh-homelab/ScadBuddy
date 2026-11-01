import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #4643: ScadBuddy's own spool inventory (backend/scadbuddy/api/native_spools.py):
// reads, and write-tier edits that leave ScadBuddy for nowhere.

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const SPOOL = '8d6f3c1e-0000-4000-8000-0000000004b3'
const LIST = `${BACKEND}/api/v1/native-spools`
const ONE = `${LIST}/${SPOOL}`

function tool(name: string): Tool {
  const t = ALL_TOOLS.find((x) => x.name === name)
  if (!t) throw new Error(`no tool ${name}`)
  return t
}

function ctx(): ToolContext {
  return {
    ...services(),
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('write') },
    progress: async () => {},
    signal: new AbortController().signal,
  }
}

describe('native spool tools', () => {
  it.each([
    ['native_spool_list', 'read'],
    ['native_spool_get', 'read'],
    ['native_spool_upsert', 'write'],
    ['native_spool_delete', 'write'],
  ])('%s is %s tier, never gated', (name, risk) => {
    expect(tool(name).risk).toBe(risk)
    expect(tool(name).gated).toBe(false)
  })

  it('adds a spool with only the fields given', async () => {
    const bodies: unknown[] = []
    server.use(
      http.post(LIST, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: SPOOL, material: 'PLA' }, { status: 201 })
      }),
    )
    const result = await runTool(tool('native_spool_upsert'), { material: 'PLA', remaining_g: 800 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ material: 'PLA', remaining_g: 800 }])
    expect(firstText(result)).toMatchObject({ id: SPOOL })
  })

  it('edits and archives one, sending a null to clear a field', async () => {
    const bodies: unknown[] = []
    server.use(
      http.patch(ONE, async ({ request }) => {
        bodies.push(await request.json())
        return HttpResponse.json({ id: SPOOL })
      }),
    )
    const result = await runTool(
      tool('native_spool_upsert'),
      { spool_id: SPOOL, brand: null, archived: true },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(bodies).toEqual([{ brand: null, archived: true }])
  })

  it('refuses to archive a spool it has not named', async () => {
    const result = await runTool(tool('native_spool_upsert'), { archived: true }, ctx())
    expect(result.isError).toBe(true)
  })

  it('lists and deletes', async () => {
    server.use(
      http.get(LIST, ({ request }) => {
        expect(new URL(request.url).searchParams.get('include_archived')).toBe('true')
        return HttpResponse.json([{ id: SPOOL }])
      }),
      http.delete(ONE, () => new HttpResponse(null, { status: 204 })),
    )
    const listed = await runTool(tool('native_spool_list'), { include_archived: true }, ctx())
    expect(firstText(listed)).toEqual([{ id: SPOOL }])
    const deleted = await runTool(tool('native_spool_delete'), { spool_id: SPOOL }, ctx())
    expect(firstText(deleted)).toEqual({ deleted: SPOOL })
  })
})
