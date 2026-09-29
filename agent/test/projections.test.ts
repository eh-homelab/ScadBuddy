import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { InMemoryTransport } from '@modelcontextprotocol/sdk/inMemory.js'
import type { McpServer } from '@modelcontextprotocol/sdk/server/mcp.js'
import { readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS, tierOf } from '../src/tools/index.js'
import { createExternalServer, createHarnessServer } from '../src/tools/projections.js'
import { createBackendClient } from '../src/api/backend.js'
import { BACKEND, services } from './helpers/mcp.js'

// Spec §5.1: "A test asserts both lists are identical, apart from browser-only
// tools." There are no browser-only tools yet (#254), so the lists must be
// equal outright: names, descriptions, input schemas and annotations.
//
// One normalisation: the two servers turn the same zod union of primitives
// into JSON Schema differently. The Agent SDK's bundled server writes
// `{"type": ["boolean", "null"]}`, @modelcontextprotocol/sdk writes
// `{"anyOf": [{"type": "boolean"}, {"type": "null"}]}`, which mean the same.
// Both are rewritten to a sorted `anyOf` before comparing; nothing else is.

function normalise(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalise)
  if (!value || typeof value !== 'object') return value
  const entries = Object.entries(value as Record<string, unknown>).map(([k, v]) => [k, normalise(v)] as const)
  const out: Record<string, unknown> = Object.fromEntries(entries)
  if (Array.isArray(out.type)) {
    out.anyOf = (out.type as string[]).map((type) => ({ type }))
    delete out.type
  }
  const anyOf = out.anyOf as { type?: unknown }[] | undefined
  if (anyOf?.every((o) => typeof o.type === 'string' && Object.keys(o).length === 1)) {
    out.anyOf = [...anyOf].sort((a, b) => String(a.type).localeCompare(String(b.type)))
  }
  return out
}

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
  it('serve the same tools in-process (harness) and over /mcp', async () => {
    const svc = services()
    const harness = createHarnessServer(ALL_TOOLS, svc, { id: 'browser', kind: 'browser', tiers: tiersUpTo('outward') })
    expect(harness.type).toBe('sdk')
    expect(harness.name).toBe('scadbuddy')
    const inProcess = await listVia(harness.instance)
    const external = await listVia(createExternalServer(ALL_TOOLS, svc))

    expect(inProcess.map((t) => t.name)).toEqual(ALL_TOOLS.map((t) => t.name).sort())
    expect(normalise(inProcess)).toEqual(normalise(external))
    // A defaulted field lists its literal default on both sides, not just the
    // same thing (claude-review of #526, finding 1).
    for (const listing of [inProcess, external]) {
      const tool = listing.find((t) => t.name === 'update_source')!
      expect((tool.inputSchema.properties as Record<string, { default?: unknown }>).force?.default).toBe(false)
    }
  })

  it('fill an omitted default in-process (the SDK 0.3.283 server refused it given a raw shape)', async () => {
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
    expect(bodies).toEqual([{ source: 'cube(1);', message: null, force: false }])
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

  it('re-check the whole-z.object cast when the Agent SDK moves off 0.3.283', async () => {
    // src/tools/projections.ts hands the SDK's tool() a z.object behind an
    // `as unknown as` cast, because the 0.3.283 server mishandles a raw shape
    // with `.default()` fields. The cast silences the types, so a bump must
    // re-run the two tests above by hand, then drop the cast if the SDK now
    // fills defaults from a raw shape, and move this pin either way.
    const sdkEntry = fileURLToPath(import.meta.resolve('@anthropic-ai/claude-agent-sdk'))
    const manifest = JSON.parse(await readFile(path.join(path.dirname(sdkEntry), 'package.json'), 'utf8')) as {
      version: string
    }
    expect(manifest.version, 'the z.object cast in createHarnessServer was measured on 0.3.283; re-check it').toBe(
      '0.3.283',
    )
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
    // ScadBuddy's own settings.json, and `/print/runs/` only reads its database.
    const bambuddyRoutes = /\/print\/(?!runs\/)|\/send$|\/settings\/(test|targets)$/
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
