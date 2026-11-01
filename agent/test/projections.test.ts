import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS, tierOf } from '../src/tools/index.js'
import { createExternalServer, createHarnessServer } from '../src/tools/projections.js'
import { createBackendClient } from '../src/api/backend.js'
import { BACKEND, services } from './helpers/mcp.js'

// Spec §5.1: "A test asserts both lists are identical, apart from browser-only
// tools." The browser_* tools (#254, src/tools/browser.ts) are in both lists,
// so the lists must be equal outright: names, descriptions, input schemas and
// annotations. (The Agent SDK 0.3.283 server wrote a union of primitives as
// `{"type": [...]}` where @modelcontextprotocol/sdk writes `anyOf`, so this
// compared normalised lists; 0.3.287 writes the same, measured 2026-10-06.)

async function listVia(server: McpServer) {
  const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
  await server.connect(serverSide)
  const client = new Client({ name: 'projection-test', version: '0' })
  await client.connect(clientSide)
  const { tools } = await client.listTools()
  await client.close()
  return tools.sort((a, b) => a.name.localeCompare(b.name))
}

describe('registry projections', () => {
  it('keep in the turn exactly the browser_* tools, whose tab only the turn reaches (plan 2026-10-13 Step 9.2)', () => {
    expect(ALL_TOOLS.filter((t) => t.inTurn).map((t) => t.name)).toEqual(ALL_TOOLS.filter((t) => t.name.startsWith('browser_')).map((t) => t.name))
    expect(ALL_TOOLS.filter((t) => t.inTurn).length).toBeGreaterThan(0)
  })

  it('serve the same tools in-process (harness) and over /mcp', async () => {
    const svc = services()
    const harness = createHarnessServer(ALL_TOOLS, svc, { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') })
    expect(harness.type).toBe('sdk')
    expect(harness.name).toBe('scadbuddy')
    const inProcess = await listVia(harness.instance)
    const external = await listVia(createExternalServer(ALL_TOOLS, svc))

    expect(inProcess.map((t) => t.name)).toEqual(ALL_TOOLS.map((t) => t.name).sort())
    // The SDK's in-process tool has no top-level `title`; the label is in annotations.title on both.
    expect(inProcess).toEqual(external.map(({ title: _title, ...rest }) => rest))
    expect(external.map((t) => t.title)).toEqual(external.map((t) => ALL_TOOLS.find((x) => x.name === t.name)?.label))
    // A defaulted field lists its literal default on both sides, not just the
    // same thing (claude-review of #526, finding 1).
    for (const listing of [inProcess, external]) {
      const tool = listing.find((t) => t.name === 'update_source')!
      expect((tool.inputSchema.properties as Record<string, { default?: unknown }>).force?.default).toBe(false)
    }
  })

  // The harness server gets each tool's raw shape (projections.ts). SDK 0.3.283
  // refused this call ("expected nonoptional"); 0.3.287 fills the default.
  it('fill an omitted default in-process', async () => {
    const bodies: unknown[] = []
    const backend = createBackendClient(BACKEND, async (request) => {
      bodies.push(await (request as Request).json())
      return Response.json({ slug: 'box', name: 'Box', origin: 'user', has_thumbnail: false, has_readme: false, updated_at: 'now' })
    })
    const principal = { id: 'browser', kind: 'browser' as const, tiers: tiersUpTo('outward') }
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await createHarnessServer(ALL_TOOLS, services({ backend }), principal).instance.connect(serverSide)
    const client = new Client({ name: 'projection-test', version: '0' })
    await client.connect(clientSide)
    // `force` has a default (`z.boolean().default(false)`); the caller leaves it out.
    const result = await client.callTool({ name: 'update_source', arguments: { slug: 'box', source: 'cube(1);' } })
    await client.close()
    expect(result.isError, JSON.stringify(result)).toBeFalsy()
    expect(bodies).toEqual([{ source: 'cube(1);', message: null, force: false, base: null }])
  })

  it('still refuse an omitted required argument in-process', async () => {
    let called = false
    const backend = createBackendClient(BACKEND, async () => {
      called = true
      return Response.json({})
    })
    const principal = { id: 'browser', kind: 'browser' as const, tiers: tiersUpTo('outward') }
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await createHarnessServer(ALL_TOOLS, services({ backend }), principal).instance.connect(serverSide)
    const client = new Client({ name: 'projection-test', version: '0' })
    await client.connect(clientSide)
    // `source` has no default; leaving it out must fail before any backend call.
    const result = await client
      .callTool({ name: 'update_source', arguments: { slug: 'box' } })
      .catch((error: unknown) => ({ isError: true, error }))
    await client.close()
    expect(result.isError, JSON.stringify(result)).toBe(true)
    expect(called).toBe(false)
  })

  // #2510: the projections pass each tool's raw shape, so the SDK strips an unknown
  // `*_base64` key rather than refusing it; the call still fails, on the missing
  // pointer, and nothing reaches the backend (claude-review of #4532).
  it.each(['harness', 'external'] as const)('refuse a stale *_base64 call over the %s projection', async (side) => {
    let called = false
    const backend = createBackendClient(BACKEND, async () => {
      called = true
      return Response.json({})
    })
    const principal = { id: 'browser', kind: 'browser' as const, tiers: tiersUpTo('outward') }
    const server =
      side === 'harness'
        ? createHarnessServer(ALL_TOOLS, services({ backend }), principal).instance
        : createExternalServer(ALL_TOOLS, services({ backend }))
    const [clientSide, serverSide] = InMemoryTransport.createLinkedPair()
    await server.connect(serverSide)
    const client = new Client({ name: 'projection-test', version: '0' })
    await client.connect(clientSide)
    const result = await client
      .callTool({ name: 'set_model_thumbnail', arguments: { slug: 'w', png_base64: 'iVBORw0KGgo=' } })
      .catch((error: unknown) => ({ isError: true, error }))
    await client.close()
    expect(result.isError, JSON.stringify(result)).toBe(true)
    expect(called).toBe(false)
  })

  it('mark read tools readOnly and outward tools destructive', () => {
    for (const tool of ALL_TOOLS) {
      expect(tool.annotations.readOnlyHint, tool.name).toBe(tool.risk === 'read')
      expect(tool.annotations.destructiveHint, tool.name).toBe(tool.risk === 'outward')
      // Every outward tool is gated, except the approval path itself: the
      // gate's own confirm, and deciding another agent's approval (#300).
      const approvalPath = ['confirm_action', 'sessions_approve', 'sessions_deny'].includes(tool.name)
      expect(tool.gated, tool.name).toBe(tool.risk === 'outward' && !approvalPath)
    }
  })

  it('declare a Bambuddy scope on every tool that reaches Bambuddy', () => {
    // The backend routes that call Bambuddy (backend/scadbuddy/api/printing.py,
    // outputs.py `send`, settings.py `test`/`targets`); `remember_*` only write
    // ScadBuddy's own settings.json, `/print/runs/` only reads its database, and
    // `/print/filament-profiles` lists ScadBuddy's built-in profiles (#717).
    const bambuddyRoutes = /\/print\/(?!runs\/|filament-profiles$)|\/send$|\/settings\/(test|targets)$/
    for (const tool of ALL_TOOLS) {
      if (tool.routes.some((r) => bambuddyRoutes.test(r.split(' ')[1]!)) && !tool.name.startsWith('remember_')) {
        expect(tool.bambuddyScope.length, tool.name).toBeGreaterThan(0)
      }
    }
  })

  it('resolve a harness tool name to its tier, and nothing else', () => {
    expect(tierOf('mcp__scadbuddy__list_models')).toBe('read')
    expect(tierOf('mcp__scadbuddy__render_model')).toBe('write')
    expect(tierOf('mcp__scadbuddy__print_output')).toBe('outward')
    expect(tierOf('mcp__scadbuddy__nope')).toBeUndefined()
    expect(tierOf('mcp__other__list_models')).toBeUndefined()
    expect(tierOf('Bash')).toBeUndefined()
  })
})
