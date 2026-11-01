import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, it } from 'vitest'
import { tiersUpTo } from '../src/auth/principal.js'
import { ALL_TOOLS } from '../src/tools/index.js'
import { runTool, type Tool, type ToolContext } from '../src/tools/registry.js'
import { BACKEND, firstText, services } from './helpers/mcp.js'

// #4966 (#284): the print analyzers, their fixes and decisions, through
// backend/scadbuddy/api/analyzers.py. Listing, running and previewing read; applying a
// fix and the decision writes are ScadBuddy's own reversible state (AI spec §8.1 write).

const server = setupServer()
beforeAll(() => server.listen({ onUnhandledFrame: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

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

const TOOLS = {
  list_analyzers: ['read', 'GET /api/v1/analyzers'],
  run_analyzers: ['read', 'POST /api/v1/analyzers/run'],
  preview_analyzer_fix: ['read', 'POST /api/v1/analyzers/fixes/preview'],
  apply_analyzer_fix: ['write', 'POST /api/v1/analyzers/fixes/apply'],
  list_analyzer_decisions: ['read', 'GET /api/v1/analyzers/decisions'],
  record_analyzer_decision: ['write', 'POST /api/v1/analyzers/decisions'],
  remove_analyzer_decision: ['write', 'DELETE /api/v1/analyzers/decisions/{decision_id}'],
} as const

/** Every JSON body POSTed to `path`, answered with `answer`. */
function posted(path: string, answer: unknown, status = 200): unknown[] {
  const bodies: unknown[] = []
  server.use(
    http.post(`${BACKEND}${path}`, async ({ request }) => {
      bodies.push(await request.json())
      return HttpResponse.json(answer as never, { status })
    }),
  )
  return bodies
}

const OUTPUT = 'a'.repeat(32)
const FINGERPRINT = 'f'.repeat(64)

describe('analyzer tools (#4966)', () => {
  it.each(Object.entries(TOOLS))('%s is %s, covering %s', (name, [risk, route]) => {
    const t = tool(name)
    expect(t.risk).toBe(risk)
    expect(t.gated).toBe(false)
    expect(t.routes).toEqual([route])
  })

  it('list_analyzers returns the catalogue', async () => {
    server.use(http.get(`${BACKEND}/api/v1/analyzers`, () => HttpResponse.json([{ id: 'SB1001' }])))
    expect(firstText(await runTool(tool('list_analyzers'), {}, ctx('read')))).toEqual([{ id: 'SB1001' }])
  })

  it('run_analyzers sends an output target and the base request', async () => {
    const bodies = posted('/api/v1/analyzers/run', { diagnostics: [] })
    const result = await runTool(
      tool('run_analyzers'),
      { output_id: OUTPUT, printer_id: 3, plate_id: 2, detail: 'advanced' },
      ctx('read'),
    )
    expect(firstText(result)).toEqual({ diagnostics: [] })
    expect(bodies).toEqual([
      { target: { output_id: OUTPUT }, request: { printer_id: 3, plate_id: 2 }, detail: 'advanced' },
    ])
  })

  it('run_analyzers sends a configuration as a slug with its params', async () => {
    const bodies = posted('/api/v1/analyzers/run', { diagnostics: [] })
    await runTool(tool('run_analyzers'), { slug: 'keychain', params: { name: 'Ada' } }, ctx('read'))
    expect(bodies).toEqual([{ target: { slug: 'keychain', params: { name: 'Ada' } }, request: {}, detail: 'simple' }])
  })

  it('run_analyzers refuses no subject or two, before calling the backend', async () => {
    for (const args of [{}, { output_id: OUTPUT, library_file_id: 4 }]) {
      const result = await runTool(tool('run_analyzers'), args, ctx('read'))
      expect(result.isError).toBe(true)
    }
  })

  it('preview_analyzer_fix sends the diagnostic, the fix and the scope', async () => {
    const bodies = posted('/api/v1/analyzers/fixes/preview', { fingerprint: FINGERPRINT })
    await runTool(
      tool('preview_analyzer_fix'),
      { output_id: OUTPUT, diagnostic_key: 'SB2001', fix_id: 'raise-temp', scope: { kind: 'print', key: OUTPUT } },
      ctx('read'),
    )
    expect(bodies).toEqual([
      {
        target: { output_id: OUTPUT },
        request: {},
        diagnostic_key: 'SB2001',
        fix_id: 'raise-temp',
        scope: { kind: 'print', key: OUTPUT },
      },
    ])
  })

  it('apply_analyzer_fix carries the fingerprint back and confirms it', async () => {
    const bodies = posted('/api/v1/analyzers/fixes/apply', { id: 'd'.repeat(32), kind: 'accept' })
    const result = await runTool(
      tool('apply_analyzer_fix'),
      { output_id: OUTPUT, diagnostic_key: 'SB2001', fix_id: 'raise-temp', fingerprint: FINGERPRINT, reason: 'ok' },
      ctx('write'),
    )
    expect(firstText(result)).toEqual({ id: 'd'.repeat(32), kind: 'accept' })
    expect(bodies).toEqual([
      {
        target: { output_id: OUTPUT },
        request: {},
        diagnostic_key: 'SB2001',
        fix_id: 'raise-temp',
        fingerprint: FINGERPRINT,
        confirm: true,
        reason: 'ok',
      },
    ])
  })

  it('apply_analyzer_fix passes a stale preview back as the backend words it', async () => {
    posted('/api/v1/analyzers/fixes/apply', { title: 'Conflict', detail: 'preview it again' }, 409)
    const result = await runTool(
      tool('apply_analyzer_fix'),
      { output_id: OUTPUT, diagnostic_key: 'SB2001', fix_id: 'raise-temp', fingerprint: FINGERPRINT },
      ctx('write'),
    )
    expect(result.isError).toBe(true)
    expect(JSON.stringify(result.content)).toContain('preview it again')
  })

  it('apply_analyzer_fix is refused to a read-only caller', async () => {
    const result = await runTool(
      tool('apply_analyzer_fix'),
      { output_id: OUTPUT, diagnostic_key: 'SB2001', fix_id: 'raise-temp', fingerprint: FINGERPRINT },
      ctx('read'),
    )
    expect(result.isError).toBe(true)
  })

  it('list_analyzer_decisions sends its filters as the query', async () => {
    const seen: URLSearchParams[] = []
    server.use(
      http.get(`${BACKEND}/api/v1/analyzers/decisions`, ({ request }) => {
        seen.push(new URL(request.url).searchParams)
        return HttpResponse.json([])
      }),
    )
    await runTool(
      tool('list_analyzer_decisions'),
      { scope: 'template', scope_key: 'keychain', diagnostic_id: 'SB2001' },
      ctx('read'),
    )
    expect(Object.fromEntries(seen[0] ?? [])).toEqual({ scope: 'template', scope_key: 'keychain', diagnostic_id: 'SB2001' })
  })

  it('record_analyzer_decision posts the decision', async () => {
    const bodies = posted('/api/v1/analyzers/decisions', { id: 'd'.repeat(32), kind: 'suppress' }, 201)
    await runTool(
      tool('record_analyzer_decision'),
      { diagnostic_id: 'SB2001', kind: 'suppress', scope: { kind: 'global' }, reason: 'known', enforced: true },
      ctx('write'),
    )
    expect(bodies).toEqual([
      {
        diagnostic_id: 'SB2001',
        instance: null,
        kind: 'suppress',
        scope: { kind: 'global', key: '' },
        reason: 'known',
        enforced: true,
        confirm: false,
      },
    ])
  })

  it('remove_analyzer_decision deletes it by id', async () => {
    const id = 'd'.repeat(32)
    const removed: string[] = []
    server.use(
      http.delete(`${BACKEND}/api/v1/analyzers/decisions/${id}`, ({ params }) => {
        removed.push(String(params.decision_id ?? id))
        return new HttpResponse(null, { status: 204 })
      }),
    )
    const result = await runTool(tool('remove_analyzer_decision'), { decision_id: id }, ctx('write'))
    expect(result.isError).toBeFalsy()
    expect(removed).toEqual([id])
  })
})
