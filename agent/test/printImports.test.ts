import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { decide } from '../src/harness/permissions.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { pinnedOutward } from '../src/tools/pinnedOutward.js'
import { effectiveTool, runTool, type Tool, type ToolContext, type ToolOverrides } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #4698 — Import print: list where an unlinked archive's 3MF can be, and link the
// archive to a model. Reading the printer's storage reaches the printer, so a call that
// does is pinned outward (pinnedOutward.ts) whatever lowers the tool's tier.

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
    principal: { id: 'test', kind: 'browser', tiers: tiersUpTo('outward') },
    progress: async () => {},
    signal: new AbortController().signal,
    callKey: 'call-1',
    ...overrides,
  }
}

/** Settings lowering every tool to `read`. */
const lowered: ToolOverrides = { of: async () => ({ tier: 'read', disabled: false }), disabled: async () => new Set() }

const IMPORTED = {
  archive_id: 193,
  slug: 'basketball-trophy',
  print_url: '/api/v1/prints/193',
  created_model: false,
  imported: { archive_id: 193, slug: 'basketball-trophy', source: 'archive', filename: 'x.gcode.3mf' },
}

describe('list_print_sources and import_print (#4698)', () => {
  it('read the sources, and import as a write, through the import routes', () => {
    expect(tool('list_print_sources')).toMatchObject({
      risk: 'read',
      routes: ['GET /api/v1/print-imports/{archive_id}/sources'],
    })
    expect(tool('import_print')).toMatchObject({ risk: 'write', routes: ['POST /api/v1/print-imports/{archive_id}'] })
  })

  it('reads the printer only when asked', async () => {
    const seen: URLSearchParams[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/print-imports/193/sources`, ({ request }) => {
        seen.push(new URL(request.url).searchParams)
        return HttpResponse.json({ archive_id: 193, sources: [] })
      }),
    )
    const result = await runTool(tool('list_print_sources'), { archive_id: 193 }, ctx())
    expect(result.isError).toBeFalsy()
    expect(Object.fromEntries(seen[0]!)).toEqual({})
  })

  it('pins every printer read to a person, whatever the tier', async () => {
    const onPrinter = { archive_id: 193, source: { kind: 'printer', path: '/cache/x.gcode.3mf' }, slug: 'm' }
    const fromArchive = { archive_id: 193, source: { kind: 'archive' }, slug: 'm' }
    expect(pinnedOutward('list_print_sources', { archive_id: 193, printer: true })).toBe(true)
    expect(pinnedOutward('mcp__scadbuddy__list_print_sources', { archive_id: 193, printer: 'yes' })).toBe(true)
    expect(pinnedOutward('list_print_sources', { archive_id: 193 })).toBe(false)
    expect(pinnedOutward('list_print_sources', { archive_id: 193, printer: false })).toBe(false)
    expect(pinnedOutward('import_print', onPrinter)).toBe(true)
    expect(pinnedOutward('import_print', { archive_id: 193 })).toBe(true)
    expect(pinnedOutward('import_print', fromArchive)).toBe(false)
    expect(await effectiveTool(tool('import_print'), { toolOverrides: lowered }, onPrinter)).toMatchObject({
      risk: 'outward',
      gated: true,
    })
    const read = () => 'read' as const
    expect(decide('mcp__scadbuddy__import_print', read, onPrinter)).toMatchObject({
      decision: 'needs_approval',
      tier: 'outward',
    })
    expect(decide('mcp__scadbuddy__import_print', read, fromArchive)).toMatchObject({ decision: 'allow' })
  })

  it('parks a printer import for approval and sends nothing', async () => {
    const sent: unknown[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/print-imports/193`, async ({ request }) => {
        sent.push(await request.json())
        return HttpResponse.json(IMPORTED, { status: 201 })
      }),
    )
    const answer = await runTool(
      tool('import_print'),
      { archive_id: 193, source: { kind: 'printer', path: '/cache/x.gcode.3mf' }, slug: 'basketball-trophy' },
      ctx(),
    )
    expect(firstText(answer)).toMatchObject({ status: 'pending_approval' })
    expect(sent).toEqual([])
  })

  it('imports onto a model with an Idempotency-Key', async () => {
    const seen: { body: unknown; key: string | null }[] = []
    server.use(
      http.post(`${BACKEND}/api/v1/print-imports/193`, async ({ request }) => {
        seen.push({ body: await request.json(), key: request.headers.get('Idempotency-Key') })
        return HttpResponse.json(IMPORTED, { status: 201 })
      }),
    )
    const result = await runTool(
      tool('import_print'),
      { archive_id: 193, source: { kind: 'archive' }, slug: 'basketball-trophy' },
      ctx(),
    )
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toEqual(IMPORTED)
    expect(seen).toHaveLength(1)
    expect(seen[0]!.body).toEqual({ source: { kind: 'archive' }, slug: 'basketball-trophy' })
    expect(seen[0]!.key).toMatch(/^[0-9a-f]{64}$/)
  })

  it('links a new model once its operation is done, with the same key', async () => {
    const keys: (string | null)[] = []
    const model = { slug: 'trophy', name: 'Trophy' }
    const op = { id: 'op-1', kind: 'mesh_model_create', subject: 'trophy', status: 'running', created_at: '2026-11-10T00:00:00Z' }
    server.use(
      http.post(`${BACKEND}/api/v1/print-imports/193`, ({ request }) => {
        keys.push(request.headers.get('Idempotency-Key'))
        return keys.length === 1
          ? HttpResponse.json(op, { status: 202 })
          : HttpResponse.json({ ...IMPORTED, slug: 'trophy', created_model: true })
      }),
      http.get(`${BACKEND}/api/v1/operations/op-1`, () => HttpResponse.json({ ...op, status: 'succeeded', result: model })),
    )
    const result = await runTool(
      tool('import_print'),
      { archive_id: 193, source: { kind: 'archive_source' }, new_model: { name: 'Trophy' } },
      ctx({ pollIntervalMs: 1 }),
    )
    expect(result.isError).toBeFalsy()
    expect(firstText(result)).toMatchObject({ slug: 'trophy', created_model: true })
    expect(keys).toHaveLength(2)
    expect(keys[1]).toBe(keys[0])
  })

  it('refuses slug and new_model together', async () => {
    const result = await runTool(
      tool('import_print'),
      { archive_id: 193, source: { kind: 'archive' }, slug: 'm', new_model: { name: 'N' } },
      ctx(),
    )
    expect(result.isError).toBe(true)
  })
})
